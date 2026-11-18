"""Read-only allowance counters shared by the unattended workers and the Dashboard.

Moved verbatim from tools/automation_usage.py (Codex invocation limits, London periods and the
weekly summary) and tools/discovery_intake.py (the rolling automatic-admission ledger), which
re-export them. Writers stay in those modules; nothing here writes.
"""
import datetime as dt
import json
from zoneinfo import ZoneInfo

_policy_cache = {}

def policy():
    from pathlib import Path
    import yaml
    import hashlib
    import copy
    raw=(Path(__file__).resolve().parents[1]/'config/research.yml').read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    if _policy_cache.get('digest')==digest:return copy.deepcopy(_policy_cache['value'])
    value=yaml.safe_load(raw).get('allowances')
    if not isinstance(value,dict) or set(value)!={'daily','weekly','lanes','sublimits'}:
        raise ValueError('Invalid Codex allowance configuration')
    if any(type(value[k]) is not int or value[k]<1 for k in ('daily','weekly')):
        raise ValueError('Invalid Codex aggregate allowance')
    if set(value['lanes'])!={'research','incident','question','review','evaluation'}:
        raise ValueError('Invalid Codex allowance lanes')
    for limits in value['lanes'].values():
        if not isinstance(limits,list) or len(limits)!=2 or any(type(x) is not int or x<0 for x in limits) or limits[0]>limits[1]:
            raise ValueError('Invalid Codex lane allowance')
    limits=value['sublimits'].get('preparation')
    if set(value['sublimits'])!={'preparation'} or not isinstance(limits,list) or len(limits)!=2 or limits[0]!='research' or type(limits[1]) is not int or not 0<=limits[1]<=value['lanes']['research'][1]:
        raise ValueError('Invalid Codex preparation sublimit')
    _policy_cache.update(digest=digest,value=value)
    return copy.deepcopy(value)


def __getattr__(name):
    key={'DAILY':'daily','WEEKLY':'weekly','LANES':'lanes','SUBLIMITS':'sublimits'}.get(name)
    if key:return policy()[key]
    raise AttributeError(name)


def periods(at):
    when=dt.datetime.fromisoformat(at.replace('Z','+00:00'))
    if when.tzinfo is None: raise ValueError('Usage needs a timezone')
    day=when.astimezone(ZoneInfo('Europe/London')).date()
    return day.isoformat(), (day-dt.timedelta(days=day.weekday())).isoformat()


def summary(conn,at):
    config=policy();lanes=config['lanes'];sublimits=config['sublimits']
    day,week=periods(at)
    rows=conn.execute('SELECT lane,day,COUNT(*) n FROM codex_invocations WHERE week=? GROUP BY lane,day',(week,)).fetchall()
    tagged=conn.execute("SELECT COUNT(*) FROM codex_invocations WHERE week=? AND lane='research' AND id LIKE 'preparation:%'",(week,)).fetchone()[0]
    return dict(day=sum(r['n'] for r in rows if r['day']==day),week=sum(r['n'] for r in rows),
                preparation_week=tagged,preparation_week_limit=sublimits['preparation'][1],
                research_day=sum(r['n'] for r in rows if r['day']==day and r['lane']=='research'),
                research_week=sum(r['n'] for r in rows if r['lane']=='research'),
                day_limit=config['daily'],week_limit=config['weekly'],research_day_limit=lanes['research'][0],research_week_limit=lanes['research'][1],
                day_start=day,week_start=week,timezone='Europe/London')


KEY = 'discovery_capacity_admissions_v1'


def instant(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc)


def ledger(conn, now):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (KEY,)).fetchone()
    entries = json.loads(row[0]) if row else []
    if not isinstance(entries, list) or len(entries) > 1000 or any(not isinstance(e, dict) or not isinstance(e.get('id'), str)
                                          or not isinstance(e.get('at'), str) for e in entries):
        raise ValueError('Invalid discovery admission ledger; refusing unbounded intake')
    if len({e['id'] for e in entries}) != len(entries):
        raise ValueError('Duplicate discovery admissions; refusing unbounded intake')
    # A future timestamp continues to spend allowance, rather than silently resetting it.
    return [e for e in entries if instant(e['at']) > now - dt.timedelta(days=7)]
