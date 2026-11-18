"""Where each approach stands and what the owner does next, derived from recorded facts.

decision_intelligence decides what is worth doing and why. This module gives every approach one
status, next step and permitted factual actions. Personal contact status comes only from recorded
facts; elapsed time never creates a personal follow-up or no-response outcome. Legacy weekly
selection helpers remain for storage/correction compatibility; the state projection never selects
a new week or exposes message composition/approval actions.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import re

from approach_history import RECOMMENDATION, STAGE, latest_drafts
import machines
import search_config
import states

CONFIG = Path(__file__).resolve().parent.parent / 'config/outreach.yml'
_cache = {}

# How the browser carries out a declared action. Anything else is refused by the contract tests.
METHODS = ('outcome', 'transition', 'handover', 'correct', 'decision', 'copy', 'open', 'week', 'next_step')
NEXT_STEP_KINDS = ('ask', 'catch_up', 'recruiter', 'desk', 'note')

PRE_CONTACT = machines.PRE_CONTACT
# A working message is the note on the card for someone he is contacting. Cold notes stay approval drafts.
MESSAGE_KINDS = ('ask', 'catch_up', 'recruiter')
ACTIVE = machines.ACTIVE
TIERS = {'close': 0, 'in_touch': 1, 'known': 2, 'connected': 3, 'cold': 4}
REASON_TRIGGERS = ('advert', 'award', 'funding', 'remit', 'interim_cover', 'new_leader', 'delivery_partner', 'warm_introduction')
APPLIED = ('applied', 'responded', 'interview', 'offer', 'rejected', 'closed')

# The owner's words for each status, the same on every page.
STATUS_LABELS = {
    'research': 'Being researched', 'ready': 'Ready', 'review': 'Note to review', 'approved': 'Note approved',
    'waiting': 'Waiting for a reply', 'follow_up': 'Follow-up suggested', 'replied': 'Replied',
    'submitted': 'CV submitted', 'conversation': 'In conversation', 'proposal': 'Proposal',
    'won': 'Engagement won', 'handed_over': 'In Work', 'held': 'On hold', 'no_response': 'No response',
    'interview': 'Interview', 'offer': 'Offer', 'closed': 'Closed', 'skipped': 'Skipped'}
# An application uses the job's own status words, the same as Work; only the derived follow-up
# states have their own.
JOB_LABELS = {'candidate': 'Discovered', 'pursue': 'Shortlisted', 'skip': 'Skipped', 'applied': 'Applied',
              'responded': 'Responded', 'interview': 'Interview', 'offer': 'Offer', 'rejected': 'Rejected', 'closed': 'Closed'}
KIND_LABELS = {'catch_up': 'Catch-up', 'ask': 'Ask', 'note': 'Note to a decision-maker', 'desk': 'Agency desk',
               'recruiter': 'Recruiter', 'application': 'Application', 'opportunity': 'Your opportunity', 'tender': 'Public tender'}
SENT_LABELS = {'catch_up': "I've been in touch", 'ask': 'I sent my ask', 'note': 'I sent this',
               'desk': 'I contacted the desk', 'recruiter': "I've been in touch", 'opportunity': 'I made contact'}
# The owner's own order on his list (migration 012): first, then next, then everyone else.
PRIORITY_RANK = {'first': 0, 'next': 1}
# The first talking point for someone he knows when there is no note, company news or old conversation.
TIER_PHRASES = {'close': 'You know them well.', 'in_touch': "You're in touch.", 'known': 'You know them from before.'}
# A person's role in his search, from the agent's prepared approach (tools/research_intake.py).
ROLE_LABELS = {'decision_maker': 'Decision-maker', 'internal_advocate': 'Internal advocate', 'connector': 'Connector',
               'referee': 'Referee', 'recruiter': 'Recruiter'}
# Asked, or offered, on the way to a referral: an approach in this state can clash with a recruiter's submission.
REFERRAL_KINDS = ('referral', 'introduction')
# The owner's rule for every recruiter (15 October): no CV goes anywhere without his yes for that role.
CONSENT = ('Agree the role and the client first: no CV goes anywhere without your yes. '
           'Check People for a referral at the same company before you agree.')
COMPANY_WORDS = {'ltd', 'limited', 'plc', 'llp', 'inc', 'group', 'holdings', 'uk', 'the', 'co', 'company'}


def config():
    """config/outreach.yml, reloaded when the file changes."""
    import yaml
    mtime = CONFIG.stat().st_mtime
    if _cache.get('mtime') != mtime:
        _cache.update(mtime=mtime, config=yaml.safe_load(CONFIG.read_text()) or {})
    return _cache['config']


def week_of(day: dt.date) -> str:
    """The Monday of the week containing `day` (a Europe/London date)."""
    return (day - dt.timedelta(days=day.weekday())).isoformat()


def london_today() -> dt.date:
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo('Europe/London')).date()


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _pretty(day):
    return f'{day.day} {day:%b}' if day else ''


def _after(a, b) -> bool:
    """Whether timestamp `a` is later than `b`; a date-only value compares by day, so never later on the same day."""
    try:
        x, y = dt.datetime.fromisoformat(str(a)), dt.datetime.fromisoformat(str(b))
        if (x.tzinfo is None) == (y.tzinfo is None) and len(str(a)) > 10 and len(str(b)) > 10:
            return x > y
    except ValueError:
        pass
    return (_date(a) or dt.date.min) > (_date(b) or dt.date.max)


def _elapsed(start: dt.date, today: dt.date, working: bool) -> int:
    if not working:
        return (today - start).days
    return sum(1 for n in range(1, (today - start).days + 1) if (start + dt.timedelta(days=n)).weekday() < 5)


def kind_of(item, record) -> str:
    route = item.get('route')
    if route == 'agency':
        return 'desk'
    if route == 'recruiter':
        return 'recruiter'
    if route == 'opportunity':
        return {'job': 'application', 'tender': 'tender'}.get(record.get('type'), 'opportunity')
    if route in ('close_network', 'known_contact'):
        return 'catch_up' if (item.get('trigger') or {}).get('kind') in REASON_TRIGGERS else 'ask'
    return 'note'


def _act(id, label, method, **values):
    return dict(id=id, label=label, method=method, **values)


class _Context:
    def __init__(self, intel, state, today, cfg):
        self.today, self.cfg = today, cfg
        self.records = {r['id']: r for r in state.get('records') or []}
        self.outcomes = [o for o in state.get('outcomes') or [] if not o.get('corrected_at')]
        self.by_action = {}
        for o in self.outcomes:
            key = (o.get('snapshot') or {}).get('action_id')
            if key:
                self.by_action.setdefault(key, []).append(o)
        for items in self.by_action.values():
            items.sort(key=lambda o: (o.get('at') or '', o.get('id', 0)))
        self.since = state.get('status_since') or {}
        self.decisions = latest_drafts(state.get('decisions') or [])
        self.links = state.get('links') or []
        # Snapshot-local views preserve link order, direction and duplicate evidence. Progress
        # consults these repeatedly; scanning the whole graph per action made rebuilds quadratic.
        self.organisations, self.introducers, self.employers = {}, {}, {}
        for link in self.links:
            a, b, relation = link.get('from_id'), link.get('to_id'), link.get('relation')
            if relation in ('hiring_for', 'advertised_by') and b in self.records:
                self.organisations.setdefault(a, self.records[b])
            if relation == 'introducer' and (self.records.get(a) or {}).get('type') == 'person':
                self.introducers.setdefault(b, []).append(self.records[a]['name'])
            if relation == 'works_at':
                for person, employer in ((a, b),) if a == b else ((a, b), (b, a)):
                    other = self.records.get(employer) or {}
                    if other.get('type') in ('company', 'agency'):
                        self.employers.setdefault(person, []).append(other.get('name'))
        self.recent = self._recent(state)
        self.notes = {o['id']: (o.get('note') or '').strip() for o in self.outcomes if o.get('id') is not None}
        self.next_steps = {(s['record_id'], s['action_id']):s['value'] for s in state.get('approach_next_steps') or []}
        self.step_context = {(s['record_id'], s['action_id']):s.get('context') or {} for s in state.get('approach_next_steps') or []}
        # The latest direct email with each person, from interactions (the compact state empties them later).
        self.mail = {}
        for i in state.get('interactions') or []:
            pid = i.get('person_id') or i.get('record_id')
            if pid and i.get('channel') == 'email' and i.get('direction') in ('in', 'out') and _date(i.get('at')) \
                    and str(i['at']) > str((self.mail.get(pid) or {}).get('at', '')):
                self.mail[pid] = i

    def _recent(self, state):
        """People the owner approached recently: any recorded outcome within the window. Logged
        interactions (an email import, say) do not count, so a past email never hides someone."""
        days = self.cfg.get('plan', {}).get('recent_contact_days', 30)
        dated = [(o.get('record_id'), o.get('at')) for o in self.outcomes]
        return {rid for rid, at in dated if (d := _date(at)) and 0 <= (self.today - d).days <= days}

    def interval(self, kind):
        key = 'desk' if kind in ('desk', 'recruiter') else 'known' if kind in ('catch_up', 'ask') else 'other'
        rule = self.cfg.get('followup', {}).get(key, {})
        return rule.get('days', 10), bool(rule.get('working_days'))

    def history(self, item):
        rows = self.by_action.get(item['id'], [])
        if not rows and item.get('last_outcome'):
            rows = [item['last_outcome']]  # a record-level (legacy) outcome
        return [dict(kind=o['kind'], at=o.get('at'), id=o.get('id'), note=' — '.join(filter(None, [
            ((o.get('snapshot') or {}).get('target') or {}).get('name', ''), o.get('note') or '']))) for o in rows]

    def org_of(self, rid):
        return self.organisations.get(rid)


def _status(item, ctx, history):
    """The approach's current status, applying the one-follow-up rule to recorded dates."""
    record = ctx.records.get(item['record_id']) or {}
    if item['kind'] == 'application' and record.get('status') in APPLIED + ('skip',):
        status = record['status']
        mapped = machines.JOB_PROGRESS.get(status)
        if mapped:
            progress, keep_since = mapped
            return progress, _date(ctx.since.get(record['id'])) if keep_since else None
        start = _date(ctx.since.get(record['id']))
        follow = [h for h in history if h['kind'] == 'followed_up']
        stage = 'followed_up' if follow else 'sent'
        if follow:
            start = _date(follow[-1]['at'])
        if any(h['kind'] == 'no_response' for h in history):
            return 'no_response', start
    else:
        stage = item.get('stage', 'research')
        start = _date(history[-1]['at']) if history else None
    if stage in ('sent', 'followed_up'):
        days, working = ctx.interval(item['kind'])
        elapsed = bool(start and _elapsed(start, ctx.today, working) >= days)
        return machines.progress_after_contact(stage, person=record.get('type') == 'person', elapsed=elapsed), start
    return stage, start


