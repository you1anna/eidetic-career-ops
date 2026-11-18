"""Read-only advice from recorded evidence; a playbook match is a hypothesis, not a diagnosis.

The projection does not write records, invent relationship warmth, or turn funding into pain.
Qualification remains owned by relevance. Outgoing drafts and approvals do not drive current advice.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
import re
from urllib.parse import urlsplit

from approach_history import ApproachHistory
from approaches import config as outreach_config
from relevance import generic_signals, qualification
import normalise
import profile_rules
import search_config
import outreach_routes

CONFIG = Path(__file__).resolve().parent.parent / 'config/problem_playbooks.yml'
_cache = {}
WARM = {'close', 'in_touch', 'known'}
RECRUITERS = ('recruiter', 'in_house_talent')
ACCESS_LABELS = {'close': 'Close network', 'in_touch': 'In touch', 'known': 'Known', 'connected': 'Connected', 'cold': 'Cold'}
ENGAGEMENTS = {'interim', 'contract', 'ftc', 'permanent', 'unknown'}
CLOSED = {'closed', 'rejected', 'skip', 'lost', 'declined', 'no_relevant_desk'}
ADVERTS = {'job', 'tender'}
COUNTEREVIDENCE = re.compile(
    r'not (?:a |an )?(?:product[ -]engineering|software[ -]engineering|engineering leadership)|'
    r'partner[ -]engineering role|not (?:a |an )?(?:relevant|suitable|good) (?:role|fit)|'
    r'no (?:current |open )?(?:leadership|engineering) (?:need|vacancy)|'
    r'role (?:has |is )closed|advert (?:has |is )closed', re.I)
# A senior title is a route to an advertised remit only when it owns engineering, quality, product or hiring.
EXECUTIVE = re.compile(r'\b(?:ceo|chief executive|founder|co-founder|chair\w*|president|coo|cfo|chief operating|'
                       r'chief financial|managing director|owner)\b', re.I)
REMIT_OWNER = re.compile(r'\b(?:cto|chief technology|engineer\w*|technolog\w*|software|platform|quality|qa|test\w*|'
                         r'ai|data|product|delivery|talent|recruit\w*|hiring)\b', re.I)
SPECIFIC_REMIT = re.compile(r'\b(?:build(?:ing|s)?|improv\w*|lead(?:ing|s)?|own(?:s|ing)?|migrat\w*|integrat\w*|recover\w*|deliver\w*|set guardrails|release readiness|incident rate|modernis\w*|moderniz\w*)\b', re.I)
COMPANY_CONTEXT_GAP = ('No current source establishes requested work or a specific delivery remit; '
                       'company activity and hiring alone are context.')
_FUNDING_ACTIVITY = re.compile(r'\b(?:funding|fundraise|raised|investment|investors?|series [a-f]|seed round)\b', re.I)
_HIRING_ACTIVITY = re.compile(r'\b(?:hiring|recruit\w*|vacanc\w*|job (?:advert|opening)|join (?:our|the) team)\b', re.I)
_NOT_REQUESTED = re.compile(r"\b(?:no|not|never|without|isn't|isn’t|don't|don’t|doesn't|doesn’t)\b.{0,65}\b(?:need\w*|requir\w*|request\w*|seek\w*|help|remit)\b|"
                           r'\b(?:not|never|no longer)\s+(?:\w+\s+){0,3}(?:planned|required|requested|needed|approved|scheduled|underway)\b|'
                           r'\b(?:cancelled|canceled|postponed|deferred)\b', re.I)
_HYPOTHETICAL_WORK = re.compile(r'\b(?:may|might|could|perhaps|potential(?:ly)?)\b.{0,65}\b(?:need\w*|requir\w*|request\w*|help|remit)\b', re.I)
_GENERIC_DEMAND = re.compile(r'\b(?:customers?|clients?|users?)\s+(?:need|want|require)|'
                            r'\b(?:teams?|customers?|clients?|users?)\s+(?:that|who)\s+(?:need|want|require)', re.I)
_PRODUCT_SUBJECT = re.compile(r'^\s*(?:our|their|this|the)\s+(?:[\w-]+\s+){0,4}'
                             r'(?:product|platform|service|solution|tool)\s+'
                             r'(?:supports?|helps?|enables?|allows?|offers?|provides?|delivers?|builds?|improves?|needs?)\b', re.I)
_MARKETING_METHOD = re.compile(r'\b(?:with|using|via|through)\s+(?:our|this|the)\s+(?:[\w-]+\s+){0,4}'
                              r'(?:product|platform|service|solution|tool)\b', re.I)
_REQUESTED_WORK = re.compile(r'\b(?:need(?:s)?|requir(?:e|es|ed)|request(?:s|ed)?|seek(?:s|ing)?|looking for)\b.{0,80}'
                             r'\b(?:help|support|expertise|engineer\w*|leadership|improv\w*|recover\w*|deliver\w*|migrat\w*|integrat\w*|evaluation|release)\b', re.I)
_NAMED_REMIT = re.compile(r'^(?:current|requested|agreed) (?:delivery )?(?:remit|work|scope):?\s*'
                         r'(?:improve|lead|own|migrate|integrate|recover|deliver|modernise|modernize|set guardrails)\b', re.I)
_TEAM_REMIT = re.compile(r'\b(?:building|establishing)\b.{0,45}\b(?:engineering|quality|software)\b.{0,25}'
                        r'\b(?:department|team|function|capability)\b.{0,45}\b(?:improve|release|incident|delivery)\b', re.I)
AI_CONTEXT = re.compile(r'\b(?:ai|artificial intelligence|llms?|language models?|machine learning|agentic|agents?|models?)\b', re.I)


def _config():
    mtime = CONFIG.stat().st_mtime_ns
    if _cache.get('mtime') != mtime:
        import yaml
        _cache.update(mtime=mtime, value=yaml.safe_load(CONFIG.read_text()))
    return _cache['value']


def _date(value):
    try:
        return dt.date.fromisoformat(str(value or '')[:10])
    except ValueError:
        return None


def _research_date(review):
    # An explicit source date governs evidence age, even when unknown or unusable.
    # Review/observation time is import chronology, not a refresh of historical evidence.
    # Omission retains the legacy page-review contract; new historical findings must supply
    # source_date (empty when unknown), because omitted dates cannot distinguish those cases.
    if 'source_date' not in review:
        return _date(review.get('reviewed_at') or review.get('observed_at'))
    try:
        return dt.datetime.fromisoformat(review['source_date']).date()
    except (TypeError, ValueError):
        return None


def _url(value):
    value = str(value or '')
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ('https', 'http') and parsed.hostname else ''
    except ValueError:
        return ''


def _advert_urls(adverts):
    return {url.rstrip('/') for advert in adverts for value in
            [*(advert.get(key) for key in ('url', 'detail_url', 'apply_url')),
             (advert.get('advert') or {}).get('url')] if (url := _url(value))}


def _text(value):
    # The same result as re.sub(r'\s+', ' ', ...).strip(): both use Python's one whitespace test.
    return ' '.join(str(value or '').split())


def _rel(record):
    return record.get('relevance') or {}


def _role(person):
    return person.get('role') or (person.get('linkedin') or {}).get('observed_role') or ''


def _company_name(record, company=None):
    if company and company.get('name'):
        return company['name']
    if record.get('type') in ('company', 'agency'):
        return record.get('name') or ''
    if record.get('organisation'):
        return record['organisation']
    li = record.get('linkedin') or {}
    return li.get('observed_company') or ''


def _reach(person):
    reach = _rel(person).get('reach') or {}
    tier = reach.get('tier')
    if tier in ACCESS_LABELS:
        return tier, reach.get('reason') or 'Relationship evidence recorded on this person.'
    # Explicit relationship state is meaningful. last_touch and one-way sends are not warmth.
    if person.get('relationship') == 'warm':
        return 'close', 'The owner recorded a warm relationship.'
    if person.get('relationship') == 'active':
        return 'in_touch', 'The owner recorded an active relationship.'
    return 'cold', 'No two-way relationship or personal statement is recorded.'


def _blocked_person(person, organisations):
    if person.get('relationship') == 'do_not_contact' or person.get('archived_at'):
        return True
    rules = profile_rules.current()
    leaver = rules.is_leaver(person.get('known_context'))
    linkedin = person.get('linkedin') or {}
    if isinstance(linkedin, list):
        linkedin = linkedin[0] if linkedin else {}
    names = ' '.join([person.get('organisation') or '', linkedin.get('observed_company') or '',
                      *(r.get('name') or '' for r in organisations)])
    current = rules.flagged(linkedin) or rules.at_employer(names)
    return bool(current and not leaver)


def _advert_eligible(record, today, config):
    q = _rel(record).get('qualification') or {}
    seen = _date(_rel(record).get('last_seen'))
    deadline = _date(record.get('published_deadline'))
    return (not record.get('archived_at') and record.get('status') not in CLOSED
            and q.get('eligible') is not False and q.get('domain') != 'outside'
            and record.get('source_lifecycle') not in ('closed', 'expired', 'removed', 'historical')
            and ((record.get('evidence_checks') or {}).get('vacancy') or {}).get('status') != 'closed'
            and (_rel(record).get('signal_band') or _rel(record).get('band')) != 'low' and not _rel(record).get('duplicate_of')
            and not (deadline and deadline < today)
            and not (seen and (today-seen).days > config['advert_seen_max_age_days']))


def _engagement(record):
    """Use qualified terms, including explicit unknowns, ahead of stale stored labels."""
    q = _rel(record).get('qualification') or {}
    if q.get('engagement') in ENGAGEMENTS:
        return q['engagement']
    advert = record.get('advert') or {}
    raw = dict(record, title=advert.get('title') or record.get('name') or '',
               text=advert.get('text') or advert.get('description') or advert.get('body') or record.get('summary') or '',
               employment_type=advert.get('employment_type') or advert.get('contract_type') or '')
    return qualification(raw)['engagement']


def _documents(record, today, generic, config):
    """Every useful source gets its own date. An updated record date is not a source-read date."""
    docs = []
    for review in record.get('research') or []:
        text = _text(review.get('signal') or review.get('mandate_evidence') or review.get('desk_summary') or review.get('summary'))
        if not text or review.get('signal') in generic:
            continue
        urls = review.get('source_urls') or []
        if isinstance(urls, str):
            urls = urls.split(';')
        date = _research_date(review)
        docs.append(dict(text=text, url=next((_url(u) for u in urls if _url(u)), ''),
                         date=date.isoformat() if date else '',
                         fresh=bool(date and 0 <= (today-date).days <= config['research_max_age_days'])))
    # Validated briefs outlive the three-recent-reviews window. Use their actual quoted source,
    # never generated wording, recorded summaries or a later maintenance date.
    for item in (record.get('evidence_brief') or {}).get('items', []):
        if item.get('provenance') != 'public_source' or not item.get('quote'):
            continue
        date = _date(item.get('at'))
        docs.append(dict(text=_text(item['quote']), url=_url(item.get('source_url')),
                         date=date.isoformat() if date else '',
                         fresh=bool(date and 0 <= (today-date).days <= config['research_max_age_days'])))
    vacancy = (record.get('evidence_checks') or {}).get('vacancy') or {}
    if record.get('type') in ADVERTS and vacancy.get('status') == 'confirmed':
        date = _date(vacancy.get('checked_at'))
        docs.append(dict(text=_text(vacancy.get('basis') or ''), url=_url(vacancy.get('source_url')),
                         date=date.isoformat() if date else '', kind='advert', record_id=record.get('id'),
                         name=record.get('name') or '',
                         fresh=bool(date and 0 <= (today-date).days <= config['advert_max_age_days'])))
    for signal in record.get('commercial_signals') or []:
        end = _date(signal.get('contract_end') or signal.get('expires'))
        if signal.get('signal_type') != 'contract_award' or signal.get('eligible') is not True or (end and end < today):
            continue
        # A notice updated today can describe a years-old award. Only the actual award date
        # supports a recent trigger; the read date never makes it a new buying event.
        date = _date(signal.get('award_date'))
        text = _text(f"Published contract award: {signal.get('title') or ''}. Buyer: {signal.get('buyer') or 'not stated'}. "
                     f"{signal.get('summary') or ''}")
        docs.append(dict(text=text, url=_url(signal.get('url') or signal.get('source_url')),
                         date=date.isoformat() if date else '', kind='contract_award',
                         fresh=bool(date and 0 <= (today-date).days <= config['award_max_age_days'])))
    if record.get('type') in ADVERTS:
        advert = record.get('advert') or {}
        text = advert.get('text') or advert.get('description') or advert.get('body') or record.get('body')
        date = _date(advert.get('observed_at') or _rel(record).get('last_seen') or record.get('date'))
        if not text:
            summary = normalise.clean(record.get('summary') or '')
            # Research can fill the record summary. It is still the same dated evidence,
            # not an independent advert refreshed by the record's date or a scanner sighting.
            research_summary = any(summary == normalise.clean(review.get(field) or '')
                                   for review in record.get('research') or []
                                   for field in ('desk_summary', 'summary'))
            if not research_summary:
                text = record.get('summary')
            # Only recent findings are projected. An earlier research source may have fallen
            # out of that window, so an unmatched summary with research has no reliable date.
            if record.get('research'):
                date = None
        text = _text(text)
        if text:
            docs.append(dict(text=text, url=_url(advert.get('url') or record.get('url')),
                             date=date.isoformat() if date else '', kind='advert', record_id=record.get('id'),
                             name=record.get('name') or '',
                             fresh=bool(date and 0 <= (today-date).days <= config['advert_max_age_days'])))
    return docs


@lru_cache(maxsize=8192)
def _contrary(text):
    return bool(COUNTEREVIDENCE.search(text))


def _requested_remit(text, cues):
    """A playbook cue describes a market, not demand. Require a sourced request or named remit.

    Sentence boundaries prevent funding/product prose from borrowing a request in another clause.
    Hiring is evidence for a vacancy, not a second direct engagement. Conservative non-matches
    stay available for qualification; neither a buyer nor a contract budget is inferred here.
    """
    if _NOT_REQUESTED.search(text):
        return False
    for sentence in re.split(r'[.;!?]+\s*', text):
        if (not SPECIFIC_REMIT.search(sentence) or _FUNDING_ACTIVITY.search(sentence)
                or _HIRING_ACTIVITY.search(sentence) or _HYPOTHETICAL_WORK.search(sentence)
                or _GENERIC_DEMAND.search(sentence) or _PRODUCT_SUBJECT.search(sentence)
                or not _cue_hits(sentence, cues)):
            continue
        # A bare imperative can be product marketing. An explicit requester may describe the
        # tools used, but an unowned remit fragment must be labelled and not a product promise.
        request = _REQUESTED_WORK.search(sentence)
        # "Requested remit:" labels the scope; it is not a requester asserting a need.
        asserted_request = request and bool(sentence[:request.start()].strip())
        requested = (asserted_request or
                     ((_NAMED_REMIT.search(sentence.strip()) or _TEAM_REMIT.search(sentence))
                      and not _MARKETING_METHOD.search(sentence)))
        if requested and qualification({'text': sentence})['domain'] != 'outside':
            return True
    return False


@lru_cache(maxsize=None)
def _cue_pattern(cue):
    return re.compile(r'(?<![a-z0-9])' + re.escape(cue) + r'(?![a-z0-9])', re.I)


def _count_cues(text, cues):
    return sum(bool(_cue_pattern(cue).search(text)) and (cue != 'guardrails' or bool(AI_CONTEXT.search(text)))
               for cue in cues)


# Remit sentences, which reach a cue check rarely (most fail the cheaper remit filters first).
@lru_cache(maxsize=8192)
def _cue_hits(text, cues):
    return _count_cues(text, cues)


# The same company evidence is matched once per company and again for each of its people, and
# every rebuild after a save matches it again. One entry holds a text's counts for every playbook,
# so this cache, like the other text caches here, needs one entry per distinct document. Keyed
# per playbook it needed four, and past 8,192 entries a rebuild missed on almost every lookup
# (3 November: 23,960 of 24,000 at 3,000 synthetic adverts, 0 of 8,000 at 1,000).
@lru_cache(maxsize=8192)
def _playbook_hits(text, playbooks):
    return tuple(_count_cues(text, cues) for cues in playbooks)


def _match(docs, config):
    """The best-supported playbook. Company evidence counts together, but each advert is its own
    remit: only the single best-matching advert supports a hypothesis (value gate, 10 October:
    separate teams' adverts were blended into one). A no-go advert never anchors one."""
    candidates = []
    company_docs = [d for d in docs if d.get('kind') != 'advert']
    adverts = [d for d in docs if d.get('kind') == 'advert' and not _no_go(d['text'])]
    playbooks = tuple(tuple(playbook['cues']) for playbook in config['playbooks'])
    for n, playbook in enumerate(config['playbooks']):
        supporting = []
        hits = 0
        for doc in company_docs:
            if _contrary(doc['text']):
                continue
            count = _playbook_hits(doc['text'], playbooks)[n]
            if count:
                supporting.append(doc)
                hits += count
        scored = [(_playbook_hits(d['text'], playbooks)[n], d['date'], d) for d in adverts if not _contrary(d['text'])]
        best = max(scored, key=lambda x: (x[0], x[1]), default=None)
        if best and best[0]:
            supporting.append(best[2])
            hits += best[0]
        if hits:
            candidates.append((hits, playbook, supporting))
    return max(candidates, key=lambda x: x[0]) if candidates else (0, None, [])


def _unique(values):
    return list(dict.fromkeys(t for t in map(_text, values) if t))


def _unique_documents(docs):
    """The same source prose at another URL is one fact; keep its freshest usable citation."""
    unique = {}
    for doc in docs:
        key = _text(doc['text']).casefold()
        previous = unique.get(key)
        rank = lambda d: (bool(d['url']), d['fresh'], d['date'], d.get('kind') in ('leadership_change', 'delivery_partner'))
        if previous is None or rank(doc) > rank(previous):
            unique[key] = doc
    return list(unique.values())


def _fact(doc, playbook):
    """Keep the brief scannable while preserving source wording around the relevant cue."""
    text = doc['text']
    if len(text) > 480:
        positions = [text.lower().find(cue) for cue in (playbook or {}).get('cues', []) if cue in text.lower()]
        start = max(0, min(positions, default=0) - 100)
        if start:
            start = text.find(' ', start) + 1
        end = min(len(text), start + 440)
        if end < len(text):
            end = text.rfind(' ', start, end)
        text = ('…' if start else '') + text[start:end] + ('…' if end < len(doc['text']) else '')
    return dict(text=text, url=doc['url'], date=doc['date'])



# Terms an advert states, kept as stated: each day rate keeps its own basis (value gate, 10 October:
# "£463/day PAYE and £537/day umbrella" were flattened into one range and asked about as unknown).
_NUMBERS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'nine': 9, 'twelve': 12, 'eighteen': 18}
_DURATION = re.compile(r'\b(\d{1,2}|one|two|three|four|five|six|nine|twelve|eighteen)[\s-]+months?\b', re.I)
_DAY_RATE = re.compile(r'(up to\s+)?£\s?(\d{3,4})(?:\s?(?:-|–|to)\s?£?\s?(\d{3,4}))?\s*(?:/|per|a)\s*day\b([^.;\n£]{0,45})', re.I)
_BASIS = re.compile(r'\b(paye|umbrella|outside ir35|inside ir35|limited company|ltd)\b', re.I)
_IR35 = re.compile(r'\b(inside|outside)\s+ir35\b', re.I)
_TERM = re.compile(r'\b(contract|interim|fixed[\s-]term|ftc|permanent|freelance|fractional)\b', re.I)
_WEEKLY = re.compile(r'\b(\d|one|two|three|four|five)\s+days?\s+(?:a|per)\s+week\b', re.I)
_CLEARANCE = re.compile(r'\b(?:sc|dv|nppv\d?|bpss)\b[\s-]*(?:clear\w*|vett\w*|eligib\w*)|security[\s-]clear\w*|\bclearable\b|'
                        r'eligible for (?:sc|dv)\b', re.I)
