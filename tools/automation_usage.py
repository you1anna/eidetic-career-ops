"""Durable admission quotas for all unattended CLI work; no invented credit prices."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import uuid

# The limits, London periods and read-only summary live in workspace/allowances.py, which the
# Dashboard reads without importing tools/; they are re-exported here unchanged.
from allowances import periods, summary, policy

def __getattr__(name):
    import allowances
    return getattr(allowances,name)

class Limited(RuntimeError): pass

@contextmanager
def serial(conn):
    path=conn.execute('PRAGMA database_list').fetchone()[2]
    if not path: raise ValueError('Usage needs a durable database')
    with Path(path).with_suffix('.codex.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise Limited('Another Codex invocation is running') from None
        try: yield
        finally: fcntl.flock(lock,fcntl.LOCK_UN)

def seed(conn):
    """Import legacy per-run counts once. Cutover cannot create a fresh allowance."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        if not conn.execute("SELECT 1 FROM meta WHERE key='codex_usage_seeded'").fetchone():
            for row in conn.execute("SELECT run_id,receipt_json FROM scan_runs WHERE run_id LIKE 'research:%'").fetchall():
                r=json.loads(row['receipt_json']); count=r.get('usage',{}).get('calls',0)
                if type(count) is not int or not 0<=count<=60: raise ValueError('Invalid historical Codex usage')
                # Legacy receipts could die after dispatch but before the next usage save.
                if r.get('status')=='running':count+=1
                if not count: continue
                day,week=periods(r['started_at'])
                for i in range(count):
                    conn.execute('INSERT OR IGNORE INTO codex_invocations(id,run_id,lane,started_at,day,week) VALUES(?,?,?,?,?,?)',
                                 (f"legacy:{row['run_id']}:{i}",row['run_id'],'research',r['started_at'],day,week))
            conn.execute("INSERT INTO meta(key,value) VALUES('codex_usage_seeded','1')")
        conn.commit()
    except BaseException: conn.rollback(); raise

def reserve(conn,run_id,lane,at,purpose=''):
    config=policy();LANES=config['lanes'];SUBLIMITS=config['sublimits']
    DAILY,WEEKLY=config['daily'],config['weekly']
    if lane not in LANES: raise ValueError('Unknown automation lane')
    if purpose and SUBLIMITS.get(purpose,('',))[0]!=lane: raise ValueError('Unknown purpose for this automation lane')
    conn.execute('PRAGMA synchronous=FULL')
    seed(conn)
    day,week=periods(at); rid=(purpose+':' if purpose else '')+uuid.uuid4().hex
    conn.execute('BEGIN IMMEDIATE')
    try:
        totals=summary(conn,at)
        row=conn.execute('SELECT COUNT(*),COALESCE(SUM(day=?),0) FROM codex_invocations WHERE week=? AND lane=?',(day,week,lane)).fetchone()
        if totals['day']>=DAILY or totals['week']>=WEEKLY or row[0]>=LANES[lane][1] or row[1]>=LANES[lane][0]:
            raise Limited('Daily or weekly Codex invocation cap reached')
        if purpose and conn.execute('SELECT COUNT(*) FROM codex_invocations WHERE week=? AND lane=? AND id LIKE ?',
                                    (week,lane,purpose+':%')).fetchone()[0]>=SUBLIMITS[purpose][1]:
            raise Limited('Weekly '+purpose+' sublimit reached')
        conn.execute('INSERT INTO codex_invocations(id,run_id,lane,started_at,day,week) VALUES(?,?,?,?,?,?)',
                     (rid,run_id,lane,at,day,week));conn.commit()
    except BaseException: conn.rollback();raise
    return rid

def finish(conn,rid,usage,completed):
    usage={k:v for k,v in (usage or {}).items() if k in ('input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens') and type(v) is int and v>=0}
    conn.execute('UPDATE codex_invocations SET status=?,usage_json=? WHERE id=?',
                 ('completed' if completed else 'unknown',json.dumps(usage),rid))