def _section(item, status):
    return machines.progress_section(status, item['kind'])


def _terminal_recorded(item, ctx, status, history):
    """Distinguish a reported ending from source holds and elapsed follow-up time.

    History is already action-scoped, chronological and stripped of corrected outcomes. The
    effective progress status also matters: a later job stage can supersede an older outcome.
    """
    if status not in ('held', 'no_response', 'closed', 'skipped', 'won', 'handed_over'):
        return False
    record = ctx.records.get(item['record_id']) or {}
    if record.get('status') in ('skip', 'closed', 'rejected') or record.get('stage') in ('won', 'lost'):
        return True
    latest = next((h for h in reversed(history) if h.get('kind') in STAGE), None)
    if latest:
        return STAGE[latest['kind']] == status
    # Historical explicit holds remain authoritative without treating a qualification hold
    # as the owner's decision. An outcome, when present, already supersedes that decision above.
    decision = ctx.decisions.get(item['record_id'], {})
    return (status == 'held' and decision.get('status') in ('answered', 'done')
            and decision.get('answer') in ('hold', 'drop', 'not_yet'))


READY_NEXT = {'catch_up': 'Get in touch and mention the reason.', 'ask': 'Send your ask.',
              'note': 'Write a short note using the brief, then record it here.',
              'desk': 'Contact the desk about its relevant roles.',
              'recruiter': 'Get in touch: say you are available and ask which briefs fit.',
              'application': 'Apply through the advert.', 'tender': 'Review the tender in Work.'}