_ESSENTIAL = re.compile(r'\b(?:essential|must have|mandatory|is required|are required|is a must)\b', re.I)
_ONSITE = re.compile(r'\b(?:5|five)\s+days?\s+(?:a week\s+)?on[\s-]?site\b|\bfully on[\s-]?site\b', re.I)
CONTINGENT_TERMS = ('Contract', 'Interim', 'Fixed term', 'Fractional', 'Freelance')
_TERMS_UNKNOWN = re.compile(r'engagement|interim|contract help|its terms', re.I)


def _stated_terms(text):
    return list(_stated_terms_of(text or ''))


@lru_cache(maxsize=8192)
def _stated_terms_of(text):
    terms = []
    term = _TERM.search(text)
    if term:
        word = term.group(1).lower().replace('-', ' ')
        terms.append({'ftc': 'Fixed term', 'fixed term': 'Fixed term'}.get(word, word.capitalize()))
    duration = _DURATION.search(text)
    if duration:
        n = duration.group(1).lower()
        terms.append(f"{_NUMBERS.get(n, n)} months")
    bases = set()
    for m in _DAY_RATE.finditer(text):
        basis = _BASIS.search(m.group(4) or '')
        label = ''
        if basis:
            label = {'paye': 'PAYE', 'ltd': 'limited company'}.get(basis.group(1).lower(), basis.group(1).lower())
            label = label.replace('ir35', 'IR35')
            bases.add(label)
        amount = f"£{m.group(2)}" + (f"–{m.group(3)}" if m.group(3) else '')
        terms.append(f"{'up to ' if m.group(1) else ''}{amount}/day" + (f" {label}" if label else ''))
    ir35 = _IR35.search(text)
    if ir35 and f"{ir35.group(1).lower()} IR35" not in bases:
        terms.append(f"{ir35.group(1).lower()} IR35")
    weekly = _WEEKLY.search(text)
    if weekly:
        terms.append(weekly.group(0).lower())
    return tuple(dict.fromkeys(terms))


