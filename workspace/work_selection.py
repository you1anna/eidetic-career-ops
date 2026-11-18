"""Pure work admission and ordering over the coherent, already-qualified workspace snapshot.

References point to records and existing assessments. No database, clock, network, generated copy
or saved-week membership participates in this projection; relevance owns qualification and score,
and intelligence owns the assessment of a current sourced company remit.

Each work record is placed by the first matching rule in `RULES`, checked in the declared order.
Every reference carries that rule's code, and each active or shortlisted reference carries the
`ATTENTION` cause that chose its weekly section, so the API and `wo approach` show why an item is
where it is without re-deriving it.
"""
from __future__ import annotations

from typing import NamedTuple

import evidence_checks
import email_sources
import machines


class Rule(NamedTuple):
    group: str
    text: str  # what the rule means; also the reason shown where no score or recorded step applies


# Placement rules in the order `_place` checks them. The first match decides the group.
# docs/architecture.md#placement-rules lists them in this order; a test keeps the two in step.
RULES = {
    'closed.recorded': Rule('closed', 'Recorded closed or skipped.'),
    'active.process': Rule('active', 'Continue the recorded process.'),
    'shortlisted.job': Rule('shortlisted', 'A role you shortlisted.'),
    'closed.source': Rule('closed', 'The public source records this advert as closed or unavailable.'),
    'closed.check': Rule('closed', 'Current advert evidence confirms that applications are closed.'),
    'closed.outcome': Rule('closed', 'Recorded outcome closes this approach.'),
    'other.excluded': Rule('other', 'Excluded; kept for reference.'),
    'other.unsuitable': Rule('other', 'Outside your software and delivery work; kept for reference.'),
    'other.duplicate': Rule('other', 'A copy of another record, which is ranked instead.'),
    'other.existing_work': Rule('other', 'A copy of work already active, shortlisted or closed in Jobs.'),
    'discovery.advert': Rule('discovery', 'A strong, current advert with no conflicting terms.'),
    'other.advert': Rule('other', 'Kept in the catalogue; not currently recommended.'),
    'other.unchecked_advert': Rule('other', 'Promising, but its listing decides availability, so research '
                                            'checks it only once you shortlist it.'),
    'qualify.advert': Rule('other', 'A promising advert research can read; its fit needs evidence first.'),
    'discovery.lead': Rule('discovery', 'A direct lead with a current, specific sourced remit.'),
    'qualify.lead': Rule('other', 'A promising direct lead; research must establish a current need '
                                  'or an essential requirement.'),
    'other.lead': Rule('other', 'A recorded lead without a current, specific need; kept for reference.'),
    'other.capacity': Rule('other', 'Outside the working list; stronger current opportunities take priority. Kept for reference.'),
    'other.capacity_stale': Rule('other', 'Unresolved for more than two weeks; kept for reference until stronger evidence arrives.'),
    'other.context': Rule('other', 'Company context; a current specific remit is not established.'),
}
# Research qualifies these through its reserved places. Each one is a record research can read.
QUALIFY = ('qualify.advert', 'qualify.lead')
SECTIONS = ('act_now', 'waiting', 'in_progress')
RESEARCH_PLACES = 10    # discovery-side research targets per pass, beyond active/shortlisted work
QUALIFY_PLACES = 6      # of those, reserved for qualification while candidates wait
CHECKING_SHOWN = 6      # Needs checking: research's next qualification targets
# This week's sections in page order. `js/week.js` renders them; a test pins its titles to these.
WEEK_PAGE = (('act_now', 'Act now'), ('best', 'Best opportunities'), ('needs_checking', 'Needs checking'),
             ('in_progress', 'In progress'), ('waiting', 'Waiting'))


# Why active or shortlisted work sits in its weekly section. The declaration lives in machines.
Attention = machines.Attention
ATTENTION = machines.ATTENTION

LANES = {'contract': 0, 'direct': 1, 'permanent': 2, 'unstated': 3}
ACTIVE_JOBS = set(machines.ACTIVE_JOBS)
CLOSED = {'skip', 'closed', 'rejected', 'lost', 'declined', 'no_relevant_desk'}
GONE = set(evidence_checks.GONE_LIFECYCLES)
ACTIVE_PROGRESS = set(machines.ACTIVE)
TERMINAL_PROGRESS = set(machines.TERMINAL)
ADVERTS = evidence_checks.ADVERT_TYPES