def _texts(item, ctx, status, start, history=()):
    """Why this person, why now, what to say or do, and what happens next."""
    kind, brief = item['kind'], item.get('brief') or {}
    record = ctx.records.get(item['record_id']) or {}
    trigger = item.get('trigger') or {}
    access = (item.get('access') or {}).get('reason', '').strip()
    if access and not access.endswith('.'):
        access += '.'
    if kind == 'catch_up':
        why = f"{trigger.get('text', 'Their company has news').rstrip('.')}. {access}".strip()
        say = f"Catch up and mention their news. If it fits, ask: {ctx.cfg.get('ask', '')}"
    elif kind == 'ask':
        why = access or 'You know them.'
        say = (ctx.cfg.get('asks') or {}).get(_ask_of(item, ctx) or '') or ctx.cfg.get('ask', '')
    elif kind == 'desk':
        why = brief.get('why_now') or 'This desk places relevant contract work.'
        say = brief.get('help') or brief.get('recommendation', '')
    elif kind == 'recruiter':
        why = access or 'A recruiter you chose.'
        say = brief.get('help') or brief.get('recommendation', '')
    elif kind in ('application', 'tender'):
        why = brief.get('why_now') or 'A relevant advertised role.'
        say = brief.get('recommendation') or 'Apply through the advert with the suggested CV.'
    else:
        why = brief.get('why_now') or ''
        say = brief.get('question') and f"Ask: {brief['question']}" or brief.get('recommendation', '')
    entry = brief.get('approach_route')
    if entry and kind not in ('application', 'tender', 'desk'):
        why, say = entry['why'], f"Ask: {entry['question']}"
    since = _pretty(start)
    days, working = ctx.interval(kind)
    due = start and _add(start, days, working)
    nxt = {
        'research': 'More evidence is needed before this is a usable approach.',
        'ready': {**READY_NEXT, 'opportunity': record.get('next_action') or 'Qualify this opportunity.'}[kind],
        'review': 'Review the drafted note.',
        'approved': 'Copy the approved note and send it.',
        'waiting': (f"Contact recorded {since}. Waiting for a reply." if since else 'Waiting for a reply.') if record.get('type') == 'person' else
                   (f"Sent {since}. A follow-up is suggested from {_pretty(due)} if nothing comes back." if since else 'Waiting for a reply.'),
        'follow_up': f"No reply since {since}. Follow up once." if since else 'Follow up once.',
        'no_response': 'You recorded no response; record a reply if one comes.',
        'replied': {'ask': 'Take up the introduction or details they offered.', 'desk': CONSENT, 'recruiter': CONSENT,
                    'application': 'Arrange the interview.'}.get(kind, 'Arrange a conversation.'),
        'submitted': "Wait for the desk's feedback on your CV.",
        'conversation': 'Agree the next step.' if kind == 'opportunity' else 'Agree the next step, then hand it over to Work.',
        'proposal': 'Decide on the proposal.' if kind == 'opportunity' else 'Decide on the proposal, then hand it over to Work.',
        'interview': 'Prepare for the interview and record the result.',
        'offer': 'Decide on the offer.',
        'won': 'The engagement is won.' if kind == 'opportunity' else 'Hand the engagement over to Work.',
        'handed_over': 'It continues in Work.',
        'held': (item.get('readiness') or {}).get('reason') or 'On hold.',
        'closed': 'This application is closed.',
        'skipped': 'You skipped this role.',
    }[status]
    if kind in ('ask', 'catch_up') and status == 'replied' and history and history[-1]['kind'] in REFERRAL_KINDS:
        nxt = 'Take up the introduction they offered. Thank them now, and tell them how it goes after you have spoken.'
    # Once the other side has acted, the step he recorded leads: his own note on the latest outcome, or
    # for an opportunity the step agreed at handover. A person's next_action is never used: agent
    # follow-ups write it (his own words about someone go in "Your note").
    agreed = ctx.notes.get(history[-1].get('id'), '') if history else ''
    if not agreed and (kind == 'opportunity' or record.get('type') == 'opportunity'):
        agreed = (record.get('next_action') or '').strip()
    if agreed and status in ('replied', 'submitted', 'conversation', 'proposal', 'interview', 'offer', 'won', 'handed_over') \
            and agreed.rstrip('.').lower() not in nxt.lower():
        nxt = f"{agreed.rstrip('.')}. {nxt}"
    introducers = _introducers(item, ctx) if status == 'conversation' else []
    if introducers and status == 'conversation':
        nxt += f" Tell {' and '.join(introducers)}, who introduced you, how it went."
    return why, say, nxt


def _introducers(item, ctx):
    """The people recorded as introducing this person to the owner (an `introducer` link to them)."""
    pid = item.get('person_id')
    return list(ctx.introducers.get(pid, ())) if pid else []


def _add(start, days, working):
    if not working:
        return start + dt.timedelta(days=days)
    day, left = start, days
    while left:
        day += dt.timedelta(days=1)
        left -= day.weekday() < 5
    return day


def _person_of(item, ctx):
    return ctx.records.get(item.get('person_id') or '') or {}


def _own_note(person):
    """The owner's own words about someone, from his list (`Your note (…): …` in known_context)."""
    for line in (person.get('known_context') or '').splitlines():
        if line.startswith('Your note') and ':' in line:
            return line.split(':', 1)[1].strip()
    return ''


def _last_spoke(person):
    threads = ((person.get('linkedin') or {}).get('evidence') or {}).get('threads') or {}
    return (threads.get('last_two_way') or '')[:4] if threads.get('two_way') else ''


def _ask_of(item, ctx):
    """The kind of ask for this move: the person's own, else the usual one for its kind. Someone he
    knows with no ask set gets the catch-up, the first rung (15 October): never a referral by default."""
    chosen = _person_of(item, ctx).get('preferred_ask')
    if chosen:
        return chosen
    return {'ask': 'catch_up', 'catch_up': 'catch_up', 'recruiter': 'availability', 'desk': 'availability'}.get(item['kind'])


def _prepared_review(person):
    """The newest research on this person that carries a prepared approach, or {}."""
    if 'prepared_approach' in person:
        return person['prepared_approach']
    return next((review for review in person.get('research') or [] if 'approach' in review), {})


def _prepared(person):
    """The agent's prepared approach for this person, or None."""
    return _prepared_review(person).get('approach') or None


def _plan(item, ctx):
    review = _prepared_review(_person_of(item, ctx)) if item['kind'] in ('ask', 'catch_up', 'recruiter') else {}
    plan = review.get('approach')
    if not plan:
        return None
    cv = search_config.cvs().get(plan.get('cv') or '') or {}
    # A draft CV can be suggested, but its readiness stays visible until the owner reviews it (config/cvs.yml).
    title = cv.get('title', '') + (' (still a draft)' if cv.get('title') and cv.get('readiness') != 'ready' else '')
    # Where the preparation came from, when it was checked, and what it has not settled.
    source = next((u for u in review.get('source_urls') or [] if str(u).startswith('https://')), '')
    return dict(role=ROLE_LABELS.get(plan.get('role'), ''), cv=title, cv_url=cv.get('url') or '',
                goal=plan.get('goal') or '', wave=plan.get('wave'), last=plan.get('last') or '',
                checked=str(review.get('reviewed_at') or '')[:10], source=source,
                check=next(iter(review.get('decisive_unknowns') or []), ''),
                gaps=list(review.get('decisive_unknowns') or []))


