"""Derived relevance: how much each record deserves the owner's attention, and why.

Computed on read from recorded facts and `config/relevance.yml`. Nothing here is stored, and it
never moves a working state, stage, status or relationship. Every point comes with a reason a
person can check. An unstated fact is neutral: a missing rate is not a low rate.
"""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict
import hashlib
import json
import threading
from functools import lru_cache
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import normalise
import profile_rules
import states
import search_config

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from job_rules import qualification, requirements, requirement_phrase, general_competency, role_action, title_equivalent, title_veto, word
CONFIG = ROOT / 'config/relevance.yml'
_cache: dict = {}
# Pure read-time results only: source/proof changes alter the digest. Serialized values bound
# retained memory, cannot be mutated by a response, and never retain whole advert bodies.
_FIT_CACHE = OrderedDict()
_FIT_MAX_ENTRIES = 4096
_FIT_MAX_BYTES = 8 * 1024 * 1024
_FIT_BYTES = 0
_FIT_LOCK = threading.Lock()

ADVERT_TYPES = {'job', 'tender'}
ORG_TYPES = {'company', 'agency'}
OPEN_STAGES = {'lead', 'contacted', 'conversation', 'proposal', None, ''}


def experience_phrase(label: str) -> str:
    """A proof label inside a sentence: lower its first letter unless it starts an acronym."""
    return label[:1].lower() + label[1:] if label[1:2].islower() else label


def _proofs():
    # Reuse the existing configuration reader and the proof owner used by intelligence. Its
    # prospecting cues are not qualifications; only explicit bounded capability mappings apply.
    return search_config._read('problem_playbooks.yml').get('playbooks') or []


@lru_cache(maxsize=256)
def _fit_pattern(pattern):
    return re.compile(pattern, re.I)


def _source_url(value):
    try:
        parsed = urlsplit(value or '')
        return value if parsed.scheme in ('https', 'http') and parsed.hostname and not (parsed.username or parsed.password) else ''
    except (TypeError, ValueError):
        return ''


def _fit_sources(record):
    """Bounded source wording, never a model's unsupported assessment or prepared wording."""
    advert = record.get('advert') or {}
    body = advert.get('text') or advert.get('description') or advert.get('body') or record.get('summary') or ''
    result = []
    if body:
        result.append(dict(text=str(body)[:16001], url=_source_url(advert.get('url') or record.get('url')),
                           date=str(advert.get('date') or advert.get('published_at') or advert.get('observed_at') or record.get('date') or '')[:40],
                           source=advert.get('source') or record.get('source_file') or 'record'))
    for item in (record.get('evidence_brief') or {}).get('items') or []:
        if item.get('kind') in ('role', 'context') and item.get('quote') and _source_url(item.get('source_url')):
            result.append(dict(text=str(item['quote'])[:2000], url=item['source_url'],
                               date=str(item.get('at') or '')[:40], source='research'))
    briefing = record.get('briefing') or {}
    provenance = (briefing.get('provenance') or {}).get('facts') or {}
    url = next((url for url in provenance.get('source_urls') or [] if _source_url(url)), '')
    if url:
        for fact in (briefing.get('facts') or [])[:20]:
            for quote in re.findall(r'["“]([^"”]{10,1000})["”]', str(fact)):
                result.append(dict(text=quote, url=url, date=str(provenance.get('source_date') or '')[:40], source='research'))
    # Inputs are already bounded by the source projection. Further bound comparisons per record.
    return result[:12]


def _capability_proof(phrase, proofs):
    phrase = requirement_phrase(phrase)
    return next((book for book in proofs for pattern in (book.get('fit') or {}).get('capabilities') or []
                 if _fit_pattern(pattern).fullmatch(phrase)), None)


def _compare_fit(record, proofs, qualified=None, proof_digest=None):
    global _FIT_BYTES
    sources = _fit_sources(record)
    advert = record.get('advert') or {}
    if not (advert.get('text') or advert.get('description') or advert.get('body') or record.get('summary')):
        qualified = None
    proof_digest = proof_digest or hashlib.blake2b(json.dumps(proofs, sort_keys=True).encode(), digest_size=16).digest()
    digest = hashlib.blake2b(proof_digest, digest_size=16)
    # Identical source text can describe physical work under a broad title, or software delivery
    # under an explicit software title. Do not reuse the first classification for the other.
    digest.update(str((qualified or {}).get('domain', '')).encode())
    for source in sources:
        for field in ('text', 'url', 'date', 'source'):
            raw = str(source[field]).encode()
            digest.update(len(raw).to_bytes(8, 'little') + raw)
    key = digest.digest()
    with _FIT_LOCK:
        cached = _FIT_CACHE.get(key)
        if cached is not None:
            _FIT_CACHE.move_to_end(key)
    if cached is not None:
        return json.loads(cached)
    result = _compare_sources(sources, proofs, qualified)
    encoded = json.dumps(result, separators=(',', ':'), ensure_ascii=True)
    size = len(encoded)  # ASCII serialization makes characters equal bytes.
    with _FIT_LOCK:
        previous = _FIT_CACHE.pop(key, '')
        _FIT_BYTES -= len(previous)
        if size <= _FIT_MAX_BYTES:
            while _FIT_CACHE and (len(_FIT_CACHE) >= _FIT_MAX_ENTRIES or _FIT_BYTES + size > _FIT_MAX_BYTES):
                _, evicted = _FIT_CACHE.popitem(last=False)
                _FIT_BYTES -= len(evicted)
            _FIT_CACHE[key] = encoded
            _FIT_BYTES += size
    return result


