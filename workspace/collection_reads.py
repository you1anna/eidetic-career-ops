"""Read-only indexed accounting projections. Missing evidence is unknown, never zero."""
import datetime as dt
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo
import collection_policy

SCHEMA = 1
AUTHORITY = Path('/srv/work-ops/data/collection.db')

def instant(at=None):
    v=at or dt.datetime.now(dt.timezone.utc)
    if isinstance(v,str):v=dt.datetime.fromisoformat(v.replace('Z','+00:00'))
    if v.tzinfo is None:raise ValueError('Collection clock needs a timezone')
    return v.astimezone(dt.timezone.utc)

def day(at):return instant(at).astimezone(ZoneInfo('Europe/London')).date().isoformat()

def connect(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    c.row_factory=sqlite3.Row
    if c.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA:
        c.close();raise ValueError('Collection ledger schema unavailable')
    return c

def summary(path=AUTHORITY, policy=None, at=None):
    try:policy=policy or collection_policy.load()
    except (OSError,ValueError):
        return dict(state='unknown',reason='invalid-policy',authority=str(path),production_authority=str(AUTHORITY),
                    policy_file=str(collection_policy.PATH),version=None,providers={})
    now=instant(at);today=day(now)
    out=dict(state='ok',authority=str(path),policy_file=str(collection_policy.PATH),version=policy['version'],
             timezone=policy['timezone'],day=today,effective_policy=policy,production_authority=str(AUTHORITY),providers={})
    try:
        c=connect(path)
        try:
            c.execute('BEGIN')
            meta=dict(c.execute('SELECT key,value FROM meta'))
            for key in ('hold_until','clock'):
                if key not in meta:raise ValueError('Collection accounting recovery state missing')
                instant(meta[key])
            out['blocked_reason']='clock-rewind' if now<instant(meta['clock']) else 'cutover-hold' if now<instant(meta['hold_until']) else None
            since=(dt.date.fromisoformat(today)-dt.timedelta(days=31)).isoformat()
            rows=c.execute('SELECT provider,day,caller,status,kind,COUNT(*) n,MAX(at) last FROM attempts WHERE day>=? GROUP BY provider,day,caller,status,kind',(since,)).fetchall()
            tasks=[dict(provider=p,last_attempt=c.execute('SELECT MAX(at) FROM attempts WHERE provider=?',(p,)).fetchone()[0],last_success=c.execute('SELECT MAX(last_success) FROM tasks WHERE provider=?',(p,)).fetchone()[0],
                        oldest_attempt=c.execute('SELECT MIN(last_attempt) FROM tasks WHERE provider=?',(p,)).fetchone()[0]) for p in policy['providers']]
            deferred=c.execute('SELECT provider,SUM(count) n FROM deferrals WHERE day=? GROUP BY provider',(today,)).fetchall()
        finally:c.close()
    except (OSError,sqlite3.Error,ValueError) as e:
        out.update(state='unknown',reason=type(e).__name__);rows=tasks=deferred=[];meta={}
    for provider,p in policy['providers'].items():
        own=[r for r in rows if r['provider']==provider];current=[r for r in own if r['day']==today]
        out['providers'][provider]=dict(daily_limit=p['daily_limit'],scheduled_target=p['scheduled_target'],account=p.get('account',{}),
            used=sum(r['n'] for r in current) if out['state']=='ok' else None,
            reserved=sum(r['n'] for r in current if r['status']=='reserved') if out['state']=='ok' else None,
            scheduled_used=sum(r['n'] for r in current if r['caller']=='scan') if out['state']=='ok' else None,
            search_used=sum(r['n'] for r in current if r['caller']=='scan' and r['kind']=='search') if out['state']=='ok' else None,
            detail_used=sum(r['n'] for r in current if r['caller']=='scan' and r['kind']=='detail') if out['state']=='ok' else None,
            seven_days=None if out['state']!='ok' else sum(r['n'] for r in own if r['day']>=(dt.date.fromisoformat(today)-dt.timedelta(days=6)).isoformat()),
            month=None if out['state']!='ok' else sum(r['n'] for r in own if r['day'][:7]==today[:7]),
            deferred=None if out['state']!='ok' else next((r['n'] for r in deferred if r['provider']==provider),0),
            last_attempt=next((r['last_attempt'] for r in tasks if r['provider']==provider),None),
            last_success=next((r['last_success'] for r in tasks if r['provider']==provider),None),
            oldest_attempt=next((r['oldest_attempt'] for r in tasks if r['provider']==provider),None),
            hold_until=meta.get('hold_until'))
    return out