def _points(item, ctx, status, why):
    """Two or three talking points before contact: why now, a question only when evidence supports
    one, and what the owner asks for. With a message on the card these stay behind "Why this note"."""
    if status not in PRE_CONTACT:
        return []
    kind, brief = item['kind'], item.get('brief') or {}
    person, entry = _person_of(item, ctx), brief.get('approach_route')
    plan = _prepared(person) if kind in ('ask', 'catch_up', 'recruiter') else None
    if plan and plan.get('points'):
        # The agent's prepared points, written from his notes and public sources, end with the fitted ask.
        note = _own_note(person)
        own = f'Your note: {note.rstrip(".")}.' if note else ''
        ask = plan.get('ask') or (ctx.cfg.get('asks') or {}).get(_ask_of(item, ctx) or '')
        if own and not any(note.rstrip('.').lower() in p.lower() for p in plan['points']):
            return [p for p in [own, plan['points'][0], ask] if p][:3]
        return [p for p in [*plan['points'][:2], ask] if p][:3]
    points = []
    if kind == 'catch_up' and not entry:
        points.append(((item.get('trigger') or {}).get('text') or why).rstrip('.') + '.')
    elif kind in ('ask', 'recruiter') and not entry:
        note, year = _own_note(person), _last_spoke(person)
        li = person.get('linkedin') or {}
        if note:
            points.append(f'How you know them: {note.rstrip(".")}.')
        elif year and year < str(ctx.today.year - 1):
            points.append(f'You last spoke in {year}: reason enough to get back in touch.')
        elif kind == 'recruiter' and (li.get('observed_company') or person.get('organisation')):
            points.append(f"Recruits at {person.get('organisation') or li['observed_company']}"
                          + ('' if person.get('organisation') else ' (per LinkedIn, Sep 2026)') + '.')
        else:
            points.append(TIER_PHRASES.get(item.get('tier'), why))
    else:
        points.append(why)
    question = entry['question'] if entry else brief.get('question') if kind in ('note', 'desk', 'recruiter') or (
        kind == 'catch_up' and (item.get('trigger') or {}).get('kind') in REASON_TRIGGERS) else ''
    if question:
        points.append(f'Ask: {question}')
    wording = (ctx.cfg.get('asks') or {}).get(_ask_of(item, ctx) or '')
    if wording and kind not in ('note', 'application', 'tender', 'opportunity'):
        points.append(wording)
    return [p for p in points if p][:3]


def _actions(item, ctx, status, history, message=None):
    """The actions the site may offer for this status, primary first."""
    kind, rid, aid = item['kind'], item['record_id'], item['id']
    record = ctx.records.get(rid) or {}
    channel = _person_of(item, ctx).get('preferred_channel')
    out = lambda k, label: _act(k, label, 'outcome', kind=k, record_id=rid, action_id=aid,
                                **({'channel': channel} if channel and k in ('sent', 'followed_up') else {}),
                                **({'asks': 'company'} if k == 'submitted' else {}))
    move = lambda to, label: _act(to, label, 'transition', record_id=rid, field='status', to=to)
    handover = lambda: _act('handover', 'Hand over to Work', 'handover', record_id=rid, action_id=aid,
                           company_id=item.get('company_id'))

    def realise(declared):
        built = []
        for offer in declared:
            if offer.method == 'outcome':
                built.append(out(offer.event, offer.label or SENT_LABELS[kind]))
            elif offer.method == 'transition':
                built.append(move(offer.event, offer.label))
            elif offer.method == 'handover':
                built.append(handover())
        return built

    acts = []
    if kind == 'tender':
        return [_act('open', 'Open the tender', 'open', record_id=rid, primary=True)]
    if kind == 'application':
        acts = realise(machines.offers(kind, status))
        # Offer only the status moves the job allows, so the card never shows a step the API refuses.
        allowed_moves = states.allowed_from('job', 'status', record.get('status'))
        acts = [a for a in acts if a['method'] != 'transition' or a['to'] in allowed_moves]
        if status in machines.APPLICATION_CORRECT and record.get('status') in APPLIED:
            acts.append(_act('correct', 'Correct the last status change', 'correct', record_id=rid, field='status'))
    else:
        acts = realise(machines.offers(kind, status))
        if status == 'won' and any(h['kind'] == 'handed_over' for h in history):
            acts = [a for a in acts if a['id'] != 'handover']
        if status == 'handed_over':
            opp = next((o.get('snapshot', {}).get('opportunity_id') for o in reversed(ctx.by_action.get(item['id'], []))
                        if o['kind'] == 'handed_over'), None)
            if opp:
                acts.append(_act('open', 'Open in Work', 'open', record_id=opp))
        if kind == 'opportunity' or record.get('type') == 'opportunity':
            acts = [a for a in acts if a['id'] != 'handover']
            if status == 'won' and not acts:
                acts.append(_act('open', 'Open in Work', 'open', record_id=rid))
        last = next((h for h in reversed(history) if h.get('id') is not None), None)
        if last and status not in PRE_CONTACT + ('research',) and _correctable(item, last, ctx):
            acts.append(_act('correct', 'Correct the last entry', 'correct', record_id=rid, outcome_id=last['id']))
    allowed = {a['kind'] for a in (record.get('outcome_actions') or [])} if 'outcome_actions' in record else None
    acts = [a for a in acts if a['method'] != 'outcome' or allowed is None or a['kind'] in allowed]
    if item['kind'] in NEXT_STEP_KINDS and status not in machines.PERSON_TERMINAL:
        acts.append(_act('next_step', 'Update next step', 'next_step', record_id=rid, action_id=item['id']))
    if message:
        for a in acts:
            if a.get('method') == 'outcome' and a.get('kind') in ('sent', 'followed_up'):
                a['version_id'] = message['version_id']
    for n, a in enumerate(acts):
        a['primary'] = n == 0
    return acts


def _mail(item, ctx):
    """The latest direct email with this person: its date, who wrote last and its Gmail link."""
    i = ctx.mail.get(_person_of(item, ctx).get('id'))
    if not i:
        return None
    url = i.get('evidence_ref') or ''
    return dict(at=str(i['at'])[:10], direction=i['direction'], url=url if url.startswith('https://mail.google.com/') else '')