def _groups_of_work(records):
    """Index already-confirmed identity without merging records or copying their processes."""
    parents = {rid: rid for rid in records}

    def root(rid):
        while parents[rid] != rid:
            parents[rid] = parents[parents[rid]]
            rid = parents[rid]
        return rid

    for rid, record in records.items():
        other = (record.get('relevance') or {}).get('duplicate_of')
        if other in records:
            a, b = root(rid), root(other)
            if a != b:
                parents[a] = b
    return {rid: root(rid) for rid in records}


def _score_reason(record, action):
    reasons = (record.get('relevance') or {}).get('reasons') or []
    positive = [reason for reason in reasons if reason.get('weight', 0) > 0 and reason.get('text')]
    chosen = max(positive, key=lambda reason: (bool(reason.get('fit')), reason['weight']), default=None)
    return chosen['text'] if chosen else ((action.get('brief') or {}).get('why_now')
                                         or record.get('next_action') or 'Review the available work evidence.')


def _source(record, action):
    context = record.get('source_context') or {}
    source = context.get('source') or (record.get('availability') or {}).get('source') or ''
    if source:
        parts = source.split(':')
        labels = {'feed:adzuna': 'Adzuna', 'feed:reed': 'Reed'}
        label = email_sources.label(source) if email_sources.is_alert(source) else labels.get(':'.join(parts[:2]))
        if not label:
            label = f"Employer's {parts[1].capitalize()} board" if parts[0] == 'board' and len(parts) > 1 else 'Source listing'
        return dict(label=label, url=context.get('url') or record.get('url') or '',
                    at=context.get('at') or (record.get('availability') or {}).get('at'),
                    basis=context.get('basis') or 'listing')
    facts = (action.get('brief') or {}).get('facts') or []
    fact = next((f for f in facts if isinstance(f, dict) and f.get('url')), {})
    if fact or record.get('url'):
        return dict(label='Research evidence' if fact else 'Saved source', url=fact.get('url') or record['url'],
                    at=fact.get('date'), basis='research' if fact else 'saved')
    return None


def _uncertainty(record, action):
    rel = record.get('relevance') or {}
    unknowns = (action.get('brief') or {}).get('unknowns') or []
    if (record['type'] not in ADVERTS and (action.get('work_evidence') or {}).get('specific') is False
            and unknowns):
        return str(unknowns[0])
    gaps = (rel.get('fit') or {}).get('gaps') or (rel.get('qualification') or {}).get('conflicts') or []
    if gaps:
        return str(gaps[0])
    if record['type'] in ADVERTS:
        # Use the job's own decisive unknown, not an employer's consulting question. Optional
        # experience remains labelled optional/unspecified and never changes admission here.
        unknowns = (record.get('briefing') or {}).get('decisive_unknowns') or []
        if unknowns:
            return str(unknowns[0])
        unknowns = [u for u in (rel.get('fit') or {}).get('optional_unknowns') or []
                    if not u.startswith('General competency requirement')]
        if unknowns:
            return str(unknowns[0])
    if record['type'] in ADVERTS and not _worth_a_look(record):
        return 'Advert availability is not confirmed.'
    unknowns = (action.get('brief') or {}).get('unknowns') or []
    if unknowns:
        return str(unknowns[0])
    if (rel.get('fit') or {}).get('status') == 'unconfirmed':
        return 'The available evidence does not establish the remit and requirements.'
    return ''


def _attention(record, action, shortlisted=False):
    """(ATTENTION cause, rank within its section) from recorded responsibility and stage."""
    progress = action.get('progress') or {}
    status = progress.get('status') or record.get('status') or record.get('stage')
    step = progress.get('next_step') or {}
    if progress.get('reply_to_record'):
        return 'reply', 1
    if step.get('kind') == 'waiting':
        return 'their_step', 0
    if step.get('kind') == 'mine':
        return 'your_step', 1
    if (record.get('incoming') or {}).get('current'):
        return 'incoming', 0
    if status in machines.STAGE_RANK:
        return 'stage', machines.STAGE_RANK[status]
    if shortlisted:
        return 'shortlisted', 5
    if status in machines.AWAITING:
        return 'submitted', 0
    return 'unassigned', 0


