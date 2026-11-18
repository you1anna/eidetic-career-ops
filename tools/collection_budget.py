"""Durable attempt reservations at dispatch. No working-record database access."""
from contextlib import contextmanager
from contextvars import ContextVar
import datetime as dt
import fcntl
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workspace'))
import collection_policy
from collection_reads import AUTHORITY, SCHEMA, connect, day, instant, summary

class Deferred(RuntimeError):
    def __init__(self,provider,reason):
        self.provider,self.reason=provider,reason
        super().__init__(f'Collection deferred: {provider} ({reason})')
        self.fetch_kind='collection-deferred'

_current=ContextVar('collection_budget',default=None)
_task=ContextVar('collection_task',default='')

@contextmanager
def using(b):
    token=_current.set(b)
    try:yield b
    finally:_current.reset(token)

@contextmanager
def task(key):
    token=_task.set(key)
    try:yield
    finally:_task.reset(token)

def initialize(path=AUTHORITY,*,at=None,hold_today=True):
    """Explicit cutover/recovery only. Never called automatically by a reader/dispatcher."""
    from zoneinfo import ZoneInfo
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    now=instant(at)
    next_day=dt.date.fromisoformat(day(now))+dt.timedelta(days=1)
    hold=dt.datetime.combine(next_day,dt.time(),ZoneInfo('Europe/London')).astimezone(dt.timezone.utc) if hold_today else now
    # Exclusive create means re-running cutover cannot reset spent allowance.
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE attempts(id TEXT PRIMARY KEY,provider TEXT NOT NULL,day TEXT NOT NULL,at TEXT NOT NULL,caller TEXT NOT NULL,task TEXT NOT NULL,status TEXT NOT NULL,kind TEXT NOT NULL);
        CREATE INDEX attempts_day ON attempts(day,provider,caller);
        CREATE INDEX attempts_recent ON attempts(provider,at);
        CREATE TABLE tasks(provider TEXT NOT NULL,key TEXT NOT NULL,last_attempt TEXT,last_success TEXT,PRIMARY KEY(provider,key));
        CREATE INDEX tasks_success ON tasks(provider,last_success);
        CREATE INDEX tasks_attempt ON tasks(provider,last_attempt);
        CREATE TABLE deferrals(day TEXT NOT NULL,provider TEXT NOT NULL,reason TEXT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(day,provider,reason));''')
        c.executemany('INSERT INTO meta VALUES(?,?)',[('hold_until',hold.isoformat()),('clock',now.isoformat())])
        c.execute(f'PRAGMA user_version={SCHEMA}')

class Budget:
    def __init__(self,path=AUTHORITY,policy=None,now=None,sleep=time.sleep):
        self.path=Path(path);self.policy=policy or collection_policy.load()
        self.now=now or (lambda:dt.datetime.now(dt.timezone.utc));self.sleep=sleep
    def connection(self):
        # mode=rw prevents missing/renamed/restored ledgers silently creating a fresh allowance.
        c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=rw',uri=True,timeout=2,isolation_level=None)
        c.row_factory=sqlite3.Row;c.execute('PRAGMA synchronous=FULL')
        if c.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA:
            c.close();raise ValueError('Invalid collection schema')
        return c
    def reserve(self,provider,caller='attended',task='',kind='read'):
        try:
            p=self.policy['providers'][provider]
            # One provider lock serializes dispatch pacing, not network I/O or record saves.
            with self.path.with_name('.collection-'+provider+'.lock').open('a') as lock:
                deadline=time.monotonic()+60
                while True:
                    try:
                        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:
                        if time.monotonic()>=deadline:raise Deferred(provider,'pacing-busy')
                        self.sleep(.05)
                c=self.connection()
                try:
                    # Pacing waits outside a SQLite transaction. Other providers can reserve.
                    last=c.execute('SELECT MAX(at) FROM attempts WHERE provider=?',(provider,)).fetchone()[0]
                    if last and instant(self.now())>=instant(last):self.sleep(max(0,p['min_interval_seconds']-(instant(self.now())-instant(last)).total_seconds()))
                    now=instant(self.now());today=day(now)
                    c.execute('BEGIN IMMEDIATE')
                    meta=dict(c.execute('SELECT key,value FROM meta'))
                    reason=None
                    if now<instant(meta['hold_until']):reason='cutover-hold'
                    elif now<instant(meta['clock']):reason='clock-rewind'
                    counts=c.execute('SELECT COUNT(*),COALESCE(SUM(caller=?),0) FROM attempts WHERE day=? AND provider=?',('scan',today,provider)).fetchone()
                    if caller=='scan' and provider=='reed' and kind in ('search','detail'):
                        spent=c.execute('SELECT COUNT(*) FROM attempts WHERE day=? AND provider=? AND caller=? AND kind=?',(today,provider,caller,kind)).fetchone()[0]
                        if spent>=p[kind+'_limit']:reason=kind+'-limit'
                    if caller=='scan' and provider=='companies-house' and task:
                        bundles={r[0] for r in c.execute('SELECT DISTINCT task FROM attempts WHERE day=? AND provider=? AND caller=?',(today,provider,caller))}
                        if task not in bundles and len(bundles)>=p['company_limit']:reason='company-limit'
                    if counts[0]>=p['daily_limit']:reason='daily-limit'
                    elif caller=='scan' and counts[1]>=p['scheduled_target']:reason='scheduled-target'
                    if reason:
                        c.execute('INSERT INTO deferrals VALUES(?,?,?,1) ON CONFLICT(day,provider,reason) DO UPDATE SET count=count+1',(today,provider,reason));c.commit()
                        raise Deferred(provider,reason)
                    rid=uuid.uuid4().hex
                    c.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',(rid,provider,today,now.isoformat(),caller,task,'reserved',kind))
                    c.execute('UPDATE meta SET value=? WHERE key=?',(now.isoformat(),'clock'))
                    if task:c.execute('INSERT INTO tasks(provider,key,last_attempt) VALUES(?,?,?) ON CONFLICT(provider,key) DO UPDATE SET last_attempt=excluded.last_attempt',(provider,task,now.isoformat()))
                    c.commit();return rid
                finally:c.close()
        except Deferred:raise
        except (OSError,sqlite3.Error,ValueError,KeyError) as e:raise Deferred(provider,'accounting-unavailable') from e
    def finish(self,rid,status):
        try:
            c=self.connection()
            try:c.execute('UPDATE attempts SET status=? WHERE id=?',(str(status),rid))
            finally:c.close()
        except (OSError,sqlite3.Error,ValueError):pass # reservation stays spent/unknown
    def defer(self,provider,reason):
        try:
            c=self.connection()
            try:c.execute('INSERT INTO deferrals VALUES(?,?,?,1) ON CONFLICT(day,provider,reason) DO UPDATE SET count=count+1',(day(self.now()),provider,reason))
            finally:c.close()
        except (OSError,sqlite3.Error,ValueError):pass
    def summary(self):return summary(self.path,self.policy,self.now())
    def history(self,provider,keys=None):
        try:
            c=connect(self.path)
            try:
                if keys is None:return {r['key']:dict(r) for r in c.execute('SELECT * FROM tasks WHERE provider=?',(provider,))}
                result={}
                for start in range(0,len(keys),900):
                    chunk=keys[start:start+900]
                    result.update({r['key']:dict(r) for r in c.execute('SELECT * FROM tasks WHERE provider=? AND key IN ('+','.join('?'*len(chunk))+')',(provider,*chunk))})
                return result
            finally:c.close()
        except (OSError,sqlite3.Error,ValueError) as e:raise Deferred(provider,'accounting-unavailable') from e
    def succeeded(self,provider,key,at=None):
        c=self.connection()
        try:c.execute('UPDATE tasks SET last_success=? WHERE provider=? AND key=?',(instant(at or self.now()).isoformat(),provider,key))
        finally:c.close()

def active():
    current=_current.get()
    if current:return current
    if not AUTHORITY.exists():raise Deferred('collection','VPS-authority-required')
    try:return Budget(AUTHORITY)
    except (OSError,ValueError) as e:raise Deferred('collection','invalid-policy') from e

def begin(url,inherited=None):
    provider=collection_policy.provider_for(url) or (inherited[2] if inherited else None)
    if not provider:return None
    b=inherited[0] if inherited else active();caller='scan' if os.environ.get('WO_COLLECTION_CALLER')=='scan' else 'research' if os.environ.get('WO_COLLECTION_CALLER')=='research' else 'attended'
    from urllib.parse import urlsplit
    kind=inherited[3] if inherited and inherited[2]==provider else ('detail' if '/jobs/' in urlsplit(url).path else 'search') if provider=='reed' else 'read'
    return b,b.reserve(provider,caller,_task.get(),kind),provider,kind

def finish(reservation,status):
    if reservation:reservation[0].finish(reservation[1],status)