def progress(item, ctx):
    history = ctx.history(item)
    status, start = _status(item, ctx, history)
    terminal_recorded = _terminal_recorded(item, ctx, status, history)
    why, say, nxt = _texts(item, ctx, status, start, history)
    next_is_default = status == 'ready' and nxt == READY_NEXT.get(item['kind'])
    if status == 'no_response' and not terminal_recorded:
        nxt = 'No reply is recorded after the follow-up; review the next step.'
    next_step = ctx.next_steps.get((item['record_id'], item['id']))
    if next_step and next_step['kind'] in ('mine', 'waiting') and item['kind'] in NEXT_STEP_KINDS \
            and status not in ('research', 'held', 'no_response', 'closed', 'skipped', 'won', 'handed_over'):
        nxt = ('Waiting for them: ' if next_step['kind'] == 'waiting' else '') + next_step['text']
        next_is_default = False
        if next_step.get('agreed_on'):
            nxt += f" · agreed {_pretty(_date(next_step['agreed_on']))}"
    message = None  # Outgoing wording stays in the historical timeline/full export.
    actions = _actions(item, ctx, status, history, message)
    mail = _mail(item, ctx)
    points = _points(item, ctx, status, why)
    reply_to_record = False
    if status in ('waiting', 'follow_up', 'no_response') and history and mail and mail['direction'] == 'in' \
            and _after(ctx.mail[_person_of(item, ctx)['id']]['at'], history[-1]['at']) \
            and any(a.get('kind') == 'replied' for a in actions):
        reply_to_record = True
        # They wrote after his last step: recording their reply comes first, not a follow-up.
        nxt = f"They wrote on {_pretty(_date(mail['at']))}: record their reply."
        next_is_default = False
        actions.sort(key=lambda a: a.get('kind') != 'replied')
        for n, a in enumerate(actions):
            a['primary'] = n == 0
    label = STATUS_LABELS[status]
    record = ctx.records.get(item['record_id']) or {}
    if item['kind'] == 'application' and status not in ('follow_up', 'no_response') and record.get('status') in JOB_LABELS:
        label = JOB_LABELS[record['status']]
    incoming = record.get('incoming') or {}
    if incoming.get('current') and item['kind'] == 'application' and incoming.get('status') == record.get('status') and status not in ('follow_up','no_response'):
        label = incoming['label']
        nxt = incoming.get('next_step') or 'Record the outcome and agreed next step when known.'
        next_is_default = False
        say = ''
        message = ''
    person = _person_of(item, ctx)
    return dict(status=status, terminal_recorded=terminal_recorded,
                shortlisted=record.get('status') == 'pursue', label=label, kind_label=KIND_LABELS[item['kind']],
                section=_section(item, status), since=start.isoformat() if start else '', why=why, say=say,
                points=points, message=message, sent=None, next_step=next_step,
                mail=mail, reply_to_record=reply_to_record, next_is_default=next_is_default, channel=person.get('preferred_channel') or '', who=person.get('kind') or '',
                ask=_ask_of(item, ctx) or '' if item['kind'] in ('ask', 'catch_up', 'recruiter') else '',
                next=nxt, history=history, actions=actions, primary=actions[0] if actions else None,
                note=item.get('tracked_note', ''), plan=_plan(item, ctx), warning='')


def _tracked(ctx, items, intel):
    """Approaches that have a recorded history but no current recommendation, and applications.
    A closed advert, an exclusion or changed evidence must not hide what already happened."""
    known_ids = {a['id'] for a in items}
    known_records = {a['record_id'] for a in items}
    extra = []
    for aid, rows in ctx.by_action.items():
        if aid in known_ids or aid.startswith('application:'):
            continue
        last = rows[-1]
        earlier = next((o['snapshot'] for o in reversed(rows) if (o.get('snapshot') or {}).get('brief')), {})
        snap = {**earlier, **(last.get('snapshot') or {})}
        record = ctx.records.get(last['record_id']) or {}
        company = ctx.records.get(snap.get('company_id') or '') or {}
        stage = next((STAGE[o['kind']] for o in reversed(rows) if o['kind'] in STAGE), 'sent')
        extra.append(dict(id=aid, record_id=last['record_id'], company_id=snap.get('company_id'), person_id=snap.get('person_id'),
                          title=f"Approach to {record.get('name') or 'a record'}", person=record.get('name', '') if record.get('type') == 'person' else '',
                          company=company.get('name') or record.get('organisation') or (record.get('linkedin') or {}).get('observed_company') or '',
                          role=record.get('role') or (record.get('linkedin') or {}).get('observed_role') or '', route=snap.get('route', 'direct'),
                          stage=stage, fit=snap.get('fit') or {}, access=snap.get('access') or {}, confidence=snap.get('confidence') or {},
                          readiness=dict(label=STATUS_LABELS.get(stage, stage), reason='Recorded history.'),
                          brief=dict(snap.get('brief') or {}, recommendation=RECOMMENDATION.get(stage, '')),
                          priority=dict(score=0, reasons=[]), tier=None, trigger=None, adverts=0,
                          tracked_note='This approach is no longer among the current recommendations; its history is kept.'))
        known_records.add(last['record_id'])
    # A saved commitment can precede the first contact. Recommendation changes must not hide it.
    tracked_ids = known_ids | {a['id'] for a in extra}
    for (rid, aid), step in ctx.next_steps.items():
        record = ctx.records.get(rid)
        if aid in tracked_ids or not record or record.get('archived_at') or step['kind'] not in ('mine', 'waiting'):
            continue
        snap = ctx.step_context[(rid, aid)]
        person = ctx.records.get(snap.get('person_id') or '') or {}
        if record.get('relationship') == 'do_not_contact' or person.get('relationship') == 'do_not_contact':
            continue
        company = ctx.records.get(snap.get('company_id')) or {}
        extra.append(dict(id=aid, record_id=rid, **snap,
                          title=f"Approach to {record['name']}", person=record['name'] if record['type'] == 'person' else '',
                          company=company.get('name') or record.get('organisation') or '', role=record.get('role') or '',
                          stage='ready', fit={}, access={}, confidence={}, brief={}, priority=dict(score=0,reasons=[]),
                          readiness=dict(label='Saved next step', reason='Your recorded commitment.'), tier=None, adverts=0,
                          tracked_note='Kept because you recorded a next step; it is not a current recommendation.'))
    for record in ctx.records.values():
        if record.get('type') != 'job' or record.get('status') not in APPLIED + ('pursue',) or record['id'] in known_records:
            continue
        org = ctx.org_of(record['id'])
        aid = f"action:{record['id']}:{org['id'] if org else ''}"
        if aid in known_ids:
            continue
        stage = 'ready' if record.get('status') == 'pursue' else 'sent'
        extra.append(dict(id=aid, record_id=record['id'], company_id=org['id'] if org else None, person_id=None,
                          title=f"Application: {record.get('name') or 'role'}", person='', company=(org or {}).get('name') or record.get('organisation') or '',
                          role=record.get('name') or '', route='opportunity', stage=stage, fit={}, access={}, confidence={},
                          readiness=dict(label='Shortlisted' if stage == 'ready' else 'Application',
                                         reason='Shortlisted role.' if stage == 'ready' else 'Recorded application.'),
                          brief=dict(recommendation='Apply through the advert with the suggested CV.' if stage == 'ready'
                                     else 'Follow the application through its recorded status.', why_now=''),
                          priority=dict(score=0, reasons=[]), tier=None, trigger=None, adverts=0, tracked_note=''))
    return extra