def _commitment(action):
    """A current saved work commitment survives changes to its recommendation."""
    progress = action.get('progress') or {}
    return ((progress.get('next_step') or {}).get('kind') in ('mine', 'waiting')
            and not progress.get('terminal_recorded')
            and progress.get('status') not in ('closed', 'skipped', 'won', 'handed_over'))


def _active_work(action):
    progress = action.get('progress') or {}
    return (progress.get('status') in ACTIVE_PROGRESS or _commitment(action)
            or (progress.get('status') == 'no_response' and not progress.get('terminal_recorded')))


def _lane(record):
    engagement = ((record.get('relevance') or {}).get('qualification') or {}).get('engagement')
    if not engagement:
        engagement = record.get('engagement')
    if engagement in ('contract', 'interim', 'ftc', 'fractional', 'freelance', 'procurement') or record['type'] == 'tender':
        return 'contract'
    if engagement == 'permanent':
        return 'permanent'
    if record['type'] in ('company', 'agency', 'opportunity'):
        return 'direct'
    return 'unstated'


def _worth_a_look(record):
    """An advert joins Worth a look only while it still looks open; leads are not adverts."""
    if record['type'] not in ADVERTS:
        return True
    return (record.get('availability') or {}).get('status') == 'open'


def _researchable(record):
    """Discovery research goes where a read can add evidence. A feed advert's daily listing already
    decides its availability and its aggregator page refuses reads, so it is researched only once
    shortlisted. A board advert is researched while open; an advert no scanner lists has only research."""
    if record['type'] not in ADVERTS:
        return True
    availability = record.get('availability') or {}
    source = availability.get('source')
    if not source:
        return True
    return not source.startswith('feed:') and availability.get('status') == 'open'


def _primary_action(rid, items):
    return next((a for a in items if a.get('record_id') == rid), items[0] if items else {})


def _promising_advert(record, eligible):
    """A plausible alert or captured advert whose remit or requirements need evidence."""
    rel = record.get('relevance') or {}
    return (record['type'] in ADVERTS
            and (email_sources.is_alert(record.get('source_file')) or (rel.get('fit') or {}).get('gaps')
                 or (rel.get('fit') or {}).get('status') == 'unconfirmed')
            and rel.get('band') in ('possible', 'strong')
            and (eligible is None or record['id'] in eligible))