def _compare_sources(sources, proofs, qualified=None):
    evidence, required, comparisons, gaps, optional_unknowns, seen = {}, [], [], [], [], set()

    def overlap(book, source, quote, strength, kind):
        previous = evidence.get(book['id'])
        if previous and previous['strength'] >= strength:
            return
        evidence[book['id']] = dict(quote=quote[:240], url=source['url'], date=source['date'],
            source=source['source'], proof_id=book['id'], proof_url=book['proof']['url'],
            proof=book['proof']['text'], label=book['label'], strength=strength, kind=kind)

    for index, source in enumerate(sources):
        source_qualified = qualified if index == 0 and qualified is not None else qualification({'text': source['text']})
        outside = source_qualified['domain'] == 'outside'
        required.extend(({**item, 'source': source, 'outside': outside} for item in source_qualified['requirements']))
        if source_qualified['requirements_truncated']:
            gaps.append('Some requirement wording exceeds the bounded comparison; essential requirements are not fully established.')
        if outside:
            continue
        # Only task wording establishes remit overlap, not a title, stack list or prospecting cue.
        requirement_quotes = {item['text'].strip().casefold() for item in source_qualified['requirements']}
        for clause in re.split(r'\n|(?<=[.!?])\s+|;', source['text']):
            clause = clause.strip()
            # Requirement extraction removes list markers. Compare the same phrase without
            # changing the literal clause used for an actual duty's source quotation.
            if clause.strip(' \t-*•').casefold() in requirement_quotes:
                continue
            action = role_action(clause)
            if not action:
                continue
            for book in proofs:
                if (evidence.get(book['id']) or {}).get('strength') == 2:
                    continue
                mapping = book.get('fit') or {}
                if any(_fit_pattern(pattern).match(action) for pattern in mapping.get('remit') or []):
                    overlap(book, source, clause, 2, 'remit')
                elif any(_fit_pattern(pattern).match(action) for pattern in mapping.get('adjacent') or []):
                    overlap(book, source, clause, 1, 'remit')
    essential = {requirement_phrase(x['text']).casefold() for x in required if x['importance'] == 'essential'}
    waived = {requirement_phrase(x['text']).casefold() for x in required if x.get('waived')}
    conflicts = essential & waived
    for subject in sorted(conflicts):
        gaps.append(f'Requirement wording conflicts: {subject[:160]} is both essential and not required.')
    for item in required:
        identity = (item['text'].casefold(), item['importance'], item['negated'])
        if identity in seen:
            continue
        seen.add(identity)
        source = item['source']
        comparison = dict(quote=item['text'], importance=item['importance'], negated=item['negated'],
                          alternatives=item['alternatives'], url=source['url'], date=source['date'], source=source['source'])
        comparisons.append(comparison)
        if requirement_phrase(item['text']).casefold() in conflicts:
            comparison['status'] = 'conflicting'
            continue
        if item.get('unresolved'):
            comparison['status'] = 'not_established'
            gaps.append(f'Requirement wording needs checking: {item["text"][:200]}')
            continue
        if item.get('waived'):
            comparison['status'] = 'not_required'
            continue
        # Full capability matching deliberately leaves additional specialist, numeric and domain
        # qualifiers unknown. A match to part of "leadership AND police systems" is insufficient.
        alternatives = item['alternatives'] or [item['text']]
        proof = next((proof for alternative in alternatives if (proof := _capability_proof(alternative, proofs))), None)
        if item['truncated'] or item['negated'] or item['outside']:
            proof = None
        comparison['status'] = 'supported' if proof else 'not_established'
        if proof:
            comparison.update(proof_id=proof['id'], proof_url=proof['proof']['url'])
            overlap(proof, source, item['text'], 2, 'requirement')
        elif item['importance'] == 'essential' and not general_competency(item['text']):
            gaps.append(f'Essential experience not established: {item["text"][:200]}')
        else:
            label = ('General competency' if item['importance'] == 'essential' else
                     'Desirable' if item['importance'] == 'desirable' else 'Unspecified')
            optional_unknowns.append(f'{label} requirement not established: {item["text"][:200]}')
    supported = any(x['strength'] == 2 for x in evidence.values())
    ordered = sorted(evidence.values(), key=lambda x: -x['strength'])
    # Internal matching strength is not another assessment schema; expose the supporting quotes.
    public_evidence = [{k: v for k, v in item.items() if k != 'strength'} for item in ordered[:3]]
    return dict(status='supported' if supported else 'adjacent' if evidence else 'unconfirmed',
                gaps=list(dict.fromkeys(gaps))[:4], optional_unknowns=list(dict.fromkeys(optional_unknowns))[:3],
                evidence=public_evidence, requirements=sorted(comparisons, key=lambda x: x['importance'] != 'essential')[:8])


def load_config(path: Path = CONFIG) -> dict:
    mtime = path.stat().st_mtime
    if _cache.get('mtime') != mtime:
        import yaml
        _cache.update(mtime=mtime, config=yaml.safe_load(path.read_text()) or {})
    return _cache['config']


def same_advertiser(a, b) -> bool:
    """One advertiser under two name forms: equal keys, or one is the other's leading words
    ("Sanderson" / "Sanderson Recruitment", "Capital One" / "Capital One UK"), never "Meta" / "Metaswitch"."""
    return same_key(normalise.org_key(a), normalise.org_key(b))


def same_key(a: str, b: str) -> bool:
    """`same_advertiser` for names already passed through `normalise.org_key`."""
    a, b = sorted((a, b), key=len)
    return bool(a) and (a == b or b.startswith(a + ' '))


def first(terms, text: str) -> str:
    return next((t for t in terms if word(t, text)), '')


def as_date(value) -> dt.date | None:
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', str(value or ''))
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