def annotate(intel, state, today=None, cfg=None):
    """Add `kind` and `progress` to every approach, track recorded history that has no current
    recommendation, and compose the week. Returns the same intelligence dict, extended."""
    today = today or london_today()
    cfg = cfg or config()
    ctx = _Context(intel, state, today, cfg)
    actions, research = intel.get('actions') or [], intel.get('research') or []
    actions += _tracked(ctx, actions + research, intel)
    for item in actions + research:
        item['kind'] = kind_of(item, ctx.records.get(item['record_id']) or {})
        item['progress'] = progress(item, ctx)
    intel['actions'] = actions
    _clashes(actions, ctx)
    sections, people_shown = {}, set()
    urgency = {'follow_up': 0, 'replied': 1, 'submitted': 1, 'conversation': 1, 'proposal': 1, 'interview': 1,
               'offer': 1, 'waiting': 2}
    for item in sorted(actions + research, key=lambda a: (urgency.get(a['progress']['status'], 3),
                                                          a['progress']['since'] if a['progress']['section'] == 'in_progress' else '')):
        section = item['progress']['section']
        if _demoted_person(item, ctx) or (_excluded(item, ctx) and section not in ('in_progress', 'parked', 'done')):
            continue  # excluded from suggestions; recorded history stays visible
        if section == 'people':
            # One card per person you know: their strongest context; the others stay on their sheet.
            if item.get('person_id') in people_shown:
                continue
            people_shown.add(item.get('person_id'))
        sections.setdefault(section, []).append(item['id'])
    intel['sections'] = sections
    intel['followups'] = [a['id'] for a in actions if a['progress']['status'] == 'follow_up'
                          and (ctx.records.get(a['record_id']) or {}).get('type') != 'person'
                          and not _demoted_person(a, ctx)]
    # Saved membership is retained as history, with no new selection or membership actions.
    intel['week'] = dict(week=week_of(today), label='', stored=bool(state.get('week_items')),
                         items=[dict(row, actions=[]) for row in state.get('week_items') or []], deferred=[], focus_ids=[])
    # work_selection alone owns weekly sections; this module owns factual progress.
    intel['tally'] = tally(ctx)
    intel['labels'] = dict(status=STATUS_LABELS, kind=KIND_LABELS)
    return intel


def _company_key(name):
    return ' '.join(w for w in re.sub(r'[^\w\s&]', ' ', (name or '').casefold()).split() if w not in COMPANY_WORDS)


def _companies(person, ctx):
    """Where someone works, as far as the site knows: their organisation, LinkedIn's observation (a
    warning tolerates "per LinkedIn") and any company linked with works_at."""
    linkedin = person.get('linkedin') or {}
    names = [person.get('organisation'), linkedin.get('observed_company') if isinstance(linkedin, dict) else '']
    names.extend(ctx.employers.get(person.get('id'), ()))
    return {k: n for n in names if n and (k := _company_key(n))}


def _clashes(actions, ctx):
    """Warn on both sides when a recruiter submitted the owner's CV to a company where a referral through
    someone he knows is under way (his rule, 15 October). Derived from outcomes; nothing is stored."""
    submitted = {}
    for o in ctx.outcomes:
        target = ((o.get('snapshot') or {}).get('target') or {}).get('name')
        if o.get('kind') == 'submitted' and target and _company_key(target):
            submitted.setdefault(_company_key(target), []).append(o)
    if not submitted:
        return
    by_id = {a['id']: a for a in actions}
    for item in actions:
        p = item['progress']
        person = _person_of(item, ctx)
        asked = {h['kind'] for h in p['history']} & set(REFERRAL_KINDS) or person.get('preferred_ask') == 'introduction'
        live = p['status'] not in PRE_CONTACT + ('research', 'no_response', 'held', 'closed', 'skipped')
        if item['kind'] not in ('ask', 'catch_up') or not (asked and live):
            continue
        # A person can have separate approaches to different companies. The selected approach's
        # company takes precedence over their employer; employer evidence is only a fallback.
        company = ctx.records.get(item.get('company_id')) or {}
        companies = {_company_key(company['name']): company['name']} if company else _companies(person, ctx)
        for key in companies.keys() & submitted.keys():
            for o in submitted[key]:
                recruiter = (ctx.records.get(o['record_id']) or {}).get('name') or 'A recruiter'
                company = o['snapshot']['target']['name']
                p['warning'] = ' '.join(filter(None, [p['warning'], f"{recruiter} submitted your CV to {company} on "
                                                      f"{_pretty(_date(o.get('at')))}. Tell {person.get('name')} before they refer you there."]))
                side = by_id.get((o.get('snapshot') or {}).get('action_id')) or next(
                    (a for a in actions if a['record_id'] == o['record_id']), None)
                if side:
                    side['progress']['warning'] = ' '.join(filter(None, [side['progress']['warning'],
                        f"A referral to {company} is under way through {person.get('name')}. Two routes into one company "
                        f"can clash: agree with {person.get('name')} first."]))


def tally(ctx):
    """This week's counts from recorded outcomes: contacts, replies and conversations. Derived on
    read, never stored, and never a target."""
    start = dt.date.fromisoformat(week_of(ctx.today))
    counts = dict(contacted=0, replies=0, conversations=0)
    for o in ctx.outcomes:
        day = _date(o.get('at'))
        if not day or not start <= day <= ctx.today:
            continue
        key = {'sent': 'contacted', 'followed_up': 'contacted', 'replied': 'replies', 'introduction': 'replies',
               'referral': 'replies', 'conversation': 'conversations'}.get(o.get('kind'))
        if key:
            counts[key] += 1
    return counts


def _demoted_person(item, ctx):
    record = ctx.records.get(item.get('record_id') or '') or {}
    person = _person_of(item, ctx)
    return record.get('relationship') == 'do_not_contact' or person.get('relationship') == 'do_not_contact'


def _excluded(item, ctx):
    """The owner's exclusions (config/relevance.yml) and demoted people stay out of suggestions."""
    record = ctx.records.get(item['record_id']) or {}
    company = ctx.records.get(item.get('company_id') or '') or {}
    return bool(record.get('exclusion') or record.get('exclusion_contexts') or company.get('exclusion')
                or _demoted_person(item, ctx))