def _place(record, action, items, eligible, blocked, retained):
    """Return (rule, reason, action) for the first matching rule, in `RULES` order.

    Replies and explicit commitments survive public closure; a shortlist or elapsed application
    wait does not. An excluded record stays available in the catalogue/history.
    """
    rid, kind = record['id'], record['type']
    rel = record.get('relevance') or {}
    q = rel.get('qualification') or {}
    progress = action.get('progress') or {}
    status, stage = record.get('status'), record.get('stage')
    # Personal contact events are not company work. Only company-owned or explicit commercial
    # stages can retain a company's active process after its original signal changes.
    work_action = next((a for a in items if a.get('record_id') == rid and _active_work(a)), None)
    if status in CLOSED or stage in CLOSED or stage == 'won' or progress.get('terminal_recorded'):
        return 'closed.recorded', progress.get('next') or RULES['closed.recorded'].text, action
    source_closed = record.get('source_lifecycle') in GONE
    check_closed = (((record.get('evidence_checks') or {}).get('vacancy') or {}).get('status') == 'closed'
                    or (record.get('availability') or {}).get('status') == 'closed')
    # Source closure does not imply rejection. Keep genuine hiring activity, but do not let the
    # automatic follow-up clock turn an unanswered application into a new weekly priority.
    ongoing = (status in {'responded', 'interview', 'offer'}
               or any(a.get('record_id') == rid and (
                   _commitment(a) or (a.get('progress') or {}).get('reply_to_record')
                   or (a.get('progress') or {}).get('status') in {'replied', 'conversation', 'proposal', 'interview', 'offer'})
                   for a in items))
    available_process = kind not in ADVERTS or not (source_closed or check_closed) or ongoing
    if available_process and (status in ACTIVE_JOBS or (kind == 'opportunity' and stage in ('contacted', 'conversation', 'proposal'))
                              or work_action):
        if work_action:
            action, progress = work_action, work_action.get('progress') or {}
        reason = progress.get('next') or record.get('next_action') or RULES['active.process'].text
        if _commitment(action) and not progress.get('reply_to_record'):
            step = progress['next_step']
            reason = ('Waiting for them: ' if step['kind'] == 'waiting' else '') + step['text']
        return 'active.process', reason, action
    if kind == 'job' and status == 'pursue' and available_process:
        return 'shortlisted.job', progress.get('next') or RULES['shortlisted.job'].text, action
    if source_closed:
        return 'closed.source', RULES['closed.source'].text, action
    if check_closed:
        return 'closed.check', RULES['closed.check'].text, action
    if progress.get('status') in TERMINAL_PROGRESS:
        return 'closed.outcome', progress.get('next') or RULES['closed.outcome'].text, action
    rule = ('other.excluded' if blocked else
            'other.unsuitable' if q.get('eligible') is False or q.get('domain') == 'outside' else
            'other.duplicate' if rel.get('duplicate_of') else
            'other.existing_work' if retained else None)
    if rule:
        return rule, RULES[rule].text, action
    if kind in ADVERTS:
        # The assessment's research section distinguishes stale/conflicting adverts from usable
        # ones. Unknown terms stay unknown; they do not prevent a strong remit being considered.
        usable = (eligible is None or rid in eligible) and action.get('stage') != 'research' and not q.get('conflicts')
        if rel.get('band') == 'strong' and usable:
            return 'discovery.advert', _score_reason(record, action), action
        if not _promising_advert(record, eligible):
            return 'other.advert', RULES['other.advert'].text, action
        # Qualification is research's job, so a candidate research will not read is held for
        # the owner's shortlist instead of waiting in Needs checking for an attempt that never comes.
        if not _researchable(record):
            return 'other.unchecked_advert', RULES['other.unchecked_advert'].text, action
        return 'qualify.advert', _score_reason(record, action), action
    specific = (action.get('work_evidence') or {}).get('specific')
    if kind == 'opportunity' or specific:
        # Every direct lead needs a current, specific sourced remit to be recommended; a recorded
        # opportunity is not one on its type alone. Essential gaps withhold the recommendation, and
        # a promising lead without a remit is researched for one.
        gaps = (rel.get('fit') or {}).get('gaps')
        if specific and not gaps:
            return 'discovery.lead', _score_reason(record, action), action
        if gaps or rel.get('band') in ('possible', 'strong'):
            return 'qualify.lead', _score_reason(record, action), action
        return 'other.lead', RULES['other.lead'].text, action
    return 'other.context', RULES['other.context'].text, action


def capacity_policy(config):
    """One validated contract for import and read-time working limits."""
    policy = config['discovery_capacity']
    for key in ('working_limit', 'additions_per_seven_days', 'qualification_days', *(['research_pool_limit'] if 'research_pool_limit' in policy else [])):
        if type(policy.get(key)) is not int or not 1 <= policy[key] <= 1000:
            raise ValueError('Invalid discovery capacity: ' + key)
    return policy


def _limit_work(groups, records, keys, policy, today):
    if policy is None:
        return  # Pure legacy callers may supply no policy; workspace/import always supply it.
    import datetime as dt
    selected = set()
    candidates = []
    for ref in [*groups['discovery'], *(r for r in groups['other'] if r['rule'] in QUALIFY)]:
        rid = ref['record_id']
        if ref['rule'] in QUALIFY and today:
            try:
                first = dt.date.fromisoformat(str(records[rid].get('created_at') or '')[:10])
            except ValueError:
                first = today
            if (today - first).days > policy['qualification_days'] and (records[rid].get('availability') or {}).get('status') != 'open':
                ref.update(rule='other.capacity_stale', reason=RULES['other.capacity_stale'].text)
                continue
        candidates.append(ref)
    candidates.sort(key=lambda ref: (ref['rule'] in QUALIFY, keys[ref['record_id']]))
    # Copy qualification references before visible-list placement changes them.
    pool = [dict(ref) for ref in candidates if ref['rule'] in QUALIFY][:policy.get('research_pool_limit', policy['working_limit'])]
    selected.update(ref['record_id'] for ref in candidates[:policy['working_limit']])
    for ref in candidates[policy['working_limit']:]:
        ref.update(rule='other.capacity', reason=RULES['other.capacity'].text)
        ref.pop('worth_a_look', None)
    overflow = [r for r in groups['discovery'] if r['record_id'] not in selected]
    groups['discovery'] = [r for r in groups['discovery'] if r['record_id'] in selected]
    groups['other'].extend(overflow)
    groups['other'].sort(key=lambda ref: keys[ref['record_id']])
    return pool if 'research_pool_limit' in policy else None


