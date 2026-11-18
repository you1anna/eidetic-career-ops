"""Automatic opportunity admission, prepared without the write lock and committed atomically.

Ranking stays in the existing relevance/intelligence/work-selection owners. The bounded meta
ledger is only a rolling allowance counter; records, state events and import reports retain history.
"""
import json

import approaches
import decision_intelligence
import evidence_checks
import record_views
import records
import relevance
import work_selection

from allowances import KEY, instant, ledger  # the read side, shared with the Dashboard


class RetrySnapshot(Exception):
    """A writer changed the inputs while expensive selection ran; prepare again outside the lock."""


def prepare(conn, candidates, now, *, comparison=False):
    version = conn.execute('PRAGMA data_version').fetchone()[0]
    with record_views.snapshot(conn):
        state = records.state_context(conn)
        policy = work_selection.capacity_policy(relevance.load_config())
        existing = {r['id'] for r in state['records']}
        identities = {(r['source_scope'], str(r['source_id'])) for r in conn.execute('SELECT source_scope,source_id FROM source_records')}
        urls = records.job_url_index(conn)
        entries = ledger(conn, now)
    fresh, keys = [], {}
    for candidate in candidates:
        row = candidate['record']
        if row['id'] in existing or candidate.get('identity') in identities:
            continue
        if row['type'] == 'job':
            try:
                if records.match_job_url(conn, row.get('url', ''), urls):
                    continue
            except (ValueError, records.Conflict):
                continue  # Canonical import reports conflicts and preserves archived identities.
        if row['id'] in keys:
            continue
        row['availability'] = evidence_checks.advert_availability(row, now=now)
        fresh.append(row)
        keys[row['id']] = candidate['key']
    combined = dict(state, records=[*state['records'], *fresh], discovery_capacity=policy)
    # Copy records before attaching new derived scores; do not mutate a caller's cached snapshot.
    combined['records'] = [dict(r) for r in combined['records']]
    sightings = {r['id']: (r.get('relevance') or {}).get('last_seen') for r in state['records']
                 if (r.get('relevance') or {}).get('last_seen')}
    sightings.update({r['id']: r['advert']['observed_at'] for r in fresh if r.get('advert', {}).get('observed_at')})
    scores = relevance.score(dict(combined, sightings=sightings), today=now.date())
    for row in combined['records']:
        row['relevance'] = scores.get(row['id'])
    combined['intelligence'] = approaches.annotate(
        decision_intelligence.build(combined, today=now.date()), combined, now.date())
    projection = work_selection.build(combined, today=now.date(), include_order=True) if comparison else work_selection.build(combined, today=now.date())
    selected = [*projection['discovery'], *(r for r in projection['other'] if r['rule'] in work_selection.QUALIFY)]
    # Research candidates can be retained before winning a visible working place.
    selected_ids = {ref['record_id'] for ref in selected}
    selected += [dict(record_id=rid) for rid in projection.get('research_pool', []) if rid not in selected_ids]
    remaining = max(0, policy['additions_per_seven_days'] - len(entries))
    selected_new = [ref['record_id'] for ref in selected if ref['record_id'] in keys]
    allowed = {keys[rid] for rid in selected_new[:remaining]}
    reasons = {key: 'working_limit' for key in keys.values() if key not in allowed}
    for rid in selected_new[remaining:]:
        reasons[keys[rid]] = 'seven_day_limit'
    for ref in projection['other']:
        if ref['record_id'] in keys and ref['rule'] == 'other.capacity_stale':
            reasons[keys[ref['record_id']]] = 'unresolved_stale'
        elif ref['record_id'] in keys and ref['rule'] not in (*work_selection.QUALIFY, 'other.capacity'):
            reasons[keys[ref['record_id']]] = 'not_recommended'
    budget = Budget(version, now, policy, allowed, reasons, [ref['record_id'] for ref in selected])
    if comparison:
        budget.comparison = _comparison(state, combined, projection, keys, reasons, remaining)
    return budget