def _slot(item, ctx):
    kind = item['kind']
    if kind in ('catch_up', 'ask'):
        return 'known'
    rec = ctx.records.get(item['record_id']) or {}
    if kind in ('desk', 'recruiter') or (kind == 'application' and (
            (rec.get('relevance') or {}).get('lane') == 'contract' or rec.get('status') == 'pursue')):
        return 'contract'
    if kind in ('note', 'opportunity'):
        return 'prospect'
    return None


def _priority(item, ctx):
    return PRIORITY_RANK.get((ctx.records.get(item.get('person_id') or '') or {}).get('priority'), 2)


def _wave(item, ctx):
    """The prepared approach's wave orders people within his own order; no wave comes last."""
    wave = (_prepared(ctx.records.get(item.get('person_id') or '') or {}) or {}).get('wave')
    return wave if type(wave) is int else 99


def _candidates(intel, ctx):
    pools = {'known': [], 'contract': [], 'prospect': []}
    for item in intel['actions']:
        p = item['progress']
        slot = _slot(item, ctx)
        if not slot or p['status'] not in PRE_CONTACT or p['section'] == 'research':
            continue
        if item.get('person_id') in ctx.recent or item['record_id'] in ctx.recent or _excluded(item, ctx):
            continue
        pools[slot].append(item)
    rank = {a['id']: n for n, a in enumerate(intel['actions'])}
    pools['known'].sort(key=lambda a: (_priority(a, ctx), _wave(a, ctx), a['kind'] != 'catch_up', TIERS.get(a.get('tier'), 5),
                                       rank[a['id']]))
    # Recruiters on his list, then desks with live roles, then other recruiters and contract roles.
    pools['contract'].sort(key=lambda a: (_priority(a, ctx), _wave(a, ctx), a['kind'] != 'desk' or not a.get('adverts'),
                                          a['kind'] != 'recruiter', rank[a['id']]))
    pools['prospect'].sort(key=lambda a: (_priority(a, ctx), rank[a['id']]))
    # Release 2: his list leads. Once it can fill the week's places for people he knows, only people on
    # it are suggested there; until then the other people he knows fill the rest, never cold notes.
    plan = ctx.cfg.get('plan', {})
    places = plan.get('size', 6) - min(plan.get('contract_slots', 2), len(pools['contract']))
    listed = [a for a in pools['known'] if _priority(a, ctx) < 2]
    if len({a.get('person_id') or a['record_id'] for a in listed}) >= places:
        pools['known'] = listed
    return pools


def _reason(item, slot, ctx=None):
    listed = ctx is not None and _priority(item, ctx) < 2
    if slot == 'known':
        how = {'close': 'Someone you know well', 'in_touch': "Someone you're in touch with"}.get(item.get('tier'), 'Someone you know')
        return ('Your list · ' + how[0].lower() + how[1:] if listed else how) + (' · their company has news' if item['kind'] == 'catch_up' else '')
    if slot == 'contract':
        if item['kind'] == 'recruiter':
            return 'Your list · a recruiter' if listed else 'Contract work: a recruiter you know'
        if item['kind'] == 'application' and ctx is not None and \
                ((ctx.records.get(item['record_id']) or {}).get('relevance') or {}).get('lane') != 'contract':
            return 'A role you shortlisted'
        return 'Contract work: ' + ('an agency desk with live roles' if item['kind'] == 'desk' else 'a contract role')
    return 'A confirmed decision-maker' if item['kind'] == 'note' else 'Your own opportunity'


# What each recorded step was called on its button, for the Undo that names it.
STEP_LABELS = {'followed_up': 'I followed up', 'replied': 'They replied', 'no_response': 'Park: no response',
               'conversation': 'We spoke', 'referral': 'They gave a referral', 'introduction': 'They offered an introduction',
               'submitted': 'CV submitted', 'no_fit': 'Not a fit', 'no_budget': 'No current budget',
               'permanent_only': 'Permanent only', 'proposal': 'Proposal sent', 'won': 'Engagement won',
               'handed_over': 'Hand over to Work'}


def _correctable(item, last, ctx):
    """Any step can be corrected except a handover whose opportunity has moved on in Work: undoing that one
    archives the opportunity it made (records.correct), so it is offered only while that is unchanged."""
    if last['kind'] != 'handed_over':
        return True
    hit = next((o for o in ctx.by_action.get(item['id'], []) if o.get('id') == last['id']), None)
    snap = (hit or {}).get('snapshot') or {}
    opp = ctx.records.get(snap.get('opportunity_id') or '')
    return bool(opp) and opp.get('revision') == snap.get('opportunity_revision')


def _undo(item, rid, start, moved, ctx):
    """A done card on This week keeps one Undo for a step recorded this week, so a mistaken tap is put
    right where it happened. A step from an earlier week is history and stays on the record's sheet."""
    if moved:
        return [_act('undo', 'Undo: I applied', 'correct', record_id=rid, field='status', undo=True, primary=True)]
    last = next((h for h in reversed(item['progress']['history']) if h.get('id') is not None), None)
    if not last or (_date(last.get('at')) or dt.date.min) < start or not _correctable(item, last, ctx):
        return []
    label = SENT_LABELS.get(item['kind'], 'I made contact') if last['kind'] == 'sent' else STEP_LABELS.get(last['kind'], 'the last step')
    return [_act('undo', f'Undo: {label}', 'correct', record_id=rid, outcome_id=last['id'], undo=True, primary=True)]