def build(state, *, today=None, include_order=False):
    """Return five disjoint work groups, the weekly sections and the bounded research references.

    Discovery refs carry worth_a_look: This week shows those; Jobs → Discovery shows them all.
    A company's person variants share one company/remit and never become separate work items.
    """
    records = {r['id']: r for r in state.get('records') or [] if not r.get('archived_at')}
    intel = state.get('intelligence') or {}
    eligible = set(intel['eligible_advert_ids']) if 'eligible_advert_ids' in intel else None
    actions = {}
    for item in [*(intel.get('actions') or []), *(intel.get('research') or [])]:
        rec = records.get(item.get('record_id')) or {}
        person = records.get(item.get('person_id') or '') or {}
        if rec.get('relationship') == 'do_not_contact' or person.get('relationship') == 'do_not_contact':
            continue
        rid = item.get('company_id') if rec.get('type') == 'person' else item.get('record_id')
        if rid in records:
            actions.setdefault(rid, []).append(item)
    # Reuse stable canonical work actions where available, keeping a person action only as a
    # compatibility fallback for a previously recorded company process.
    for rid, items in actions.items():
        items.sort(key=lambda a: (a.get('record_id') != rid, a.get('id', '')))
    groups = {name: [] for name in ('discovery', 'shortlisted', 'active', 'closed', 'other')}
    weekly = {name: [] for name in SECTIONS}
    keys = {}
    attention_keys = {}
    work_groups = _groups_of_work(records)
    retained_groups = set()
    excluded_groups = set()
    for rid, record in records.items():
        if record.get('exclusion') or record.get('exclusion_contexts'):
            excluded_groups.add(work_groups[rid])
        if (record.get('status') in ACTIVE_JOBS | CLOSED | {'pursue'} or record.get('stage') in CLOSED | {'contacted', 'conversation', 'proposal', 'won'}
                or any(a.get('record_id') == rid and ((a.get('progress') or {}).get('status') in ACTIVE_PROGRESS | TERMINAL_PROGRESS
                                                     or _commitment(a))
                       for a in actions.get(rid, []))):
            retained_groups.add(work_groups[rid])
    excluded = set()
    for rid, record in records.items():
        if record.get('type') not in ('job', 'tender', 'opportunity', 'company', 'agency'):
            continue
        items = actions.get(rid, [])
        primary = _primary_action(rid, items)
        blocked = work_groups[rid] in excluded_groups
        if blocked:
            excluded.add(rid)
        rule, reason, action = _place(record, primary, items, eligible, blocked, work_groups[rid] in retained_groups)
        group = RULES[rule].group
        progress = action.get('progress') or {}
        ref = dict(record_id=rid, action_id=action.get('id'), lane=_lane(record), rule=rule, reason=reason)
        source = _source(record, action)
        if source:
            ref['source'] = source
        uncertainty = _uncertainty(record, action)
        if uncertainty:
            ref['uncertainty'] = uncertainty
        if group == 'discovery':
            ref['worth_a_look'] = _worth_a_look(record)
        groups[group].append(ref)
        if group in ('active', 'shortlisted'):
            cause, rank = _attention(record, action, group == 'shortlisted')
            ref['attention'] = cause
            section = ATTENTION[cause].section
            weekly[section].append(ref)
            attention_keys[rid] = (SECTIONS.index(section), rank, str(progress.get('since') or '9999'), rid)
        # Direct leads use only their qualifying remit date. Editing the containing record or
        # reading unrelated company news cannot make an older buying signal newly current.
        evidence = primary.get('work_evidence') or {}
        date = str(evidence.get('date') or '') if record['type'] in ('company', 'agency') else max(
            [str(record.get('date') or ''), *((f.get('date') or '')
             for f in (action.get('brief') or {}).get('facts') or [])])
        context = record.get('source_context') or {}
        if record['type'] in ADVERTS:
            # Brief facts can carry today's scanner read time. They are not publication dates.
            # Prefer a stated publication date, then the original email, then dated research.
            # Neither a daily re-listing nor a vacancy recheck can renew an older publication.
            date = str(record.get('date') or '')
            if not date and context.get('basis') == 'email':
                date = str(context.get('at') or '')
            if not date:
                date = max((str((review.get('source_date') if 'source_date' in review else
                                 review.get('reviewed_at')) or '') for review in record.get('research') or []), default='')
        date = date[:10]
        # Dated evidence precedes undated at equal score; ISO factual dates then descend before lane
        # without parsing or clock reads. Stable identity is the final tie.
        keys[rid] = (-((record.get('relevance') or {}).get('score') or 0),
                     not date, tuple(-ord(c) for c in date), LANES[ref['lane']], rid)
    for name, refs in groups.items():
        refs.sort(key=lambda ref: attention_keys[ref['record_id']] if name in ('active', 'shortlisted') else keys[ref['record_id']])
    for refs in weekly.values():
        refs.sort(key=lambda ref: attention_keys[ref['record_id']])
    groups.update(weekly)
    pool = _limit_work(groups, records, keys, state.get('discovery_capacity'), today)
    if pool is not None:
        groups['research_pool'] = [ref['record_id'] for ref in pool]
    attempts = state.get('research_attempts') or {}
    def research_order(ref):
        # Never attempted first, then the oldest attempt; equal attempts keep the shared ranking.
        return ((attempts.get(ref['record_id']) or {}).get('attempted_at', ''), keys[ref['record_id']])
    # Research is how uncertain candidates become qualified; admission cannot require an already
    # usable recommendation. They stay in the catalogue and share the existing research places.
    qualification = sorted(pool if pool is not None else (ref for ref in groups['other'] if ref['rule'] in QUALIFY), key=research_order)
    discovery = sorted((ref for ref in groups['discovery'] if _researchable(records[ref['record_id']])),
                       key=research_order)
    # Reserve places for qualification so neither a persistent backlog nor new alerts can starve
    # existing opportunities. Unused capacity is shared; active/shortlisted work stays first.
    targets = qualification[:QUALIFY_PLACES]
    targets += discovery[:RESEARCH_PLACES - len(targets)]
    targets += qualification[QUALIFY_PLACES:QUALIFY_PLACES + RESEARCH_PLACES - len(targets)]
    # A source discovery plus its assessment can cost two calls. Interleave two qualification
    # targets with one existing opportunity so the six-call cap cannot spend every pass on alerts.
    pending = [ref for ref in targets if ref['rule'] in QUALIFY]
    established = [ref for ref in targets if ref['rule'] not in QUALIFY]
    targets = []
    for index in range(max((len(pending) + 1) // 2, len(established))):
        targets.extend(pending[index * 2:index * 2 + 2])
        targets.extend(established[index:index + 1])
    # Needs checking is research's own next qualification targets (always within `targets`), so
    # each card's attempt status moves as research works through the pool. Jobs keeps the rest;
    # the mobile summary does not repeat the whole pool, its source URLs and attempts.
    groups['needs_checking'] = []
    shown = CHECKING_SHOWN
    if pool is not None:
        shown = min(shown, max(0, state['discovery_capacity']['working_limit'] - len(groups['discovery'])))
    for ref in qualification[:shown]:
        attempt = attempts.get(ref['record_id']) or {}
        groups['needs_checking'].append(dict(
            ref, uncertainty=ref.get('uncertainty') or 'The available evidence does not establish the remit and requirements.',
            research_status=dict(status=attempt.get('outcome') or 'not_attempted',
                                 attempted_at=attempt.get('attempted_at'), reason=attempt.get('reason') or '')))
    groups['research_ids'] = list(dict.fromkeys(
        ref['record_id'] for ref in (*groups['active'], *groups['shortlisted'], *targets)
        if ref['record_id'] not in excluded))
    if include_order:
        groups['ranked_ids'] = sorted(keys, key=keys.get)
    return groups


def week_page(projection):
    """This week's references by section in page order, as a page loaded now shows them.

    Best opportunities are the discovery refs marked worth_a_look, which already applies advert
    availability when the projection is built; a page left open also ages them past `until`.
    """
    sections = {key: list(projection.get(key) or []) for key, _ in WEEK_PAGE if key != 'best'}
    sections['best'] = [ref for ref in projection.get('discovery') or [] if ref.get('worth_a_look')]
    return {key: sections[key] for key, _ in WEEK_PAGE}
