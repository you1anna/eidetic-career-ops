"""Source health in words: each source's last read, each channel's measured health, and the
freshness line. Measured from receipts, never inferred from configuration alone.
"""
from __future__ import annotations

from pathlib import Path
import datetime as dt
import json



def freshness(conn, source_rows, expected_board_scopes=None):
    """When the data was last read: the latest daily ingest receipt, and the latest source success."""
    out={'last_success':max((r['last_success'] or '' for r in source_rows.values()),default='')}
    last=conn.execute("SELECT receipt_json,status FROM scan_runs WHERE run_id LIKE 'ingest:%' ORDER BY run_id DESC LIMIT 1").fetchone()
    if last:
        receipt=json.loads(last['receipt_json'])
        # Receipts before 25 Sep 2026 listed page-capped reads as failures; source health says which they were.
        capped=list(receipt.get('capped',[]))+[s for s in receipt.get('failures',[]) if (source_rows.get(s) or {}).get('state')=='partial']
        out.update(ingest_at=receipt.get('started_at',''),ingest_status=last['status'],
                   failures=[s for s in receipt.get('failures',[]) if s not in capped],capped=capped,
                   reused_pending=bool(receipt.get('reused_pending')),fetch=receipt.get('fetch') or {})
        if expected_board_scopes is not None:
            # Current warnings follow configured boards. The original run receipt remains intact.
            for field in ('failures', 'capped'):
                retired = [s for s in out[field] if s.startswith('board:') and s not in expected_board_scopes]
                out['retired_' + field] = retired
                out[field] = [s for s in out[field] if s not in retired]
    # The nightly backup (ops/vps/wo-backup.sh) leaves a receipt beside the database's backups.
    try:
        name=conn.execute('PRAGMA database_list').fetchone()[2]
        at=json.loads((Path(name).parent/'backups'/'last-backup.json').read_text()).get('at') if name else None
        if at: out['backup_at']=str(at)
    except (OSError,ValueError,AttributeError):
        pass
    return out


# What a classified fetch failure (tools/web_read.failure_kind) means for the owner, and what follows.
FAILURE_TEXT={
    'challenged':'blocked by a bot challenge (HTTP {status}). Retrying will not help; this source needs another channel such as an API, feed or email.',
    'refused':'access refused (HTTP {status}). Check whether the source still offers public access.',
    'disallowed':"the site's robots.txt disallows this read.",
    'not-found':'address not found (HTTP {status}); the source may have moved.',
    'rate-limited':'rate-limited by the source (HTTP {status}); the next run tries again.',
    'timeout':'timed out after {attempts} attempts; the next run tries again.',
    'network':'unreachable after {attempts} attempts; the next run tries again.',
    'server':'source server error (HTTP {status}) after {attempts} attempts; the next run tries again.',
}


_feed_policy_cache={}

def _configured_feed_counts():
    import yaml
    path=Path(__file__).resolve().parents[1]/'config/portals.yml'
    stamp=path.stat().st_mtime_ns
    if _feed_policy_cache.get('stamp')!=stamp:
        config=yaml.safe_load(path.read_text())
        counts={f"feed:{f['provider']}:{f['name']}":max(1,len(f.get('params',{}).get('phrases',[]))) for f in config.get('feeds',[]) if f.get('enabled')}
        _feed_policy_cache.update(stamp=stamp,counts=counts)
    return _feed_policy_cache['counts']

def _expected_age(sources):
    """Operational read cadence, separate from the unchanged 48-hour vacancy evidence rule."""
    try:
        import collection_policy
        import math
        policy=collection_policy.load();providers=policy['providers']
        counts={}
        feed_counts=_configured_feed_counts()
        for source in sources:counts[source.get('provider')]=counts.get(source.get('provider'),0)+feed_counts.get(source.get('scope'),1)
        days=2
        for provider,count in counts.items():
            p=providers.get(provider)
            if p:
                days=max(days,p.get('refresh_days',0)+1 if p.get('refresh_days') else
                         math.ceil(count/max(1,min(p['scheduled_target'],p.get('full_discovery_limit',p['scheduled_target']))))+2)
                if provider in ('hn_algolia','contracts_finder','find_a_tender','uktn'):
                    weekdays=sorted(collection_policy.DAYS.index(d) for d in policy['background_days'])
                    gap=max(b-a for a,b in zip(weekdays,weekdays[1:]+[weekdays[0]+7]))
                    days=max(days,gap+1)
        return days
    except (OSError,ValueError):return None


