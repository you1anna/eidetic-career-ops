"""Pure historical approach state, derived once per coherent workspace snapshot.

Selection retains the original timestamp-string/id ordering and stable tie behavior.
Indexes hold references to the newest event and oldest recorded route per scope; they
never copy or discard the underlying history. No database, configuration or shared cache.
"""
from collections import Counter

# A recorded outcome moves the approach to this stage. Corrected entries never reach this mapping.
STAGE = {'sent': 'sent', 'followed_up': 'followed_up', 'replied': 'replied', 'introduction': 'replied',
         'referral': 'replied', 'submitted': 'submitted', 'conversation': 'conversation', 'proposal': 'proposal',
         'won': 'won', 'no_fit': 'held', 'no_budget': 'held', 'permanent_only': 'held',
         'no_response': 'no_response', 'handed_over': 'handed_over'}
RECOMMENDATION = {
    'sent': 'The approach was sent. Use any response or changed evidence to decide the next move.',
    'followed_up': 'You recorded a follow-up. Use any reply or agreed next step to decide what happens next.',
    'replied': 'Use the recorded response to qualify the buyer, need and engagement before preparing another approach.',
    'submitted': 'Your CV is with the desk. Use their feedback to decide the next step.',
    'conversation': 'Prepare around the recognised problem and record the commercial next step from the conversation.',
    'proposal': 'Use the agreed scope and buying criteria to assess the proposal decision.',
    'won': 'The engagement is won; use delivery evidence and agreed outcomes for the next decision.',
    'held': 'Keep this approach held; the recorded outcome changes the earlier recommendation.',
    'no_response': 'You recorded no response. A later reply can still be recorded.',
    'handed_over': 'This conversation continues as an opportunity in Work.'}


def latest_drafts(decisions):
    """Newest draft decision per record, including pending/withdrawn decisions.

    Equal IDs preserve the first input, just like a stable descending sort or max.
    """
    result = {}
    for decision in decisions:
        if decision.get('kind') != 'draft':
            continue
        rid = decision.get('record_id')
        previous = result.get(rid)
        if previous is None or decision.get('id', 0) > previous.get('id', 0):
            result[rid] = decision
    return result


class ApproachHistory:
    """O(decisions + outcomes) construction; O(1) lookup for each approach.

    A scope is (record ID, action ID), with None for legacy record-only outcomes.
    Input position preserves both newest-first and reversed oldest-first tie rules.
    Instances and referenced snapshot rows are read-only after construction.
    """
    def __init__(self, decisions, outcomes):
        self.decisions = latest_drafts(decisions)
        self.counts = Counter()
        self._latest, self._routes = {}, {}
        for position, outcome in enumerate(outcomes):
            if outcome.get('corrected_at'):
                continue
            kind = outcome.get('kind')
            if kind:
                self.counts[kind] += 1
            snapshot = outcome.get('snapshot') or {}
            action = snapshot.get('action_id')
            scope = (outcome.get('record_id'), action or None)
            order = (outcome.get('at') or '', outcome.get('id', 0), -position)
            previous = self._latest.get(scope)
            if previous is None or order > previous[0]:
                self._latest[scope] = (order, outcome)
            route = (snapshot.get('brief') or {}).get('approach_route')
            if action and route:
                previous = self._routes.get(scope)
                if previous is None or order < previous[0]:
                    self._routes[scope] = (order, route)

    def latest(self, record_id, action_id):
        legacy = self._latest.get((record_id, None))
        scoped = self._latest.get((record_id, action_id))
        if legacy is None:
            return scoped[1] if scoped is not None else None
        if scoped is None:
            return legacy[1]
        return max(legacy, scoped, key=lambda entry: entry[0])[1]

    def original_route(self, record_id, action_id):
        entry = self._routes.get((record_id, action_id))
        return entry[1] if entry is not None else None

    def apply(self, item, record):
        needs_research = item['stage'] == 'research'
        # Keep the owner's explicit historical hold, without resurrecting approval/copy workflow.
        decision = self.decisions.get(record['id'])
        if decision and decision.get('status') in ('answered', 'done') and decision.get('answer') in ('hold', 'drop', 'not_yet'):
            item['stage'] = 'held'
            item['readiness'] = dict(label='Held', reason='The owner chose to hold or drop this approach.')
            item['brief']['recommendation'] = 'Keep this approach on hold unless the owner changes the decision or new evidence changes the opportunity.'
        # New outcomes name an exact approach. Older record-only events retain their conservative
        # scope because choosing a company for them now would invent missing historical context.
        latest = self.latest(record['id'], item['id'])
        if latest is not None:
            outcome = dict(latest)
            stage = STAGE.get(outcome.get('kind'))
            if stage:
                item['stage'], item['last_outcome'] = stage, outcome
                item['readiness'] = dict(label=stage.replace('_', ' ').capitalize(), reason=outcome.get('note') or f"Recorded {outcome['kind'].replace('_', ' ')} outcome.")
                if not (outcome.get('snapshot') or {}).get('action_id'):
                    item['readiness']['reason'] += ' This is a legacy record-level outcome; its specific approach was not recorded.'
                item['brief']['recommendation'] = RECOMMENDATION[stage]
                original = self.original_route(record['id'], item['id'])
                if original:
                    item['brief']['approach_route'] = dict(original, recorded=True)
        elif record.get('type') == 'opportunity' and record.get('stage') in ('conversation', 'proposal', 'won'):
            item['stage'] = record['stage']
            item['readiness'] = dict(label=record['stage'].capitalize(), reason='The recorded commercial stage is already beyond a first approach.')
            item['brief']['recommendation'] = 'Use the recorded commercial stage and agreed next step to prepare the next decision; do not repeat the first approach.'
            needs_research = False
        if needs_research and not item.get('last_outcome') and item['stage'] != 'held':
            item['stage'] = 'research'
            item['readiness'] = dict(label='Research needed', reason='A saved note cannot replace current evidence and a verified recipient.')
        return item