def _comparison(state, combined, projection, keys, reasons, remaining):
    by_id = {row['id']: row for row in combined['records']}
    refs = {ref['record_id']: ref for group in ('discovery','other','active','shortlisted','closed')
            for ref in projection[group]}
    protected = {ref['record_id'] for group in ('active','shortlisted')
                 for ref in (state.get('opportunities') or {}).get(group, [])}
    maintained = {rid for rid in (state.get('opportunities') or {}).get('research_ids', [])
                  if rid not in protected and by_id.get(rid, {}).get('type') == 'job'
                  and (by_id[rid].get('research_disposition') or {}).get('scope') != 'needs_input'}
    current_refs = {ref['record_id']:ref for group in ('discovery','other','active','shortlisted','closed')
                    for ref in (state.get('opportunities') or {}).get(group, [])}
    def row(rid):
        record, ref = by_id[rid], current_refs.get(rid, refs[rid]) if rid in maintained else refs[rid]
        rel = record.get('relevance') or {}
        qualification = rel.get('qualification') or {}
        concerns = qualification.get('conflicts') or (rel.get('fit') or {}).get('gaps') or qualification.get('reasons') or []
        return dict(record_id=rid if rid not in keys else None, candidate_id=rid,
                    name=record['name'], organisation=record.get('organisation',''), url=record.get('url',''),
                    score=rel.get('score'), band=rel.get('band','unknown'), rule=ref['rule'],
                    reason=concerns[0] if concerns else ref['reason'], uncertainty=ref.get('uncertainty',''),
                    terms=' · '.join(record.get(k) or label for k,label in (('location','Location unknown'),('engagement','Engagement unknown'),('pay','Pay unknown'))),
                    deferral=reasons.get(keys.get(rid),''), source=ref.get('source'))
    ranked = projection['ranked_ids']
    deferred = [rid for rid in ranked if rid in keys and keys[rid] in reasons
                and not refs[rid]['rule'].startswith('closed.')
                and refs[rid]['rule'] not in ('other.excluded','other.duplicate','other.existing_work')]
    # Show capacity and policy exclusions separately; a higher score alone cannot override a rule.
    capacity = [rid for rid in deferred if reasons[keys[rid]] in ('working_limit','seven_day_limit')][:3]
    policy = [rid for rid in deferred if reasons[keys[rid]] not in ('working_limit','seven_day_limit')][:3]
    return dict(excluded=[row(rid) for rid in [*capacity,*policy]],
                maintained=[row(rid) for rid in reversed(ranked) if rid in maintained][:3],
                allowance_remaining=remaining, assessed_new=len(keys), excluded_total=len(deferred))


class Budget:
    def __init__(self, version, now, policy, allowed, reasons, selected):
        self.version, self.now, self.policy = version, now, policy
        self.allowed, self.reasons, self.selected = allowed, reasons, selected
        self.admitted, self.deferred, self.entries = [], {}, []

    def start(self, conn):
        if conn.execute('PRAGMA data_version').fetchone()[0] != self.version:
            raise RetrySnapshot()
        self.entries = ledger(conn, self.now)

    def permit(self, key):
        reason = self.reasons.get(key, 'working_limit') if key not in self.allowed else ''
        if not reason and len(self.entries) + len(self.admitted) >= self.policy['additions_per_seven_days']:
            reason = 'seven_day_limit'
        if reason:
            self.deferred[reason] = self.deferred.get(reason, 0) + 1
        return not reason

    def added(self, rid):
        self.admitted.append(rid)

    def finish(self, conn):
        entries = self.entries + [dict(id=rid, at=self.now.isoformat()) for rid in self.admitted]
        if entries or conn.execute('SELECT 1 FROM meta WHERE key=?', (KEY,)).fetchone():
            encoded = json.dumps(entries, sort_keys=True)
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value '
                         'WHERE value<>excluded.value', (KEY, encoded))
        return dict(working_limit=self.policy['working_limit'], additions_per_seven_days=self.policy['additions_per_seven_days'],
                    admitted=self.admitted, allowance_remaining=max(0,self.policy['additions_per_seven_days']-len(entries)),
                    selected=self.selected, deferred=self.deferred)
