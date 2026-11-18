"""The Dashboard's server-only figures: capacity, quotas, recent hunt counts and responsiveness.

Everything else the Dashboard shows is already in the page's summary or `/api/status`, so this
never builds workspace state, never takes the build lock and never writes. Each query is bounded
by a primary-key range, an index or a LIMIT. A section that cannot be read says so instead of
reporting zero, and never takes the others down.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

import allowances
import collection_policy
import collection_reads
from approach_history import ApproachHistory
import record_read
import request_timings
from record_views import SPOOL_WARN_BYTES, SPOOL_WARN_COUNT

ROOT = Path(__file__).resolve().parent.parent
LONDON = ZoneInfo('Europe/London')
SCAN_RUNS_SHOWN = 7          # one week of daily scans
HEADROOM_TARGET = 0.20       # plan task 1: keep a fifth of the service limit free
RECENT_DAYS = 30
JOB_STATUS_SHOWN = ('applied', 'responded', 'interview', 'offer', 'rejected', 'closed')
_PORTALS: dict = {}


def overview(conn, data_dir, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    with record_read.snapshot(conn):
        out = dict(capacity=_section(_capacity, conn, Path(data_dir)), quotas=_section(_quotas, conn, now),
                   hunt=_section(_hunt, conn, now))
    out['quotas']['collection'] = _section(collection_reads.summary, Path(data_dir)/'collection.db', None, now)
    out.update(responsiveness=request_timings.snapshot(), generated_at=now.isoformat(timespec='seconds'))
    return out


def _section(read, *args):
    try:
        return {'state': 'ok', **read(*args)}
    except Exception as error:  # one unreadable source must not hide the rest
        return dict(state='unknown', reason=type(error).__name__)


def _receipts(conn, prefix, limit=SCAN_RUNS_SHOWN, fields=()):
    """The newest receipts of one kind, newest first: a primary-key range read, never the archive."""
    select = ', '.join(f"json_extract(receipt_json, '$.{f}')" for f in fields)
    rows = conn.execute(f'SELECT run_id, {select} FROM scan_runs WHERE run_id >= ? AND run_id < ? '
                        'ORDER BY run_id DESC LIMIT ?', (f'{prefix}:', f'{prefix};', limit)).fetchall()
    return [dict(zip(('run_id', *fields), row)) for row in rows]


def _memory(raw):
    m = json.loads(raw) if raw else {}
    group = m.get('cgroup') or {}
    events = group.get('events') or {}
    peaks = [v for v in (m.get('process_peak_rss_bytes'), m.get('largest_child_peak_rss_bytes')) if isinstance(v, int)]
    return dict(process_peak_bytes=m.get('process_peak_rss_bytes'),
                child_peak_bytes=m.get('largest_child_peak_rss_bytes'),
                largest_process_bytes=max(peaks) if peaks else None,
                cgroup_peak_bytes=group.get('peak_bytes'), anon_bytes=group.get('anon_bytes'),
                limit_bytes=group.get('limit_bytes'),
                pressure_events=sum(v for k, v in events.items() if k in ('high', 'max', 'oom', 'oom_kill')
                                    and isinstance(v, int)) if events else None)


def _capacity(conn, data_dir):
    scans = [dict(finished_at=r['finished_at'], **_memory(r['memory']))
             for r in _receipts(conn, 'ingest', fields=('finished_at', 'memory'))]
    email = (_receipts(conn, 'email', limit=1, fields=('finished_at', 'memory', 'spool')) or [{}])[0]
    spool = json.loads(email['spool']) if email.get('spool') else {}
    page_size, pages, free_pages = (conn.execute(f'PRAGMA {p}').fetchone()[0]
                                    for p in ('page_size', 'page_count', 'freelist_count'))
    path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
    wal = path.with_name(path.name + '-wal')
    disk = shutil.disk_usage(data_dir)
    return dict(scans=scans, headroom_target=HEADROOM_TARGET,
                email=dict(finished_at=email.get('finished_at'), **_memory(email.get('memory'))),
                spool=dict(bytes=spool.get('bytes'), count=spool.get('count'),
                           warn_bytes=SPOOL_WARN_BYTES, warn_count=SPOOL_WARN_COUNT),
                database=dict(bytes=page_size * pages, free_bytes=page_size * free_pages,
                              wal_bytes=wal.stat().st_size if wal.exists() else 0),
                disk=dict(free_bytes=disk.free, total_bytes=disk.total))


def _portals():
    """Feed providers and declared account limits, parsed again only when portals.yml changes."""
    path = ROOT / 'config' / 'portals.yml'
    stamp = (path.stat().st_mtime_ns, collection_policy.PATH.stat().st_mtime_ns)
    if _PORTALS.get('stamp') != stamp:
        import yaml
        config = yaml.safe_load(path.read_text()) or {}
        _PORTALS.update(stamp=stamp, quotas={k:v.get('account',{}) for k,v in collection_policy.load()['providers'].items() if k in ('adzuna','reed')},
                        caps={k:v['daily_limit'] for k,v in collection_policy.load()['providers'].items() if k in ('adzuna','reed')},
                        providers={f.get('name'): f.get('provider') for f in config.get('feeds') or []})
    return _PORTALS


def _quotas(conn, now):
    try:portals = _portals()
    except (OSError,ValueError):
        return dict(codex=allowances.summary(conn,now.isoformat()),feeds={},feed_state='unknown',spend='not recorded')
    today = now.astimezone(LONDON).date()
    # Every daily-scan receipt since the earlier of the month's start and seven days ago, however many
    # runs that period held (a primary-key range, bounded by time rather than a row cap that would
    # silently undercount reruns). One row per feed per run.
    since = (now - dt.timedelta(days=32)).date().isoformat()
    rows = conn.execute("SELECT json_extract(s.receipt_json, '$.started_at'), f.key, f.value "
                        "FROM scan_runs s, json_each(s.receipt_json, '$.feed_calls') f "
                        "WHERE s.run_id >= ? AND s.run_id < 'ingest;'", (f'ingest:{since}',)).fetchall()
    used: dict = {}
    for started, name, calls in rows:
        started = dt.datetime.fromisoformat(str(started).replace('Z', '+00:00')).astimezone(LONDON)
        provider = portals['providers'].get(name)
        if not provider or type(calls) is not int:
            continue
        total = used.setdefault(provider, dict(day=0, seven_days=0, month=0))
        total['day'] += calls if started.date() == today else 0
        total['seven_days'] += calls if (today - started.date()).days < 7 else 0
        total['month'] += calls if (started.year, started.month) == (today.year, today.month) else 0
    feeds = {provider: dict(used=used.get(provider, dict(day=0, seven_days=0, month=0)),
                            run_cap=portals['caps'].get(provider), account=portals['quotas'].get(provider) or {})
             for provider in sorted(set(portals['caps']) | set(used))}
    return dict(codex=allowances.summary(conn, now.isoformat()), feeds=feeds,
                spend='not recorded')


def _hunt(conn, now):
    since = (now - dt.timedelta(days=RECENT_DAYS)).isoformat(timespec='seconds')
    # The same rule as the all-time counts (ApproachHistory): corrected outcomes do not count.
    rows = conn.execute('SELECT id, record_id, kind, at, corrected_at FROM outcomes WHERE at >= ?', (since,))
    outcomes = ApproachHistory([], [dict(row) for row in rows]).counts
    statuses = dict(conn.execute(
        # CROSS JOIN keeps the indexed date range outermost (SQLite's documented join-order control);
        # driven from records by type, it walked every status event any job ever had.
        "SELECT e.to_value, COUNT(*) FROM state_events e CROSS JOIN records r ON r.id = e.entity_id "
        "WHERE e.recorded_at >= ? AND e.field = 'status' AND r.type = 'job' "
        f"AND e.to_value IN ({','.join('?' * len(JOB_STATUS_SHOWN))}) GROUP BY e.to_value",
        (since, *JOB_STATUS_SHOWN)).fetchall())
    return dict(days=RECENT_DAYS, outcomes=dict(outcomes), job_status=statuses,
                admissions_seven_days=len(allowances.ledger(conn, now)))
