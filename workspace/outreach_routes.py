"""Evidence for ways into a conversation, separate from lifecycle and problem diagnosis.

An explicit channel is not a mandate. These routes refine an existing brief; they never create
an application, infer relationship warmth, or replace the shared contact/qualification gates.
"""
import datetime as dt
import re
from urllib.parse import urlsplit

import normalise

LABELS = {'interim_cover': 'Interim cover while hiring', 'new_leader': 'A new leader’s first 90 days',
          'delivery_partner': 'Delivery partner', 'warm_introduction': 'Warm introduction'}
LEADERSHIP = re.compile(r'\b(?:head|director|vp|vice president|chief|cto|leadership|engineering manager)\b', re.I)


def day(value):
    try:
        return dt.date.fromisoformat(str(value or '')[:10])
    except ValueError:
        return None


def recent(value, today, limit):
    date = day(value)
    return bool(date and 0 <= (today - date).days <= limit)


def validate_signals(review):
    """Optional typed evidence on the existing research observation, validated before any write."""
    if 'outreach_signals' not in review:
        return
    signals = review['outreach_signals']
    if not isinstance(signals, list) or len(signals) > 10:
        raise ValueError('outreach_signals must be a list of at most ten sourced facts')
    for signal in signals:
        if not isinstance(signal, dict) or signal.get('kind') not in ('leadership_change', 'delivery_partner'):
            raise ValueError('Choose leadership_change or delivery_partner for an outreach signal')
        allowed = {'kind', 'text', 'url', 'person_id', 'started_on'} if signal['kind'] == 'leadership_change' else {
            'kind', 'text', 'url', 'accepts_specialists'}
        if set(signal) - allowed:
            raise ValueError('Unsupported outreach signal fields')
        text, url = signal.get('text'), signal.get('url')
        if not isinstance(text, str) or not text.strip() or len(text) > 2000 or '\x00' in text:
            raise ValueError('An outreach signal needs a short sourced fact')
        if not isinstance(url, str) or url not in (review.get('source_urls') or []):
            raise ValueError('An outreach signal URL must be one of its reviewed source_urls')
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('An outreach signal needs a complete public source URL')
        if review.get('verification') != 'primary-read':
            raise ValueError('Outreach signals require first-party evidence (primary-read)')
        if signal['kind'] == 'leadership_change':
            person = signal.get('person_id')
            if not isinstance(person, str) or not person.strip() or len(person) > 200:
                raise ValueError('A leadership change needs the existing person_id')
            started = signal.get('started_on')
            if not isinstance(started, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', started) or not day(started):
                raise ValueError('A leadership change needs its actual started_on date (YYYY-MM-DD)')
        elif type(signal.get('accepts_specialists')) is not bool:
            raise ValueError('A delivery partner needs an explicit accepts_specialists true or false')


def documents(record, today, cfg):
    """Newest explicitly supplied signal set wins; an empty set withdraws earlier route claims.

    Legacy observations without the field do not overwrite it. Re-reading an old announcement
    cannot make a leader new again, and missing source dates never acquire a fresh read date.
    """
    reviews = [r for r in record.get('research') or [] if 'outreach_signals' in r]
    if not reviews:
        return []
    clock = lambda r: normalise.observation_time(r.get('reviewed_at') or r.get('observed_at'))
    newest = max(map(clock, reviews))
    latest = [r for r in reviews if clock(r) == newest]
    review = latest[0]
    if any(r['outreach_signals'] != review['outreach_signals'] for r in latest[1:]):
        return []  # Equal-time contradictory claims cannot choose a winner by import order.
    try:
        validate_signals(review)
    except (TypeError, ValueError):
        return []  # Old/imported malformed evidence cannot break the rest of the workspace.
    limit = cfg.get('route_evidence_max_age_days', 90)
    if not recent(review.get('reviewed_at'), today, limit) or not recent(review.get('source_date'), today, limit):
        return []
    result = []
    for signal in review['outreach_signals']:
        if signal['kind'] == 'leadership_change':
            if not recent(signal['started_on'], today, cfg.get('new_leader_max_days', 90)):
                continue
        elif signal['accepts_specialists'] is not True:
            continue
        result.append(dict(signal, date=day(review['source_date']).isoformat(), fresh=True))
    return result


class Routes:
    def __init__(self, records, links):
        self.records = records
        self.employers, self.introducers = {}, {}
        for link in links:
            a, b = link.get('from_id'), link.get('to_id')
            if a not in records or b not in records:
                continue
            if link.get('relation') == 'works_at':
                # Employment links have historically been readable in either direction.
                for person, company in ((a, b), (b, a)):
                    if records[person]['type'] == 'person' and records[company]['type'] == 'company':
                        self.employers.setdefault(person, set()).add(company)
        for link in links:
            a, b = link.get('from_id'), link.get('to_id')
            if link.get('relation') != 'introducer' or a not in records or b not in records or records[a]['type'] != 'person':
                continue
            # Direction is meaningful: A is the recorded introducer to B, never vice versa.
            companies = {b} if records[b]['type'] == 'company' else self.employers.get(b, set())
            for company in sorted(companies):
                self.introducers.setdefault(company, {}).setdefault(a, []).append(b)

    def people(self, company):
        return [self.records[rid] for rid in self.introducers.get(company['id'], {})]

    def choose(self, company, person, tier, docs, adverts, engagement, no_go):
        if not company or company.get('type') != 'company':
            return None
        cid, pid = company['id'], (person or {}).get('id')
        target = self.introducers.get(cid, {}).get(pid, [])
        if target and tier in ('close', 'in_touch', 'known'):
            people = sorted((self.records[t] for t in target if self.records[t]['type'] == 'person'),
                            key=lambda p: (p.get('name', '').casefold(), p['id']))
            name = people[0]['name'] if people else None
            return self.route('warm_introduction',
                why=f"You know {person['name']}, and an introduction route to {name or company['name']} is recorded. Their willingness still needs checking.",
                question=f"Would you be comfortable introducing me to {name} to understand their current priorities?" if name else
                         f"Could you help me find who owns this remit at {company['name']}, and introduce us if it seems useful?",
                help='Use the recorded relationship to ask for permission and an introduction before proposing work.',
                unknowns=['Whether the introducer is comfortable making this introduction', 'Whether the recipient recognises a need and wants outside help'],
                target_id=people[0]['id'] if people else cid, evidence=[])
        for doc in docs:
            if doc.get('kind') == 'leadership_change' and doc.get('person_id') == pid and cid in self.employers.get(pid, ()):
                return self.route('new_leader',
                    why=f"{person['name']} started in the recorded leadership role on {doc['started_on']}. Their first 90 days are a reason to ask about priorities, not evidence of a problem or budget.",
                    question='Which engineering outcome matters most in your first 90 days, and would a bounded piece of outside help be useful?',
                    help='Start with the new leader’s priorities and agree one useful outcome before discussing a scope.',
                    unknowns=['The priorities the new leader actually owns', 'Whether outside help is wanted and who can agree it'], evidence=[doc])
        partner = next((d for d in docs if d.get('kind') == 'delivery_partner'), None)
        if partner:
            return self.route('delivery_partner',
                why='Their own source describes a route for external specialists. This establishes a channel, not a live mandate or available budget.',
                question='Which current workstream needs an external engineering or QE specialist, who owns delivery, and how do associates join it?',
                help='Qualify a bounded delivery workstream, its owner and the associate or subcontract terms before offering help.',
                unknowns=['A current workstream with a delivery owner', 'Supplier onboarding, engagement terms and available budget'], evidence=[partner])
        return None

    @staticmethod
    def route(kind, **values):
        return dict(kind=kind, label=LABELS[kind], **values)