@lru_cache(maxsize=512)
def pretty(d: dt.date) -> str:
    return f'{d.day} {d:%b %Y}'


def ago(days: int) -> str:
    if days <= 1:
        return 'today' if days <= 0 else 'yesterday'
    if days < 60:
        return f'{days} days ago'
    return f'{round(days / 30.4)} months ago'


def unseen_since(r: dict, seen: str | None, today: dt.date, f: dict) -> dt.date | None:
    """The last sighting of an advert no scanner has listed for over `unseen_days`. Not closure: a
    feed shows only its newest results, and a moved board is no longer read. A closing date or a
    gone lifecycle already says more, and an advert no scanner ever saw is neutral."""
    last = as_date(seen)
    if not last or r.get('published_deadline') or (r.get('source_lifecycle') or '') in f['gone_lifecycles']:
        return None
    return last if (today - last).days > f['unseen_days'] else None


def reach_of(person: dict, interactions: list, today: dt.date) -> dict:
    """Relationship strength needs a statement or reciprocal evidence, never an outbound alone."""
    rel = person.get('relationship') or ''
    li = person.get('linkedin') or {}
    evidence = li.get('evidence') or {}
    threads = evidence.get('threads') or {}
    if rel == 'do_not_contact':
        return dict(tier='blocked', reason='You marked this person do not contact')
    if rel == 'warm':
        return dict(tier='close', reason='You recorded a warm relationship')
    if rel == 'active':
        return dict(tier='in_touch', reason='You recorded an active relationship')
    own = [x for x in interactions if x.get('record_id') == person['id']]
    directions = {x.get('direction') for x in own}
    reciprocal = 'in' in directions and 'out' in directions
    last = as_date(threads.get('last_two_way')) if threads.get('two_way') else None
    if reciprocal:
        # A new outbound cannot refresh an old reciprocal relationship on its own.
        inbound = max(as_date(x.get('at')) or dt.date.min for x in own if x.get('direction') == 'in')
        outbound = max(as_date(x.get('at')) or dt.date.min for x in own if x.get('direction') == 'out')
        last = max(min(inbound, outbound), last or dt.date.min)
    if last and rel != 'dormant' and (today-last).days <= 730:
        return dict(tier='in_touch', reason=f'Two-way contact recorded {pretty(last)}')
    if reciprocal or threads.get('two_way') or evidence.get('endorsed_you') or evidence.get('you_endorsed') or evidence.get('recommendations') or rel == 'dormant':
        return dict(tier='known', reason='Historical two-way contact, endorsement, recommendation or recorded past relationship')
    if li or own or person.get('last_touch') or rel == 'cold':
        return dict(tier='connected', reason='Connection or one-way contact only; relationship strength unconfirmed')
    return dict(tier='cold', reason='No relationship evidence recorded')