def compose_week(intel, rows, ctx):
    week = week_of(ctx.today)
    by_id = {a['id']: a for a in intel['actions'] + intel['research']}
    start = dt.date.fromisoformat(week)
    label = f"{start.day}–{(start + dt.timedelta(days=6)).day} {(start + dt.timedelta(days=6)):%b}"
    rows = sorted((r for r in rows if r.get('week') == week), key=lambda r: (r['position'], r['action_id']))
    if rows:
        items, deferred = [], []
        for row in rows:
            item = by_id.get(row['action_id'])
            entry = dict(action_id=row['action_id'], record_id=row['record_id'], slot=row['slot'], reason=row.get('reason') or '')
            record = ctx.records.get(row['record_id']) or {}
            moved = record.get('type') == 'job' and record.get('status') in APPLIED and (_date(ctx.since.get(record['id'])) or dt.date.min) >= start
            # Any recorded step means the owner has acted on it: handled, never "no longer suitable". A set-aside
            # item he acted on anyway comes back to the list as done.
            done = bool(item and (moved or item['progress']['history']))
            if row['state'] == 'deferred' and not done:
                # Put back while it still qualifies; otherwise it would return as "no longer suitable".
                ok = item and item['progress']['status'] in PRE_CONTACT and not _excluded(item, ctx)
                entry['actions'] = [_act('restore', 'Put back', 'week_restore', action_id=row['action_id'])] if ok else []
                deferred.append(entry)
                continue
            if item and (done or item['progress']['status'] in PRE_CONTACT):
                entry.update(state='planned', done=done)
            else:
                why = ((item or {}).get('readiness', {}).get('reason') or '').rstrip('.') if item else ''
                entry.update(state='invalid', done=False,
                             reason=f"No longer suitable: {why or 'it may have closed, been excluded or need more research'}.")
            planned = entry['state'] == 'planned'
            entry['actions'] = _undo(item, row['record_id'], start, moved, ctx) if entry['done'] else [
                _act('defer', 'Not this week' if planned else 'Replace it',
                     'week', action_id=row['action_id'], primary=not planned, undoable=planned)]
            items.append(entry)
        return dict(week=week, label=label, stored=True, items=items, deferred=deferred)
    chosen = choose_week(_candidates(intel, ctx), ctx)
    for c in chosen:
        c.update(state='planned', done=False,
                 actions=[_act('defer', 'Not this week', 'week', action_id=c['action_id'], primary=False, undoable=True)])
    return dict(week=week, label=label, stored=False, items=chosen, deferred=[])


def choose_week(pools, ctx, kept=(), skip=()):
    """The week's places: people he knows, then at most the contract places, then prospects, then any
    place left, with one item per person and per company. `kept` items (already acted on) hold their
    places and count against their slot; `skip` holds this week's deferred items."""
    plan = ctx.cfg.get('plan', {})
    size = max(plan.get('size', 6), len(kept))
    contract_n = min(plan.get('contract_slots', 2), len(pools['contract']))
    chosen = [dict(action_id=c['action_id'], record_id=c['record_id'], slot=c['slot'], reason=c.get('reason', ''))
              for c in kept]
    taken = set(skip) | {c['action_id'] for c in kept}
    people = {c['person_id'] for c in kept if c.get('person_id')}
    companies = {c['company_id'] for c in kept if c.get('company_id')}

    if plan.get('people_only'):
        for item in _pilot_candidates(pools, ctx):
            if len(chosen) >= size:
                break
            if item['id'] in taken or item.get('person_id') in people:
                continue
            slot = _slot(item, ctx)
            chosen.append(dict(action_id=item['id'], record_id=item['record_id'], slot=slot,
                               reason=_reason(item, slot, ctx)))
            taken.add(item['id'])
            people.add(item.get('person_id') or item['record_id'])
        return chosen

    def take(pool, slot, limit):
        for item in pool:
            if len(chosen) >= size or limit <= 0:
                return
            if item['id'] in taken or item.get('person_id') in people or item.get('company_id') in companies:
                continue
            chosen.append(dict(action_id=item['id'], record_id=item['record_id'], slot=slot, reason=_reason(item, slot, ctx)))
            taken.add(item['id'])
            people.update([item['person_id']] if item.get('person_id') else [])
            companies.update([item['company_id']] if item.get('company_id') else [])
            limit -= 1
    held = {slot: sum(c['slot'] == slot for c in kept) for slot in ('known', 'contract')}
    take(pools['known'], 'known', size - contract_n - held['known'])
    take(pools['contract'], 'contract', contract_n - held['contract'])
    take(pools['prospect'], 'prospect', size)
    take(pools['known'], 'known', size)
    take(pools['contract'], 'contract', size)
    order = {'known': 0, 'contract': 1, 'prospect': 2}
    chosen.sort(key=lambda c: order.get(c['slot'], 3))
    return chosen


def _pilot_candidates(pools, ctx):
    """One shared selection order for fresh weeks, refresh and replacement.

    Several chosen people may work at the same company: the pilot is about people,
    not a company quota. Existing referral/submission warnings still apply.
    """
    candidates = [a for pool in pools.values() for a in pool
                  if (ctx.records.get(a.get('person_id') or a['record_id']) or {}).get('type') == 'person'
                  and _priority(a, ctx) < 2]
    candidates.sort(key=lambda a: (_priority(a, ctx), _wave(a, ctx),
                                   TIERS.get(a.get('tier'), 5)))
    return candidates


def refreshed_week(intel, state, today=None, cfg=None):
    """This week's list chosen again from his list as it stands (Refresh from your list): items he has
    acted on keep their places and deferred ones stay out."""
    ctx = _Context(intel, state, today or london_today(), cfg or config())
    week = intel['week']
    by_id = {a['id']: a for a in intel['actions'] + intel['research']}
    kept = [dict(i, person_id=(by_id.get(i['action_id']) or {}).get('person_id'),
                 company_id=(by_id.get(i['action_id']) or {}).get('company_id'))
            for i in week['items'] if i.get('done')]
    return choose_week(_candidates(intel, ctx), ctx, kept, {i['action_id'] for i in week['deferred']})


def reason_for(intel, state, action_id, slot, today=None, cfg=None):
    """Why an item is on this week's list, worded as when it was chosen (for one that is put back)."""
    item = next((a for a in intel['actions'] if a['id'] == action_id), None)
    return _reason(item, slot, _Context(intel, state, today or london_today(), cfg or config())) if item else ''


def replacement(intel, slot, chosen_ids, today=None, state=None, cfg=None):
    """The next eligible item for `slot` that is not already in this week's list."""
    ctx = _Context(intel, state or {'records': [], 'outcomes': [], 'interactions': []}, today or london_today(), cfg or config())
    if state:
        records = {r['id']: r for r in state.get('records') or []}
        ctx.records = records
    by_id = {a['id']: a for a in intel['actions']}
    taken = [by_id[i] for i in chosen_ids if i in by_id]
    people = {a.get('person_id') for a in taken if a.get('person_id')}
    companies = {a.get('company_id') for a in taken if a.get('company_id')}
    pools = _candidates(intel, ctx)
    if ctx.cfg.get('plan', {}).get('people_only'):
        for item in _pilot_candidates(pools, ctx):
            if item['id'] not in chosen_ids and item.get('person_id') not in people:
                return dict(action_id=item['id'], record_id=item['record_id'], slot=_slot(item, ctx))
        return None
    order = [slot] + [s for s in ('known', 'contract', 'prospect') if s != slot]
    for name in order:
        for item in pools[name]:
            if item['id'] in chosen_ids or item.get('person_id') in people or (item.get('company_id') and item['company_id'] in companies):
                continue
            return dict(action_id=item['id'], record_id=item['record_id'], slot=name)
    return None
