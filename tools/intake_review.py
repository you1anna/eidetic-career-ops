"""On-demand admission comparison, using the importer's canonical preparation without writes.

Pending observations are streamed. Retain at most 2,000 eligible candidates, disclose overflow,
and serialize scoring without queuing expensive work. Never used by ordinary state/status reads.
"""
import db
from pathlib import Path
import threading

import discovery_intake
import email_leads
import email_sources
import observation_reader
import record_imports
import record_views
import records
import normalise
import workspace_state

_LOCK = threading.Lock()
MAX_CANDIDATES = 2000


def compare(conn, data_dir, *, pending=None):
    if not _LOCK.acquire(blocking=False):
        raise ValueError('An intake comparison is already running. Retry shortly.')
    building = workspace_state._BUILD_LOCK.acquire(blocking=False)
    try:
        if not building:
            raise ValueError('The workspace is rebuilding. Retry the comparison shortly.')
        # Share the existing server build guard: two expensive projections must not overlap.
        saved = {(r[0], str(r[1])) for r in conn.execute('SELECT source_scope,source_id FROM source_records')}
        saved_urls = records.job_url_index(conn)
        folder = Path(pending) if pending is not None else Path(data_dir) / 'pending'
        items, eligible = {}, 0
        def visit(key, item):
            nonlocal eligible
            if not isinstance(item, dict) or item.get('kind') not in ('job','tender'):
                return
            if item.get('compacted_at') or not record_imports.selected_candidate(item, set(), set()):
                return
            if (item.get('source_scope'), str(item.get('source_id'))) in saved:
                return
            if item.get('kind') == 'job':
                try:
                    if normalise.job_url((item.get('current') or {}).get('url', '')) in saved_urls:
                        return
                except ValueError:
                    return
            eligible += 1
            if len(items) < MAX_CANDIDATES:
                current = dict(item.get('current') or {})
                for field in ('text','summary','email_summary'):
                    if field in current: current[field] = str(current[field])[:16001]
                items[key] = {field:item[field] for field in ('kind','source_scope','source_id','last_seen_at','first_seen_at','source_lifecycle') if field in item}
                items[key]['current'] = current
        try:
            with (folder / 'observations.json').open(encoding='utf-8') as stream:
                observation_reader.read(stream, sources_only=True, visit_item=visit)
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError('Intake candidates could not be read. Retry; retained evidence is unchanged.') from None
        terms = record_imports.advert_terms(dict(items=items))
        admission = record_imports.intake_decisions(items, terms, candidate_urls=set())
        candidates = record_imports.capacity_candidates(folder, (items, {}, []), terms, admission, [], {}, set())
        now = discovery_intake.instant(db.now())
        with record_views.snapshot(conn):
            budget = discovery_intake.prepare(conn, candidates, now, comparison=True)
        result = budget.comparison
        identities = {candidate['record']['id']: candidate['identity'] for candidate in candidates}
        for row in result['excluded']:
            scope, sid = identities[row['candidate_id']]
            if email_sources.is_alert(scope):
                row['review_url'] = '/#dashboard/lead/' + email_leads.lead_id(dict(source_scope=scope, source_id=sid))
        return dict(result, state='ok', generated_at=now.isoformat(), complete=eligible <= MAX_CANDIDATES,
                    eligible_observations=eligible, candidate_limit=MAX_CANDIDATES,
                    scope='Compares current adverts available to automatic intake. Earlier imported files and adverts rejected by the initial suitability filters are outside this comparison.')
    finally:
        if building: workspace_state._BUILD_LOCK.release()
        _LOCK.release()