class Scorer:
    def __init__(self, state: dict, config: dict, today: dt.date):
        self.c, self.today = config, today
        self.records = {r['id']: r for r in state.get('records', [])}
        self.cvs = state.get('cvs') or search_config.cvs()
        self.excluded = search_config.exclusions(self.records.values(), state.get('links', []), config=config)
        self.excluded.update({r['id']: r['exclusion'] for r in self.records.values() if r.get('exclusion')})
        self.neighbours: dict[str, list[tuple[str, str]]] = {}
        for link in state.get('links', []):
            for a, b in ((link['from_id'], link['to_id']), (link['to_id'], link['from_id'])):
                if b in self.records:
                    self.neighbours.setdefault(a, []).append((b, link.get('relation') or 'related'))
        self.touched: dict[str, dt.date] = {}
        interactions_by_record = {}
        for x in state.get('interactions', []):
            interactions_by_record.setdefault(x.get('record_id'), []).append(x)
            d = as_date(x.get('at'))
            if d and d > self.touched.get(x['record_id'], dt.date.min):
                self.touched[x['record_id']] = d
        self.drafts = {d['record_id'] for d in state.get('drafts', [])}
        self.decisions = state.get('decisions') or []
        # Reuse this snapshot's grouping instead of scanning all history for each person.
        self.reach = {r['id']: reach_of(r, interactions_by_record.get(r['id'], []), today)
                      for r in self.records.values() if r['type'] == 'person'}
        del interactions_by_record
        self.qualified = {}
        self.fit = {}
        proofs = [book for book in _proofs() if (book.get('proof') or {}).get('text')
                  and _source_url((book.get('proof') or {}).get('url'))]
        proof_digest = hashlib.blake2b(json.dumps(proofs, sort_keys=True).encode(), digest_size=16).digest()
        for r in self.records.values():
            if r['type'] in ADVERT_TYPES:
                advert = r.get('advert') or {}
                raw = dict(r, title=advert.get('title') or r.get('name') or '',
                           text=advert.get('text') or advert.get('description') or advert.get('body') or r.get('summary') or '',
                           employment_type=advert.get('employment_type') or advert.get('contract_type') or r.get('engagement') or '',
                           category=advert.get('category') or '', department=advert.get('department') or '')
                self.qualified[r['id']] = qualification(raw)
                self.fit[r['id']] = _compare_fit(r, proofs, self.qualified[r['id']], proof_digest)
            elif r['type'] in ORG_TYPES or r['type'] == 'opportunity':
                self.fit[r['id']] = _compare_fit(r, proofs, proof_digest=proof_digest)
        self.sightings: dict[str, str] = state.get('sightings') or {}
        self.generic = generic_signals(self.records.values())
        self.contact = {rid: self.contact_of(r) for rid, r in self.records.items() if r['type'] == 'person'}
        self.also_by: dict[str, list[str]] = {}
        self.duplicate_of = self.duplicates()
        self.work_on_duplicates: dict[str, list[dict]] = {}
        for rid, keep in self.duplicate_of.items():
            if self.records[rid]['type'] == 'job' and self.has_work(self.records[rid]):
                self.work_on_duplicates.setdefault(keep['id'], []).append(self.records[rid])
        self.results: dict[str, dict] = {}
        self.funded: dict[str, str] = {}
        self.approach_of: dict[str, dict] = {}
        self.routes: dict[str, dict] = {}
        self.found_on: dict[str, str] = {}
        self.agency_keys = {normalise.org_key(r['name']) for r in self.records.values() if r['type'] == 'agency'} - {''}
        self._agency_named: dict[str, bool] = {}

    # --- facts ------------------------------------------------------------------------------

    def linked(self, rid: str, *types: str, relations=None) -> list[dict]:
        return [self.records[x] for x, relation in self.neighbours.get(rid, [])
                if self.records[x]['type'] in types and (relations is None or relation in relations)]

    def touch(self, r: dict) -> dt.date | None:
        dates = [d for d in (self.touched.get(r['id']), as_date(r.get('last_touch'))) if d]
        return max(dates) if dates else None

    def contact_of(self, person: dict) -> tuple[dt.date, str] | None:
        """The latest evidenced contact with the owner: the person's own interactions or touch date.
        A pipeline row's date records when the row was last worked on, not contact."""
        if self.reach[person['id']]['tier'] not in ('close', 'in_touch', 'known'):
            return None
        own = self.touch(person)
        li_date = as_date(((person.get('linkedin') or {}).get('evidence') or {}).get('threads', {}).get('last_two_way'))
        return (max(own or dt.date.min, li_date or dt.date.min), '')

    def has_work(self, r: dict) -> bool:
        return bool(r['id'] in self.drafts or r.get('next_action') or r.get('status') not in (None, '', 'candidate'))

    def current_advert(self, r: dict) -> bool:
        """Whether an advert can support current hiring, independently of workflow priority."""
        q = self.qualified.get(r['id']) or {}
        deadline = as_date(r.get('published_deadline'))
        freshness = self.c['freshness']
        return (q.get('eligible', True) and not q.get('conflicts')
                and (r.get('source_lifecycle') or '') not in freshness['gone_lifecycles']
                and not (deadline and deadline < self.today)
                and not unseen_since(r, self.sightings.get(r['id']), self.today, freshness))

    def duplicates(self) -> dict[str, dict]:
        """Prefer current qualified evidence, then the copy carrying the owner's work.

        Other copies remain records with their workflow intact; the representative links back to
        any earlier work so a closed or stale source cannot hide a current hiring signal.
        """
        norm = normalise.org_key
        jobs = {r['id']: r for r in self.records.values() if r['type'] == 'job'}
        parent = {rid: rid for rid in jobs}

        def root(rid):
            while parent[rid] != rid:
                parent[rid] = parent[parent[rid]]
                rid = parent[rid]
            return rid

        def join(a, b):
            parent[root(a)] = root(b)

        identities = {}
        for rid, r in jobs.items():
            advert, context = r.get('advert') or {}, r.get('source_context') or {}
            keys = []
            for value in (r.get('url'), advert.get('url'), context.get('url')):
                if value:
                    try:
                        keys.append(('url', normalise.job_url(value)))
                    except ValueError:
                        pass
            for source in (r, advert):
                scope, native = source.get('source_scope'), source.get('source_id')
                if scope and native:
                    keys.append(('source', str(scope), str(native)))
                requisition, company = source.get('requisition_id'), norm(source.get('company') or r.get('organisation'))
                if requisition and company:
                    keys.append(('requisition', company, str(requisition)))
            explicit = r.get('duplicate_of')
            if explicit in jobs:
                join(rid, explicit)
            for other, relation in self.neighbours.get(rid, []):
                if relation == 'duplicate_of' and other in jobs:
                    join(rid, other)
            for key in keys:
                if key in identities:
                    join(rid, identities[key])
                else:
                    identities[key] = rid
        groups = {}
        for rid, r in jobs.items():
            groups.setdefault(root(rid), []).append(r)
        out = {}
        for rows in groups.values():
            if len(rows) < 2:
                continue
            rows.sort(key=lambda r: (not self.current_advert(r), not self.has_work(r), r['id']))
            for r in rows[1:]:
                out[r['id']] = rows[0]
        # Similar boilerplate is only a possible match. It cannot hide a different requisition,
        # infer one end client across agencies, or direct the owner to send a single application.
        similar = {}
        for rid, r in jobs.items():
            body = norm(r.get('summary'))[:120]
            if r.get('name') and len(body) >= 60 and rid not in out:
                similar.setdefault((norm(r['name']), body), []).append(r)
        for rows in similar.values():
            if len(rows) > 1:
                names = sorted({r.get('organisation') or r['id'] for r in rows})[:4]
                for r in rows:
                    self.also_by[r['id']] = [name for name in names if name != (r.get('organisation') or r['id'])] or names[:1]
        # Organisations recorded twice as the same type, for example with an HTML-escaped
        # apostrophe. An agency that also has a company record is a modelling overlap, not a copy.
        orgs: dict[tuple, list[dict]] = {}
        for r in self.records.values():
            if r['type'] in ORG_TYPES and r.get('name'):
                orgs.setdefault((r['type'], norm(r['name'])), []).append(r)
        for rows in orgs.values():
            if len(rows) > 1:
                rows.sort(key=lambda r: ('&#' in r['name'], -len(self.neighbours.get(r['id'], [])), r['id']))
                for r in rows[1:]:
                    out[r['id']] = rows[0]
        return out

    def known_people(self, rid: str) -> list[dict]:
        return list({p['id']: p for p in self.linked(rid, 'person', relations=('works_at', 'consultant_at'))
                     if self.contact.get(p['id']) and p.get('relationship') != 'do_not_contact'}.values())

    # --- signals ----------------------------------------------------------------------------

    def common(self, r: dict, add) -> None:
        s = self.c['signals']
        ws = r.get('working_state') or 'needs_review'
        w = min(0, self.c['states']['working_state'].get(ws, 0))
        if w:
            add(w, f"Working state: {ws.replace('_', ' ')}")
        action = (r.get('next_action') or '').strip()
        if action and first(s['on_hold_terms'], action.lower()):
            add(s['on_hold_weight'], 'Next step is marked on hold')
        elif action and first(s['watch_terms'], action.lower()):
            add(s['watch_weight'], 'Watch only; no step to take')
        elif r.get('waiting_on') == states.DECISION_WAITING and r.get('working_state') == 'waiting':
            # "Waiting on you" already shows the decision; ranking it again here would list it twice.
            add(0, 'Waiting on your answer to a decision')
        elif action and (r.get('owner') == 'owner' or action.lower().startswith('owner ')):
            add(0, f'Your next step: {action[:90]}', readiness=True)

    def activity(self, d: dt.date | None, add, via: str = '') -> None:
        if not d or d == dt.date.min:
            return
        a, days = self.c['activity'], (self.today - d).days
        where = f' ({via})' if via else ''
        if days <= a['recent_days']:
            add(a['recent_weight'], f'Last touch {pretty(d)}, {ago(days)}{where}')
        elif days <= a['known_days']:
            add(a['known_weight'], f'Last touch {pretty(d)}{where}')
        else:
            add(0, f'Last touch {pretty(d)}{where}; over a year ago')

    def title(self, r: dict, add) -> dict | None:
        """The first title family whose term appears, in config order, with the CV it routes to."""
        t = self.c['titles']
        text = f"{r.get('name', '')} {r.get('role', '')}".lower()
        hit = title_veto(text, t['negative']['terms'])
        if hit:
            add(t['negative']['weight'], f'Outside your targets: "{hit}"')
            return None
        equivalent = title_equivalent(text)
        for fam in t['families']:
            hit = first(fam['terms'], text) or first(fam['terms'], equivalent)
            if hit:
                add(fam['weight'], f'{fam["label"]}: "{hit}"')
                return dict(family=fam['id'], cv=(fam.get('cv_by_term') or {}).get(hit, fam.get('cv') or ''), term=hit)
        add(t['unmatched_weight'], 'Title matches none of your target roles')
        return None

    def route(self, r: dict, fam: dict | None) -> None:
        """Title rules suggest a CV; preserve that basis separately from a manual choice."""
        fam = fam or {}
        org = normalise.org_key(r.get('organisation'))
        # Many adverts share an advertiser: compare each distinct name with the agencies once.
        named = self._agency_named.get(org)
        if named is None:
            named = self._agency_named[org] = any(same_key(org, a) for a in self.agency_keys)
        via = named or bool(self.linked(r['id'], 'agency'))
        title = (r.get('name') or '').lower()
        equivalent = title_equivalent(title)
        rule, term = {}, None
        for candidate in self.c.get('cv_routes', []):
            term = first(candidate['terms'], title) or first(candidate['terms'], equivalent)
            if term:
                rule = candidate
                break
        suggested = rule.get('cv') or fam.get('cv') or None
        # A manual choice can retain a route even when automatic routing is ineligible.
        # Its suggestion must describe what resetting the override would actually select.
        if (self.qualified.get(r['id']) or {}).get('eligible') is False:
            suggested = None
        suggested = 'QE' if suggested == 'HeadQE' else suggested
        basis = (dict(kind='title_rule', term=term) if rule.get('cv') else
                 dict(kind='title_family', term=fam['term'])) if suggested else None
        cv = r.get('cv_override') or suggested
        cv = 'QE' if cv == 'HeadQE' else cv
        if cv:
            self.routes[r['id']] = dict(cv=cv, family=fam.get('family') or rule.get('family') or 'unmatched', via_agency=via,
                                       **self.cvs[cv], selection='manual' if r.get('cv_override') else 'suggested',
                                       suggested_cv=suggested, suggestion_basis=basis)

    def location(self, r: dict, add) -> None:
        loc = self.c['location']
        text = f"{r.get('location', '')} {r.get('workplace', '')}".lower()
        if not text.strip():
            add(0, 'Location not stated')
            return
        remote = word('remote', text) or (r.get('workplace') or '').lower() == 'remote'
        home, place = first(loc['home'], text), first(loc['abroad'], text)
        if home:
            add(loc['home_weight'], f'{home.title() if len(home) > 3 else home.upper()}-based or reachable')
        elif remote and first(loc['remote_open'], text):
            add(loc['remote_open_weight'], f"Remote ({first(loc['remote_open'], text)})")
        elif place:
            name = place.title() if len(place) > 3 else place.upper()
            if remote:
                add(loc['remote_abroad_weight'], f'Remote, but tied to {name}')
            else:
                add(loc['onsite_abroad_weight'], f'Based in {name}; you do not relocate')
        elif remote:
            add(loc['remote_open_weight'], 'Remote; region not stated')
        else:
            add(0, 'Location unclear')

    def pay(self, r: dict, add) -> None:
        from job_rules import text_pay
        p = self.c['pay']
        cur, period = r.get('pay_currency') or '', r.get('pay_period') or ''
        stated = text_pay(r.get('pay') or '')
        try:
            if stated:
                _, top, cur, period = stated
            else:
                top = float(r.get('pay_max') or r.get('pay_min') or '')
        except ValueError:
            add(0, 'Pay not stated')
            return
        if cur != 'GBP' or period not in ('day', 'year'):
            add(0, f"Pay stated as {cur or '?'} per {period or '?'}; not compared")
            return
        floor = p['floor_day_gbp'] if period == 'day' else p['floor_year_gbp']
        shown = f'£{top:,.0f}/{period}'
        if top >= floor:
            add(p['meets_weight'], f'{shown} meets your floor')
        else:
            add(p['below_weight'], f'{shown} is below your £{floor:,}/{period} floor', below_pay_floor=True)

    def freshness(self, r: dict, add) -> None:
        f = self.c['freshness']
        if (r.get('source_lifecycle') or '') in f['gone_lifecycles']:
            add(f['gone_weight'], 'No longer listed at the source')
        deadline = as_date(r.get('published_deadline'))
        if deadline and deadline < self.today:
            add(f['gone_weight'], f'Closing date {pretty(deadline)} has passed')
        elif deadline and (deadline - self.today).days <= 14:
            add(5, f'Closes {pretty(deadline)}')
        since = unseen_since(r, self.sightings.get(r['id']), self.today, f)
        if since:
            add(f['unseen_weight'], f'No source has listed it since {pretty(since)}; it may have closed', unseen=True)
        d = as_date(r.get('date'))
        if not d:
            return
        days = (self.today - d).days
        if days <= f['fresh_days']:
            add(f['fresh_weight'], f'Published {ago(days)}')
        elif days > f['old_days']:
            add(f['old_weight'], f'Published {pretty(d)}; likely filled or stale')
        elif days > f['stale_days']:
            add(f['stale_weight'], f'Published {ago(days)}')

    def found(self, r: dict, add) -> None:
        """An advert a scan created recently: when, and through which source."""
        f = self.c['found']
        via = next((label for prefix, label in f['via'].items() if (r.get('source_file') or '').startswith(prefix)), '')
        d = as_date(r.get('created_at'))
        if via and d and 0 <= (self.today - d).days < f['days']:
            add(0, f'Found on {pretty(d)} via {via}')
            self.found_on[r['id']] = d.isoformat()

    def network(self, r: dict, add) -> None:
        orgs = self.linked(r['id'], *ORG_TYPES)
        people = self.known_people(r['id']) + [p for o in orgs for p in self.known_people(o['id'])]
        seen = []
        for p in people:
            if p['id'] not in seen:
                seen.append(p['id'])
        if seen:
            names = ', '.join(self.records[x]['name'] for x in seen[:2])
            add(self.c['signals']['known_person_weight'], f'Someone you know is linked: {names}')

    # --- per type ---------------------------------------------------------------------------

    def advert(self, r: dict, add) -> None:
        keep = self.duplicate_of.get(r['id'])
        if keep:
            add(self.c['signals']['duplicate_weight'], f"Same role also listed as {keep['id']}, which ranks instead")
        if self.also_by.get(r['id']):
            add(0, f"Similar advert wording at {', '.join(self.also_by[r['id']][:3])}; whether these are the same vacancy is unconfirmed", note=True)
        for previous in self.work_on_duplicates.get(r['id'], []):
            work = [previous['status']] if previous.get('status') not in (None, '', 'candidate') else []
            if previous['id'] in self.drafts:
                work.append('saved wording available in history')
            if previous.get('next_action'):
                work.append(f"next step: {previous['next_action'][:90]}")
            add(0, f"Earlier work remains on {previous['id']}: {'; '.join(work)}", note=True, record_id=previous['id'])
        q = self.qualified.get(r['id']) or {}
        for reason in q.get('reasons') or []:
            add(0, reason)
        for conflict in q.get('conflicts') or []:
            add(0, conflict, conflict=True)
        fam = self.title(r, add) if q.get('eligible', True) else None
        if q.get('eligible') is False:
            add(-100, 'Engineering domain is outside your software and delivery work')
        if q.get('eligible', True) or r.get('cv_override'):
            self.route(r, fam)
        e = self.c['engagement']
        eng = q.get('engagement') or (r.get('engagement') or '').lower()
        if eng in e['preferred']:
            add(e['preferred_weight'], f'{eng.upper() if eng == "ftc" else eng.title()}: your preferred engagement')
        elif eng == 'permanent':
            add(e['permanent_weight'], 'Permanent; you prefer contract or interim')
        else:
            add(0, 'Engagement not stated')
        self.location(r, add)
        self.pay(r, add)
        self.freshness(r, add)
        self.found(r, add)
        w = self.c['states']['job_status'].get(r.get('status') or '', 0)
        if w:
            add(w, f"Status: {r['status']}")
        self.network(r, add)

    def evidence_fit(self, r, add):
        fit = self.fit.get(r['id']) or {}
        weights = self.c.get('fit') or {}
        if fit.get('evidence'):
            evidence = fit['evidence'][0]
            weight = weights.get('supported_weight', 100) if fit['status'] == 'supported' else weights.get('adjacent_weight', 40)
            relation = 'overlaps' if fit['status'] == 'supported' else 'is adjacent to'
            # Name the proof as the owner's experience: a leading label such as "Interim engineering
            # leadership" read as the advert's own terms.
            add(weight, f'Advertised work {relation} your sourced experience in {experience_phrase(evidence["label"])}.',
                fit=True, evidence=evidence)
        elif r['type'] in ADVERT_TYPES:
            add(0, 'Fit needs checking: the available wording does not establish supported experience overlap.', fit=True)
        for gap in fit.get('gaps') or []:
            add(0, gap, fit=True, essential_unknown=True)

    def opportunity(self, r: dict, add) -> None:
        stage = r.get('stage') or ''
        w = self.c['states']['stage'].get(stage, 0)
        if w:
            add(w, f'Stage: {stage}')
        worked = self.touch(r)
        if worked:
            days = (self.today - worked).days
            add(self.c['signals']['pipeline_worked_weight'] if days <= self.c['activity']['recent_days'] else 0,
                f'Pipeline row last worked on {pretty(worked)}')
        self.network(r, add)

    def person(self, r: dict, add) -> None:
        s = self.c['signals']
        contact = self.contact.get(r['id'])
        if contact:
            add(s['evidenced_contact_weight'], 'Has a recorded touch point with you' if contact[0] != dt.date.min
                else self.reach[r['id']]['reason'])
            self.activity(contact[0], add, contact[1])
        else:
            add(s['no_contact_weight'], 'No recorded contact with you yet')
        rel = r.get('relationship') or ''
        w = self.c['states']['relationship'].get(rel, 0)
        if w:
            add(w, f'Relationship: {rel.replace("_", " ")}')
        opps = [o for o in self.linked(r['id'], 'opportunity') if o.get('stage') in OPEN_STAGES]
        if opps:
            add(s['live_opportunity_weight'], f"Route into {opps[0].get('organisation') or opps[0]['name']}")
        if first(s['senior_person_terms'], (r.get('role') or '').lower()):
            add(s['senior_person_weight'], 'Senior decision-maker role')
        rules = profile_rules.current()
        if rules.warning and rules.warning in (r.get('known_context') or ''):
            add(0, f'{rules.label} contact: check confidentiality and non-solicitation obligations before contact')

    def org(self, r: dict, add) -> None:
        s = self.c['signals']
        keep = self.duplicate_of.get(r['id'])
        if keep:
            add(s['duplicate_weight'], f"Same organisation as {keep['id']}; use that record")
        roles = list({j['id']: j for j in self.linked(r['id'], 'job', 'tender', relations=('hiring_for', 'advertised_by', 'buyer'))
                      if j['id'] not in self.duplicate_of
                      and self.signal_band(j) in ('strong', 'possible')
                      and self.current_advert(j)}.values())
        if roles:
            names = '; '.join(list(dict.fromkeys(j['name'] for j in roles))[:2])
            add(min(s['relevant_role_weight'] * len(roles), s['relevant_role_cap']),
                f"{len(roles)} relevant role{'s' if len(roles) > 1 else ''}: {names}")
        opps = [o for o in self.linked(r['id'], 'opportunity') if o.get('stage') in OPEN_STAGES]
        if opps:
            add(10, f"Open opportunity: {opps[0]['name']}")
        known = self.known_people(r['id'])
        if known:
            add(s['known_person_weight'], f"Someone you know is here: {', '.join(p['name'] for p in known[:2])}")
        st = r.get('status') or ''
        w = self.c['states']['org_status'].get(st, 0)
        if w:
            add(w, f"Status: {st.replace('_', ' ')}")
        for practice in self.c.get('practices', []):
            if first(practice['terms'], (r.get('subtype') or '').lower()):
                add(practice['weight'], practice['reason'])
                break
        tier = first(s['preferred_channel_terms'], (r.get('subtype') or '').lower())
        if tier:
            add(s['preferred_channel_weight'], f'{tier.title()} channel' if tier.startswith('tier') else 'Your preferred channel')
        self.funding(r, add)
        self.approach(r, add)
        self.activity(self.touch(r), add)

    def funding(self, r: dict, add) -> None:
        s = self.c['signals']
        dated = [(as_date(x.get('published')), x) for x in r.get('signals') or []]
        dated = sorted(((d, x) for d, x in dated if d), key=lambda p: p[0], reverse=True)
        if dated and (self.today - dated[0][0]).days <= s['funding_days']:
            d, x = dated[0]
            where = f" ({x['region']})" if x.get('region') else ''
            add(s['funding_weight'], f"Raised {x.get('amount') or 'funding'}{where}, reported {pretty(d)}")
            self.funded[r['id']] = d.isoformat()

    def approach(self, r: dict, add) -> None:
        """A company to approach before any advert of its own reaches an agency: it has just raised money,
        or it is advertising a leadership or AI role itself."""
        if r['type'] != 'company' or r['id'] in self.excluded:
            return
        hiring = [j for j in self.linked(r['id'], 'job', relations=('hiring_for', 'advertised_by')) if j['id'] not in self.duplicate_of
                  and j['id'] not in self.excluded
                  and self.signal_band(j) in ('strong', 'possible')
                  and self.current_advert(j)
                  and self.routes.get(j['id'], {}).get('family') in ('leadership', 'ai')
                  and not self.routes[j['id']]['via_agency']]
        funded = r['id'] in self.funded
        if not (funded or hiring):
            return
        s = self.c['signals']
        senior = [p for p in self.linked(r['id'], 'person', relations=('works_at',))
                  if p.get('relationship') != 'do_not_contact'
                  and first(s['senior_person_terms'], (p.get('role') or '').lower())]
        if senior:
            add(s['decision_maker_weight'], f"Named decision-maker: {senior[0]['name']}, {senior[0].get('role')}")
        if hiring:
            add(0, f"Hiring now: {hiring[0]['name']}", note=True)
        registry = (r.get('registry') or [None])[0]
        if registry:
            filed = sorted((d for d in (as_date(a.get('filed_on')) for a in registry.get('allotments') or []) if d), reverse=True)
            if filed and (self.today - filed[0]).days <= s['funding_days']:
                add(0, f'Shares allotted {pretty(filed[0])} (Companies House)', note=True)
            if registry.get('company_status') not in (None, '', 'active'):
                add(0, f"Companies House status: {registry['company_status']}", note=True)
        # A sentence that several companies share describes a batch, not this company (GENERIC_SIGNAL_MIN).
        study = next((x for x in r.get('research') or [] if x.get('signal') and x['signal'] not in self.generic), None)
        if study:
            add(0, f"Why approach: {study['signal']}", note=True)
        known = self.known_people(r['id'])
        warm = [p for p in known if self.reach[p['id']]['tier'] in ('close', 'in_touch')]
        self.approach_of[r['id']] = dict(trigger='both' if funded and hiring else 'funded' if funded else 'hiring',
                                         named=bool(senior), warm=bool(warm))

    def event(self, r: dict, add) -> None:
        d = as_date(r.get('date'))
        if d and d >= self.today:
            days = (d - self.today).days
            add(15 if days <= 45 else 5, f'Takes place {pretty(d)}')
        elif d:
            add(-30, f'Took place {pretty(d)}')
        else:
            add(0, 'Date not confirmed')
        self.network(r, add)

    # --- assembly ---------------------------------------------------------------------------

    def lane(self, r: dict) -> str | None:
        kind = r['type']
        if kind == 'event':
            return 'event'
        if kind not in ADVERT_TYPES and kind != 'opportunity':
            return None
        eng, lanes = self.qualified.get(r['id'], {}).get('engagement') or (r.get('engagement') or '').lower(), self.c['lanes']
        if kind == 'tender' or eng in lanes['contract']:
            return 'contract'
        if eng in lanes['permanent']:
            return 'permanent'
        return 'pipeline' if kind == 'opportunity' else 'unstated'

    def signal_band(self, advert: dict) -> str:
        """An advert's relevance when considering supporting company context."""
        result = self.results.get(advert['id'], {})
        return result.get('signal_band') or result.get('band')

    def score(self, r: dict) -> dict:
        reasons: list[dict] = []
        add = lambda weight, text, **flags: reasons.append(dict(weight=int(weight), text=text, **flags))
        kind = r['type']
        if kind in ADVERT_TYPES:
            self.advert(r, add)
        elif kind == 'opportunity':
            self.opportunity(r, add)
        elif kind == 'person':
            self.person(r, add)
        elif kind in ORG_TYPES:
            self.org(r, add)
        elif kind == 'event':
            self.event(r, add)
        if kind != 'source':
            self.common(r, add)
        if kind in ADVERT_TYPES or kind in ORG_TYPES or kind == 'opportunity':
            self.evidence_fit(r, add)
            # Cap combined positive context, not individual claim count. A separate signed factor
            # keeps the existing breakdown exact, including the original observed signal weights.
            positive = sum(x['weight'] for x in reasons if x['weight'] > 0 and not x.get('fit'))
            cap = self.c.get('fit', {}).get('secondary_positive_cap', 40)
            if positive > cap:
                add(cap - positive, 'Combined title, access, terms and recency contribution is capped.', context_cap=True)
        total = sum(x['weight'] for x in reasons)
        bands = self.c['bands']
        band = 'strong' if total >= bands['strong'] else 'possible' if total >= bands['possible'] else 'low'
        q = self.qualified.get(r['id'])
        if q and q.get('eligible') is False:
            band = 'low'
        elif q and q.get('conflicts') and band == 'strong':
            band = 'possible'
        fit = self.fit.get(r['id'])
        if fit and band == 'strong' and (fit['gaps'] or (kind in ADVERT_TYPES and fit['status'] == 'unconfirmed')):
            band = 'possible'
        if band == 'strong' and any(reason.get('below_pay_floor') for reason in reasons):
            band = 'possible'
        reasons.sort(key=lambda x: -abs(x['weight']))
        out = dict(score=total, band=band, reasons=reasons)
        if r['id'] in self.duplicate_of:
            out['duplicate_of'] = self.duplicate_of[r['id']]['id']
        if q:
            out['qualification'] = q
        if fit:
            out['fit'] = fit
        if r['id'] in self.sightings:
            out['last_seen'] = self.sightings[r['id']]
        lane = self.lane(r)
        if lane:
            out['lane'] = lane
        if kind == 'person':
            out['known'] = bool(self.contact.get(r['id']))
            out['reach'] = self.reach[r['id']]
        if r['id'] in self.funded:
            out['funded'] = self.funded[r['id']]
        if r['id'] in self.approach_of:
            out['approach'] = self.approach_of[r['id']]
        if r['id'] in self.routes:
            out['route'] = self.routes[r['id']]
        if r['id'] in self.found_on:
            out['found'] = self.found_on[r['id']]
        return out

    def run(self) -> dict[str, dict]:
        order = sorted(self.records.values(), key=lambda r: r['type'] in ORG_TYPES)
        for r in order:
            self.results[r['id']] = self.score(r)
        return self.results


GENERIC_SIGNAL_MIN = 3


def generic_signals(records) -> set[str]:
    """Repeated batch prose across distinct records, retaining original spellings for callers.

    Refreshing one company's evidence never makes it generic. Case and whitespace changes do
    not turn a sentence copied across several companies into company-specific evidence.
    """
    seen: dict[str, set[str]] = {}
    originals: dict[str, set[str]] = {}
    for r in records:
        for x in r.get('research') or []:
            if x.get('signal'):
                key = ' '.join(x['signal'].split()).casefold()
                if key:
                    seen.setdefault(key, set()).add(r['id'])
                    originals.setdefault(key, set()).add(x['signal'])
    return {text for key, ids in seen.items() if len(ids) >= GENERIC_SIGNAL_MIN for text in originals[key]}


def score(state: dict, config: dict | None = None, today: dt.date | None = None) -> dict[str, dict]:
    return Scorer(state, config or load_config(), today or dt.date.today()).run()