def _constraints(text):
    """What the remit demands that may rule it out or needs checking first."""
    return list(_constraints_of(text or ''))


@lru_cache(maxsize=8192)
def _constraints_of(text):
    out = []
    if _CLEARANCE.search(text):
        out.append('Requires security clearance or vetting')
    if _ONSITE.search(text):
        out.append('Five days a week on site')
    for sentence in re.split(r'(?<=[.!?])\s+', text):
        if _ESSENTIAL.search(sentence) and re.search(r'experience|knowledge|background|certif', sentence, re.I):
            out.append('Check: ' + sentence.strip()[:200])
    return tuple(dict.fromkeys(out))


@lru_cache(maxsize=8192)
def _no_go(text):
    """The owner's rule (commercial/rates.md): no defence or clearance roles; vetting outlasts the runway."""
    return bool(_CLEARANCE.search(text or ''))


class _Builder:
    def __init__(self, state, today):
        self.state, self.today, self.config = state, today, _config()
        self.records = {r['id']: r for r in state.get('records', []) if not r.get('archived_at')}
        self.excluded = search_config.exclusions(self.records.values(), state.get('links') or [])
        self.excluded.update({r['id']: r['exclusion'] for r in self.records.values() if r.get('exclusion')})
        self.routes = outreach_routes.Routes({rid: r for rid, r in self.records.items()
                                              if rid not in self.excluded and r.get('relationship') != 'do_not_contact'},
                                             state.get('links') or [])
        self.links = {rid: [] for rid in self.records}
        for link in state.get('links') or []:
            a, b = link.get('from_id'), link.get('to_id')
            if a in self.records and b in self.records:
                self.links[a].append((b, link.get('relation')))
                self.links[b].append((a, link.get('relation')))
        self.generic = generic_signals(self.records.values())
        self.history = ApproachHistory(state.get('decisions') or [], state.get('outcomes') or [])
        self.actions, self.research, self.seen_people = [], [], set()

    def linked(self, record, types, relations=None):
        return list({rid: self.records[rid] for rid, relation in self.links[record['id']]
                     if self.records[rid]['type'] in types and (relations is None or relation in relations)}.values())

    def people(self, company):
        people = self.linked(company, {'person'}, {'works_at', 'consultant_at', 'introducer', 'related'})
        introducers = [p for p in self.routes.people(company) if _reach(p)[0] in WARM]
        people = list({p['id']: p for p in [*people, *introducers]}.values())
        return [p for p in people if p['id'] not in self.excluded
                and not _blocked_person(p, self.linked(p, {'company', 'agency'}, {'works_at', 'consultant_at'}))
                and (_reach(p)[0] in WARM or p.get('kind') in ('recruiter', 'in_house_talent', 'introducer')
                     or re.search(r'\b(cto|ceo|founder|co-founder|chief|director|vp|head)\b', _role(p), re.I))]

    def content(self, record, adverts):
        docs = _documents(record, self.today, self.generic, self.config)
        docs.extend(outreach_routes.documents(record, self.today, outreach_config()))
        # A known vacancy remains vacancy evidence even after it closes or is skipped. A review
        # of its URL cannot turn it into a separate company engagement on the next projection.
        if record.get('type') in ('company', 'agency'):
            advert_urls = _advert_urls(self.linked(record, ADVERTS))
            for doc in docs:
                if _url(doc.get('url')).rstrip('/') in advert_urls:
                    doc['kind'] = 'advert_context'
        for advert in adverts:
            if advert['id'] in self.excluded:
                continue
            # Research/quoted evidence attached to this vacancy is still vacancy evidence when
            # its canonical source differs from the listing URL. Preserve that ownership before
            # combining it with company context; URL coincidence is not the only attribution.
            docs.extend(dict(doc, kind='advert', record_id=advert['id'], name=advert.get('name') or '')
                        for doc in _documents(advert, self.today, self.generic, self.config))
        # A repeated import or identical advert should not strengthen confidence.
        return _unique_documents(docs)

    def status(self, item, record, company):
        return self.history.apply(item, record)

    def make(self, record, company=None, person=None, route='direct', docs=None, adverts=None, conflicts=None, referral=False):
        docs, adverts, conflicts = docs or [], adverts or [], conflicts or []
        docs = [d for d in docs if d.get('kind') != 'leadership_change' or (person
                and d.get('person_id') == person['id'] and company
                and company['id'] in self.routes.employers.get(person['id'], ()))]
        docs = _unique_documents(docs)
        hits, playbook, supporting = _match(docs, self.config)
        advert_docs = [d for d in docs if d.get('kind') == 'advert']
        anchored = [d for d in supporting if d.get('kind') == 'advert']
        if not anchored and not playbook:
            anchored = sorted((d for d in advert_docs if not _no_go(d['text'])), key=lambda d: d['date'], reverse=True)[:1]
        nogo = bool(advert_docs) and not anchored and all(_no_go(d['text']) for d in advert_docs) \
            and not any(d.get('kind') != 'advert' for d in supporting)
        terms = [dict(remit=d['name'], url=d['url'], record_id=d.get('record_id'), terms=_stated_terms(d['text'])) for d in anchored]
        known_terms = any(set(x['terms']) & set(CONTINGENT_TERMS) or any('/day' in y or 'months' in y for y in x['terms'])
                          for x in terms)
        constraints = list(dict.fromkeys(c for d in (anchored or (advert_docs if nogo else [])) for c in _constraints(d['text'])))
        other_remits = list({d.get('record_id') or d['url']: dict(name=d['name'], url=d['url'], record_id=d.get('record_id'),
                                                                 terms=_stated_terms(d['text']), constraints=_constraints(d['text']))
                             for d in advert_docs if d not in anchored}.values())
        credible = [d for d in supporting if d['fresh'] and d['url']]
        awards = [d for d in docs if d.get('kind') == 'contract_award']
        advert_urls = _advert_urls(adverts)
        remit_docs = [d for d in credible if not d.get('kind')
                      and _url(d.get('url')).rstrip('/') not in advert_urls
                      and not _contrary(d['text']) and _requested_remit(d['text'], tuple((playbook or {}).get('cues') or []))]
        if record.get('type') not in ADVERTS:
            credible = [d for d in credible if d in remit_docs or d.get('kind') in
                        ('advert', 'contract_award', 'delivery_partner', 'leadership_change')]
        tier, reach_reason = _reach(person or record) if person or record.get('type') == 'person' else ('cold', 'No personal route is recorded.')
        evidence_record = record if record.get('type') in ADVERTS else (company or record)
        reviews = evidence_record.get('research') or []
        if evidence_record.get('type') == 'agency' and any(r.get('verification') == 'primary-read' for r in reviews):
            reviews = [r for r in reviews if r.get('verification') == 'primary-read']
        unknowns = [*((evidence_record.get('briefing') or {}).get('decisive_unknowns') or []),
                    *(x for review in reviews for x in review.get('decisive_unknowns') or [])]
        unknowns += conflicts
        if awards:
            unknowns += ['Identify the supplier’s delivery owner and current delivery phase',
                         'Confirm whether the supplier uses associates or subcontractors for this work',
                         'Confirm an external workstream, procurement requirements and commercial terms; award value is not available subcontract budget']
        if person and person.get('linkedin'):
            li = person['linkedin']
            if li.get('observed_company') or li.get('observed_role'):
                unknowns.append('Confirm their current role and employer; LinkedIn employment is a dated observation, not verification')
        if supporting and not credible:
            unknowns.append('Refresh and verify the source evidence before using this hypothesis')
        if not person and route == 'direct':
            unknowns.append('Identify the person who owns this problem or a verified route to them')
        if playbook:
            unknowns += playbook['unknowns']
        elif record.get('type') not in ADVERTS:
            unknowns.append('Find a specific existing engineering remit or problem before proposing help')
        context_only = (not remit_docs and not referral and route not in ('agency', 'recruiter')
                        and (company or record.get('type') in ('company', 'opportunity'))
                        and record.get('type') not in ADVERTS)
        if context_only:
            unknowns.insert(0, COMPANY_CONTEXT_GAP)
        stories = (company or record).get('signals') or []
        recent_stories = [s for s in stories if (d := _date(s.get('published'))) and 0 <= (self.today-d).days <= 120]
        supporting_urls = {d['url'] for d in credible or supporting if d['url']}
        supported_adverts = [a for a in adverts if _url((a.get('advert') or {}).get('url') or a.get('url')) in supporting_urls]
        agency_doc = next((d for d in docs if d.get('kind') != 'advert' and d['fresh'] and not _contrary(d['text'])), None) if route == 'agency' else None
        if (route == 'agency' and adverts) or supported_adverts or (adverts and not playbook):
            anchor_ids = [d.get('record_id') for d in anchored]
            current_advert = adverts[0] if route == 'agency' else (supported_adverts or [a for a in adverts if a.get('id') in anchor_ids] or adverts)[0]
            stated = next((x['terms'] for x in terms if x['record_id'] == current_advert.get('id')), [])
            if route == 'agency' and not stated:
                eng = _engagement(current_advert)
                stated = [v for v in ({'ftc': 'Fixed term'}.get(eng, eng.capitalize()) if eng != 'unknown' else '',
                                      current_advert.get('pay') or '', current_advert.get('ir35') or '') if v]
            why_now = (f"A relevant advertised remit: {current_advert.get('name') or 'engineering work'} ({', '.join(stated[:4])})."
                       if stated else f"A relevant advertised remit: {current_advert.get('name') or 'engineering work'}. Engagement terms still need confirmation.")
            trigger = dict(kind='advert', text=f"{current_advert.get('name') or 'A relevant role'} is advertised",
                           record_id=current_advert.get('id'))
        elif awards:
            award_date = max((d['date'] for d in awards), default='')
            timing = f"dated {award_date}" if award_date else 'with no stated award date'
            why_now = f"A published contract award {timing} identifies a supplier to research. Current delivery needs and subcontract demand are unconfirmed."
            trigger = dict(kind='award', text=f"They won a public contract {timing}")
        elif credible:
            why_now = f"A specific remit was recorded on {max(d['date'] for d in credible)}; confirm it is still current."
            trigger = dict(kind='remit', text=_fact(credible[0], playbook)['text'])
        elif agency_doc:
            why_now = _fact(agency_doc, playbook)['text']
            trigger = dict(kind='desk', text=why_now)
        elif recent_stories:
            why_now = 'Recent funding is a reason to check timing; it does not establish an engineering problem or buying intent.'
            trigger = dict(kind='funding', text=recent_stories[0].get('headline') or 'They have raised money recently')
        elif referral:
            why_now = 'The recorded relationship provides a route to an informed referral; no company trigger is assumed.'
            trigger = None
        else:
            why_now = 'No current, specific buying trigger is confirmed.'
            trigger = None
        fact_docs = ([*credible, *supporting, *(d for d in docs if _contrary(d['text']))]
                     if playbook else [d for d in docs if d.get('kind') != 'advert'] + anchored)
        facts = [_fact(d, playbook) for d in _unique_documents(fact_docs) if d['url']][:5]
        problem_ready = bool(credible and not conflicts)
        ready = problem_ready or referral
        if route in ('agency', 'opportunity', 'recruiter') and record.get('type') not in ADVERTS and not conflicts:
            ready = True  # These are explicit qualification/referral moves, not diagnosed client needs.
        if record.get('type') in ADVERTS:
            chosen_job = record.get('type') == 'job' and record.get('status') in ('pursue', 'applied', 'responded', 'interview', 'offer')
            ready = bool((any(d['fresh'] and d['url'] for d in docs) or chosen_job) and not conflicts)
        if known_terms:
            # Stated terms answer the engagement questions; asking them again wastes the conversation.
            unknowns = [u for u in unknowns if not _TERMS_UNKNOWN.search(u)]
        gap = self.contact_gap(person, adverts) if person and route == 'direct' and tier not in WARM and not referral else None
        if gap:
            unknowns.insert(0, gap)
            ready = False
        if referral:
            hypothesis = 'This is a referral route, not evidence that this person or their employer has an engineering problem.'
            help_text = 'Describe the kinds of existing-system delivery, quality or AI-evaluation problems the owner can help qualify, and ask who actually owns one.'
            question = 'Who do you know with an existing engineering or AI-delivery problem who would value an experienced pair of hands or interim leadership?'
        elif playbook:
            hypothesis, help_text, question = playbook['hypothesis'], playbook['help'], playbook['question']
        else:
            hypothesis = ('Funding indicates timing, not a diagnosed problem.' if recent_stories else
                          'No specific engineering problem is supported by the current evidence.')
            help_text = 'First establish an existing problem and its owner; choose an intervention only when the evidence supports it.'
            question = 'What existing engineering outcome needs help, who owns it, and is an external engagement appropriate?'
        if awards and not referral:
            hypothesis = ('The awarded delivery scope may contain a useful workstream for the owner. ' + hypothesis
                          + ' The award does not establish a capacity gap or demand for subcontracting.')
            question = 'Who owns delivery of this contract, and do you use associates or subcontractors for a bounded integration, evaluation or release-evidence workstream?'
        if route == 'agency':
            hypothesis = 'This desk may route a relevant paid engagement; its client demand and terms need qualification.'
            help_text = 'Brief the desk on the relevant engineering or QE remit, confirm its live mandates and terms, and use the CV for that remit.'
            question = 'Which current contract or interim briefs need this experience, and who controls the shortlist and terms?'
        elif route == 'recruiter':
            hypothesis = 'This recruiter may place relevant contract or interim work; their current briefs and terms need qualification.'
            help_text = 'Tell them you are available for contract or interim work and which remits you cover, then use the CV for the brief that fits.'
            question = 'Which current contract or interim briefs need this experience, and who controls the shortlist and terms?'
        proof = dict(playbook['proof']) if playbook else dict(self.config['playbooks'][-1]['proof'])
        recommendation = ('Use the existing relationship to ask for a specific introduction or inside view.' if tier in WARM else
                          'Qualify the recognised remit and engagement route with the named decision-maker before proposing a scope.')
        if route == 'agency':
            recommendation = 'Qualify this desk’s current relevant briefs and terms, then choose the matching CV.'
        elif route == 'recruiter':
            recommendation = 'Tell them you are available, ask which current briefs fit, then choose the matching CV.'
        elif route == 'opportunity':
            recommendation = 'Qualify this recorded opportunity against its decision owner, scope and engagement terms.'
        if not ready:
            recommendation = 'Resolve the decisive evidence gap before qualifying this opportunity.'
        if awards and not referral:
            recommendation = ('Qualify the supplier’s delivery owner, current workstream and associate/subcontract route before proposing help.' if ready else
                              'Research the supplier’s delivery lead, current delivery phase and associate/subcontract policy before preparing an approach.')
        alternatives = [dict(label='Qualify the role', reason='An advertised position may be the useful route; an advert does not prove demand for consulting.'),
                        dict(label='Hold', reason='Use this when no confirmed problem, buyer or suitable engagement emerges.')]
        if awards and not adverts:
            alternatives[0] = dict(label='Explore a delivery partnership', reason='An awarded supplier may route a bounded workstream; confirm its use of external specialists.')
        if tier in WARM:
            alternatives.insert(0, dict(label='Ask for an inside view', reason='Check ownership and current need before a direct commercial approach.'))
        elif person:
            alternatives.insert(0, dict(label='Verify a warm route', reason='Use an evidenced introducer if one exists; a stored name alone is not a relationship.'))
        item = dict(id=f"action:{record['id']}:{company['id'] if company else ''}", record_id=record['id'],
                    company_id=company['id'] if company else record['id'] if record.get('type') in ('company', 'agency') else None,
                    person_id=person['id'] if person else record['id'] if record.get('type') == 'person' else None,
                    title=('Ask for a useful referral' if referral else 'Qualify ' + (playbook['label'].lower() if playbook else 'the next opportunity')),
                    person=(person or {}).get('name') or (record.get('name') if record.get('type') == 'person' else ''),
                    company=_company_name(record, company),
                    role=_role(person or record), route=route, stage='ready' if ready else 'research',
                    fit=dict(label=playbook['label'] if playbook else 'Needs qualification',
                             reason='A specific sourced remit supports this hypothesis.' if credible else 'No current source establishes a specific client need.'),
                    access=dict(label=ACCESS_LABELS[tier], reason=reach_reason),
                    confidence=dict(label='Supported hypothesis' if problem_ready else 'Needs verification',
                                    reason='The remit is sourced; pain, buying intent and terms remain hypotheses.' if problem_ready else
                                    'Missing, stale or conflicting evidence must be resolved before proposing a client intervention.'),
                    readiness=dict(label='Qualify first' if ready else 'Research needed', reason='The route can support a useful qualification question.' if ready else 'Evidence or a named route is incomplete.'),
                    brief=dict(facts=facts, terms=terms, constraints=constraints, other_remits=other_remits,
                               hypothesis=hypothesis, help=help_text, proof=proof, question=question,
                               unknowns=_unique(unknowns), recommendation=recommendation, alternatives=alternatives,
                               why_now=why_now, expected_result=playbook['expected_result'] if playbook else
                               'A confirmed problem, owner and engagement route, or a clear reason to hold.'))
        # Rank evidence and access, never the quantity of prepared prose or repeated keywords.
        # The configurable source/contact weights beat a generic historical referral, while
        # an explicitly close relationship remains a valuable route in its own right.
        weights = self.config['priority']
        priority = [dict(factor='access', points=weights['access'][tier], text=f'Relationship evidence: {ACCESS_LABELS[tier]}')]
        if problem_ready and not referral:
            priority.append(dict(factor='evidence', points=weights['sourced_remit'], text='Current sourced remit supports a specific qualification question'))
        if not referral and person and company and (
                person.get('kind') in ('recruiter', 'in_house_talent')
                or re.search(r'\b(cto|ceo|founder|co-founder|chief|director|vp|head)\b', _role(person), re.I)):
            priority.append(dict(factor='contact', points=weights['named_contact'], text='Named contact in a relevant leadership or recruiting role'))
        terms_adverts = supported_adverts or (adverts if not playbook or record.get('type') in ADVERTS else [])
        terms = [_engagement(a) for a in terms_adverts if not (_rel(a).get('qualification') or {}).get('conflicts')]
        if terms:
            term = max(terms, key=lambda t: (weights['engagement'][t], t))
            priority.append(dict(factor='engagement', points=weights['engagement'][term], text=f'Qualified engagement: {term}'))
        item['priority'] = dict(score=sum(r['points'] for r in priority), reasons=priority)
        item.update(tier=tier, trigger=trigger, adverts=len(adverts))
        item['work_evidence'] = dict(specific=bool(remit_docs and not conflicts),
                                    date=max((d['date'] for d in remit_docs), default=''))

        if record.get('type') not in ADVERTS and route not in ('agency', 'recruiter'):
            entry = self.routes.choose(company or record, person, tier,
                                       [*anchored, *(d for d in docs if d.get('kind') != 'advert')],
                                       adverts, _engagement, _no_go)
            if entry:
                # The route refines the same approach. Readiness, exclusions, exact approvals and
                # outcome history remain governed by the existing shared rules.
                item['brief']['approach_route'] = entry
                item['brief']['why_now'] = entry['why']
                item['brief']['question'] = entry['question']
                item['brief']['help'] = entry['help'] + ' ' + help_text
                current_unknowns = item['brief']['unknowns']
                item['brief']['unknowns'] = _unique([*entry['unknowns'], *current_unknowns] if ready else
                                                  [*current_unknowns, *entry['unknowns']])
                item['brief']['facts'] = [_fact(d, playbook) for d in _unique_documents(
                    [*entry['evidence'], *fact_docs]) if d.get('url')][:5]
                entry['evidence'] = [_fact(d, playbook) for d in entry['evidence']]
                if ready:
                    item['brief']['recommendation'] = entry['help']
                if entry['kind'] == 'interim_cover':
                    item['brief']['alternatives'][0] = dict(label='Keep the vacancy as a company signal',
                        reason='The advertised role is its own opportunity; a permanent vacancy does not prove demand for cover.')
                item['title'] = entry['label']
                if not item['trigger']:
                    item['trigger'] = dict(kind=entry['kind'], text=entry['why'])
        if context_only:
            # A warm route can make contact possible; it cannot resolve whether there is work.
            item['brief']['unknowns'] = _unique([COMPANY_CONTEXT_GAP, *item['brief']['unknowns']])
        if record.get('type') in ADVERTS:
            q = _rel(record).get('qualification') or {}
            item['title'] = f"Qualify {record.get('name') or 'the advertised role'}"
            item['role'] = record.get('name') or ''
            if q.get('domain') == 'unknown':
                item['confidence'] = dict(label='Confirm the remit', reason='The available advert does not establish a software-engineering remit. Qualify that before proceeding.')
                item['brief']['unknowns'].insert(0, 'Confirm that this is a software-engineering remit')
            terms = _engagement(record)
            item['brief']['recommendation'] = f"Qualify this advertised {terms} role against its remit, decision owner and terms; do not assume a separate consulting budget."
            if not ready:
                item['brief']['recommendation'] = 'Refresh the advert and resolve its evidence gaps before pursuing this role.'
        item = self.status(item, record, company)
        if nogo and not item.get('last_outcome'):
            item['stage'] = 'held'
            item['readiness'] = dict(label='Outside your constraints',
                                     reason='The remit requires security clearance or vetting, which you have ruled out.')
            item['brief']['recommendation'] = 'Leave this remit: it requires security clearance or vetting.'
        return item

    def order(self, item):
        """Priority, then the record's own relevance, then the newest dated evidence, then the name:
        ties no longer fall back to arbitrary identifiers (154 moves tied at 50 points on 12 October)."""
        record = self.records.get(item['record_id']) or {}
        newest = max((f.get('date') or '' for f in item['brief'].get('facts') or []), default='')
        newest = -int(newest[:10].replace('-', '')) if re.fullmatch(r'\d{4}-\d{2}-\d{2}.*', newest) else 0
        name = (item.get('person') or item.get('company') or item.get('title') or '').casefold()
        return (-item['priority']['score'], -(_rel(record).get('score') or 0), newest, name, item['id'])

    def contact_gap(self, person, adverts):
        """Why a named person you don't know is not yet a usable contact, or None."""
        seen = _date(person.get('date')) or _date(person.get('created_at'))
        limit = outreach_config().get('contact_max_age_days', 90)
        if (person.get('verification_level') != 'primary-read' or not person.get('evidence_urls')
                or not seen or not 0 <= (self.today - seen).days <= limit):
            return "Confirm their current role on the company's own site"
        role = _role(person)
        if adverts and EXECUTIVE.search(role) and not REMIT_OWNER.search(role):
            return 'Find who owns this remit, or use the advertised recruitment route; a senior name is not a route to it'
        return None

    def add(self, item, research=False):
        (self.research if research else self.actions).append(item)
        if not research and item.get('person_id'):
            self.seen_people.add(item['person_id'])

    def build(self):
        contexts = []
        for company in self.records.values():
            if company['type'] not in ('company', 'agency') or company.get('status') in CLOSED or company['id'] in self.excluded:
                continue
            agency = company['type'] == 'agency'
            all_adverts = self.linked(company, ADVERTS, {'hiring_for', 'advertised_by', 'related'})
            adverts = [r for r in all_adverts if r['id'] not in self.excluded and _advert_eligible(r, self.today, self.config)]
            if agency:
                adverts = [r for r in adverts if not (_rel(r).get('qualification') or {}).get('conflicts')
                           and ((_rel(r).get('score') or 0) >= 35
                                or ('score' not in _rel(r) and (_rel(r).get('signal_band') or _rel(r).get('band')) in ('strong', 'possible')))]
                adverts.sort(key=lambda r: (_engagement(r) not in ('contract', 'interim', 'ftc'), -(_rel(r).get('score') or 0), r['id']))
            own_docs = self.content(company, [])
            # A company's only observed remit can be conclusively irrelevant; do not resurrect it.
            if (all_adverts and not adverts and not _match(own_docs, self.config)[1]
                    and not any(d.get('kind') == 'contract_award' for d in own_docs)
                    and not (agency and any(d['fresh'] and not _contrary(d['text']) for d in own_docs))):
                continue
            docs = self.content(company, adverts)
            if docs and all(_contrary(d['text']) for d in docs):
                continue
            rel = _rel(company)
            people = self.people(company)
            if agency:
                relevant_desk = bool(re.search(r'quality|\bqe\b|engineering|interim|fractional|\bai\b',
                                              ' '.join([*(company.get(k) or '' for k in ('subtype', 'summary')),
                                                        *(d['text'] for d in own_docs)]), re.I))
                verified_desk = any(r.get('verification') == 'primary-read' for r in company.get('research') or [])
                if not adverts and not (relevant_desk and (rel.get('band') in ('strong', 'possible')
                                                           or company.get('status') in ('on_books', 'called_brief_received')
                                                           or verified_desk or bool(people))):
                    continue
            elif not (rel.get('approach') or rel.get('funded') or adverts or _match(docs, self.config)[1]
                      or any(d.get('kind') == 'contract_award' for d in docs)):
                continue
            conflicts = [] if agency else [str(c) for r in adverts for c in (_rel(r).get('qualification') or {}).get('conflicts') or []]
            conflicts += ['Contrary source finding: ' + _fact(d, None)['text'] for d in (own_docs if agency else docs) if _contrary(d['text'])]
            contexts.append((company, agency, adverts, docs, conflicts, people))
        for company, agency, adverts, docs, conflicts, people in contexts:
            if not people:
                item = self.make(company, route='agency' if agency else 'direct', docs=docs, adverts=adverts, conflicts=conflicts)
                self.add(item, research=item['stage'] == 'research')
            else:
                company_item = self.make(company, route='agency' if agency else 'direct', docs=docs, adverts=adverts, conflicts=conflicts)
                if company_item['work_evidence']['specific'] or company_item['stage'] not in ('ready', 'research'):
                    # Reduced source confidence cannot remove a canonical historical hold or
                    # outcome and silently replace it with a person's different approach.
                    self.add(company_item, research=company_item['stage'] == 'research')
                for person in people:
                    tier = _reach(person)[0]
                    route = 'agency' if agency else 'close_network' if tier == 'close' else 'known_contact' if tier in WARM else 'direct'
                    item = self.make(person, company, person, route, docs, adverts, conflicts)
                    self.add(item, research=item['stage'] == 'research')
        for record in self.records.values():
            if record['id'] in self.excluded:
                continue
            if record['type'] == 'person' and record['id'] not in self.seen_people:
                if _blocked_person(record, self.linked(record, {'company', 'agency'}, {'works_at', 'consultant_at'})):
                    continue
                tier = _reach(record)[0]
                if record.get('kind') in RECRUITERS and (tier in WARM or record.get('priority')):
                    # A recruiter the owner knows or chose, with no agency desk here: approached like a desk.
                    self.add(self.make(record, person=record, route='recruiter'))
                elif tier in WARM:
                    route = 'close_network' if tier == 'close' else 'known_contact'
                    self.add(self.make(record, person=record, route=route, referral=True))
                elif record.get('priority'):
                    # Someone on the owner's list he doesn't know: the same evidence and contact rules as any
                    # direct approach, so it waits in the assistant's research until they are met.
                    item = self.make(record, person=record, route='direct')
                    self.add(item, research=item['stage'] == 'research')
            elif record['type'] == 'opportunity' and record.get('stage') not in CLOSED:
                if not (record.get('summary') or record.get('next_action')) or (_rel(record).get('qualification') or {}).get('eligible') is False:
                    continue
                docs = _documents(record, self.today, self.generic, self.config)
                # An explicit manual opportunity is allowed to start as a qualification move.
                if not docs and record.get('summary'):
                    docs = [dict(text=_text(record['summary']), url='', date='', fresh=False)]
                if any(_contrary(d['text']) for d in docs):
                    continue
                self.add(self.make(record, route='opportunity', docs=docs))
            elif record['type'] in ADVERTS and (_rel(record).get('band') in ('strong', 'possible')
                                                or record.get('status') in ('pursue', 'applied', 'responded', 'interview', 'offer')):
                chosen_job = record.get('type') == 'job' and record.get('status') in ('pursue', 'applied', 'responded', 'interview', 'offer')
                if not chosen_job and not _advert_eligible(record, self.today, self.config):
                    continue
                docs = self.content(record, [])
                conflicts = list((_rel(record).get('qualification') or {}).get('conflicts') or [])
                conflicts += ['Contrary source finding: ' + _fact(d, None)['text'] for d in docs if _contrary(d['text'])]
                organisations = self.linked(record, {'company', 'agency'}, {'hiring_for', 'advertised_by'})
                item = self.make(record, company=organisations[0] if organisations else None,
                                 route='opportunity', docs=docs, adverts=[record], conflicts=conflicts)
                self.add(item, research=item['stage'] == 'research')
        for items in (self.actions, self.research):
            items.sort(key=self.order)
        counts = self.history.counts
        return dict(actions=self.actions, research=self.research,
                    eligible_advert_ids=[r['id'] for r in self.records.values() if r.get('type') in ADVERTS
                                         and r['id'] not in self.excluded and _advert_eligible(r, self.today, self.config)],
                    outcomes_summary=dict(counts=dict(counts), total=sum(counts.values())))


def build(state, today=None):
    """Return action and agent-research briefs after relevance has been attached to records."""
    today = today or dt.date.today()
    if isinstance(today, str):
        today = dt.date.fromisoformat(today)
    return _Builder(state, today).build()