def channel_health(conn, channel, source_rows, today, expected_board_scopes=None):
    """Current availability plus measured read history. A disabled source cannot be shown working
    merely because its last retained receipt succeeded."""
    m=channel.get('measure') or {}
    prefix,providers=m.get('scope_prefix'),m.get('provider_in') or []
    matches=[v for k,v in source_rows.items() if (prefix and k.startswith(prefix)) or v.get('provider') in providers]
    # Yield belongs to source history even when a board moves or is disabled. Read health
    # belongs only to today's configured boards, with missing receipts represented explicitly.
    scopes=[v['scope'] for v in matches]
    retired, unread = [], []
    if expected_board_scopes is not None:
        retired = [v for v in matches if v['scope'] not in expected_board_scopes]
        matches = [v for v in matches if v['scope'] in expected_board_scopes]
        unread = sorted(set(expected_board_scopes) - {v['scope'] for v in matches})
    def made(since=None):
        if not scopes: return None
        q='SELECT count(DISTINCT s.record_id) FROM source_records s JOIN records r ON r.id=s.record_id WHERE r.archived_at IS NULL AND s.source_scope IN ('+','.join('?'*len(scopes))+')'
        args=list(scopes)
        if since: q+=' AND r.created_at>=?';args.append(since.isoformat())
        return conn.execute(q,args).fetchone()[0]
    last_read=max((v.get('observed_at') or '' for v in matches),default='')
    runs=channel.get('runs')
    if runs=='not-built': health='not_built'
    elif runs=='off': health='off'
    elif expected_board_scopes is not None and not expected_board_scopes: health='off'
    elif matches:
        cadence=_expected_age(matches)
        if any(v.get('problem') for v in matches): health='failing'
        elif unread: health='partial'
        elif cadence is None: health='unknown'
        elif runs in ('daily', 'every 15 minutes') and any(not v.get('observed_at') or (today-dt.date.fromisoformat(v['observed_at'][:10])).days>cadence for v in matches): health='stale'
        elif any(v.get('state')=='partial' for v in matches): health='partial'
        elif all(v.get('state')=='empty' for v in matches): health='empty'
        else: health='working'
    elif channel.get('method')=='derived': health='derived'
    else: health={'off':'off','attended':'attended'}.get(runs,'no_reads')
    missing = [dict(scope=scope, provider=scope.split(':')[1], label=expected_board_scopes[scope], state='no_reads',
                    problem=False, observed_at='', last_success='', item_count=0,
                    summary='Configured board has not been read yet.') for scope in unread]
    result = dict(health=health,sources=matches + missing,last_read=last_read,
                last_success=max((v.get('last_success') or '' for v in matches),default=''),
                observed_count=sum(v.get('item_count') or 0 for v in matches),count=made(),
                new_7=made(today-dt.timedelta(days=7)),new_30=made(today-dt.timedelta(days=30)))
    if expected_board_scopes is not None:
        result.update(retired_sources=retired, unread_scopes=unread)
    return result


def coverage_limit(detail):
    """The scanner's retained stop reason; older partial receipts mean a page cap."""
    return 'request' if detail.get('stop_reason') == 'budget' else 'page'


def describe_source(row):
    """A source's last read in words, for the freshness line and CLI coverage. `detail` is the scanner's own JSON; only an
    error is a problem. A partial read stopped at a page/request limit, which the
    ingest receipt already counts as capped, not failed."""
    try:
        detail=json.loads(row.get('detail') or '{}')
    except ValueError:
        detail={'error':row.get('detail')}
    if not isinstance(detail,dict): detail={'error':str(detail)}
    scope=row['scope']
    row['label']=detail.get('name') or (scope.split(':',2)[2] if scope.count(':')>=2 else scope)
    when=(row.get('observed_at') or '')[:10] or 'an unknown date'
    count=row.get('item_count') or 0
    if detail.get('kind') in FAILURE_TEXT:
        row['problem'],row['summary']=True,f"failed on {when}: "+FAILURE_TEXT[detail['kind']].format(
            status=detail.get('status') or '?',attempts=detail.get('attempts') or 1)
    elif detail.get('error'):
        row['problem'],row['summary']=True,f"failed on {when}: {detail['error']}"
    elif row.get('state')=='partial':
        row['problem'],row['summary']=False,f"read {count} items on {when}, then stopped at the {coverage_limit(detail)} limit (a partial read, not a failure)"
    elif row.get('state') in ('ok','empty'):
        row['problem'],row['summary']=False,f"read {count} items on {when}"
    else:
        row['problem'],row['summary']=True,f"state {row.get('state') or 'unknown'} on {when}"
    return row
