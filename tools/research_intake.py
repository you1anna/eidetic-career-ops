"""Validation and conservative identity for the existing attended research importer.

No network access, workflow decisions or automatic contact creation. The importer supplies
the shared record/organisation writers; evidence and workflow still use records.py.
"""
import hashlib

import normalise
import records as store
import row_shape as ed
import search_config
import states
import outreach_routes
import evidence_checks
import email_sources
import evidence_briefs

# The agent's prepared approach for one person (the first batch, 15 October): their role in the owner's
# search, two talking points and the fitted ask, the CV, what he wants from a first conversation and
# one dated line on what they last talked about (16 October, his choice). Talking points only:
# the message itself is a draft with purpose "message", not a field on this object. An empty
# object withdraws an earlier approach.
APPROACH_ROLES = ('decision_maker', 'internal_advocate', 'connector', 'referee', 'recruiter')
APPROACH_FIELDS = {'role', 'points', 'ask', 'cv', 'goal', 'wave', 'last'}


def validate_approach(approach):
    if not isinstance(approach, dict):
        raise ValueError('approach must be an object')
    if not approach:
        return
    extra = set(approach) - APPROACH_FIELDS
    if extra:
        raise ValueError('Unsupported approach fields: ' + ', '.join(sorted(extra)))
    if approach.get('role') not in APPROACH_ROLES:
        raise ValueError('Choose an approach role: ' + ', '.join(APPROACH_ROLES))
    points = approach.get('points')
    if not isinstance(points, list) or not 1 <= len(points) <= 2:
        raise ValueError('An approach has one or two talking points; the ask comes last')
    if not all(store.text(p, 240) for p in points):
        raise ValueError('A talking point cannot be empty')
    for field in ('ask', 'goal', 'last'):
        store.text(approach.get(field), 240)
    if approach.get('cv') is not None and approach['cv'] not in search_config.CV_KEYS:
        raise ValueError('Choose a registered CV: ' + ', '.join(search_config.CV_KEYS))
    wave = approach.get('wave')
    if wave is not None and (type(wave) is not int or not 0 <= wave <= 9):
        raise ValueError('A wave is a whole number from 0 to 9')


def advert_url(value):
    """Use the same canonical URL as manual capture and scanner imports."""
    return normalise.job_url(value)


def validate(entries):
    if not isinstance(entries, list) or not all(isinstance(r, dict) for r in entries):
        raise ValueError('Research must be a JSON list of objects')
    for r in entries:
        if not r.get('id') and not r.get('create'):
            raise ValueError('Each finding needs id or create')
        if r.get('id') and r.get('create'):
            raise ValueError('Use id for an existing record or create for a new target, not both')
        if r.get('id'): store.text(r['id'], 200)
        if 'finding_id' in r: store.text(r['finding_id'], 200)
        if normalise.observation_time(store.text(r.get('reviewed_at'), 50)) == normalise.observation_time(None):
            raise ValueError('Research needs an ISO review date or timestamp')
        urls = r.get('source_urls')
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) and u.strip() for u in urls):
            raise ValueError('Research needs a list of source URLs')
        for url in urls: store.safe_url(url)
        if 'alert_evidence' in r:
            alert = r['alert_evidence']
            if (not isinstance(alert, dict) or set(alert) != {'source', 'received_at', 'url'}
                    or not email_sources.is_alert(alert.get('source')) or alert.get('url') not in urls
                    or advert_url(alert.get('url')) != alert.get('url')):
                raise ValueError('Recovered alert needs its canonical job URL, source and original receipt time')
            evidence_checks.checked_time(alert.get('received_at'))
            if r.get('create') and (r['create'].get('type') != 'job'
                                   or advert_url(r['create'].get('url')) != alert['url']):
                raise ValueError('Recovered alert must describe this exact job')
        evidence_checks.validate_finding(r)
        if 'evidence_brief' in r:
            evidence_briefs.validate(r['evidence_brief'], r.get('id'))
        if r.get('source_date') and not normalise.is_factual_date(store.text(r['source_date'], 50)):
            raise ValueError('Source date must be a factual date')
        for field in ('summary', 'desk_summary', 'signal', 'mandate_evidence', 'public_contact_route'):
            if field in r: store.text(r[field])
        if 'verification' in r:
            states.validate('record', 'verification', r['verification'])
        outreach_routes.validate_signals(r)
        if 'approach' in r:
            validate_approach(r['approach'])
        if r.get('named_consultant'):
            named = r['named_consultant']
            if not isinstance(named, dict) or not store.text(named.get('name')):
                raise ValueError('A named consultant needs a name')
            for field in ('role', 'public_route'):
                if field in named: store.text(named[field])
        for key in ('facts', 'assumptions', 'decisive_unknowns', 'related_ids'):
            if key in r:
                if not isinstance(r[key], list): raise ValueError(f'{key} must be a list of text')
                for value in r[key]:
                    if not store.text(value): raise ValueError(f'{key} cannot contain empty text')
        if 'suggested_task' in r: store.text(r['suggested_task'], 240)
        new = r.get('create')
        if not new: continue
        if not isinstance(new, dict): raise ValueError('create must be an object')
        typ = new.get('type', 'company')
        if typ not in ('company', 'agency', 'job'):
            raise ValueError('Research may create only a company, agency or job')
        if not store.text(new.get('name')): raise ValueError('A name is required')
        if typ == 'job':
            extra = set(new) - {'type', 'name', 'organisation', 'url', 'location', 'engagement', 'date'}
            if extra: raise ValueError('Unsupported vacancy fields: ' + ', '.join(sorted(extra)))
            if not store.text(new.get('organisation')) or not advert_url(new.get('url')):
                raise ValueError('A vacancy needs its organisation and advert URL')
        store._values({k: v for k, v in new.items() if k != 'type'})


def resolve_job(conn, new, source, at, upsert, link_organisation):
    """Exact URL only: an old interview with the same title is not a new vacancy."""
    url = advert_url(new['url'])
    index = store.job_url_index(conn)
    try:
        matched = store.match_job_url(conn, url, index)
    except store.Conflict as error:
        # Keep the shared matcher's refusal; classify it for a machine-readable receipt.
        archived, survivors = [], set()
        for candidate in sorted(index.get(url, ())):
            try: survivors.add(store.active_job_survivor(conn, candidate))
            except store.Conflict: archived.append(candidate)
        return dict(record_id=archived[0] if len(archived) == 1 else None,
                    outcome='archived' if archived else 'ambiguous',
                    matches=sorted(set(archived) | survivors), archived_ids=archived, reason=str(error)), False
    if matched: return dict(record_id=matched), False
    rid = 'JOB-' + hashlib.sha256(url.encode()).hexdigest()[:16]
    if conn.execute('SELECT 1 FROM records WHERE id=?', (rid,)).fetchone():
        raise ValueError('Vacancy identity collision; supply the reviewed record id')
    facts = {k: new[k] for k in ('name', 'organisation', 'location', 'engagement', 'date') if new.get(k)}
    upsert(conn, ed.row(id=rid, type='job', url=url, source_file='source:' + source, **facts),
           {'status': 'candidate'}, at)
    employer = link_organisation(conn, rid, new['organisation'], 'hiring_for', source, at)
    result = dict(record_id=rid, organisation_id=employer)
    if employer is None: result['archived_organisation'] = new['organisation']
    return result, True


def validate_signal_targets(conn, rid, finding):
    if 'alert_evidence' in finding:
        target = conn.execute('SELECT type,url FROM records WHERE id=?', (rid,)).fetchone()
        if not target or target['type'] != 'job' or advert_url(target['url']) != finding['alert_evidence']['url']:
            raise ValueError('Recovered alert must describe this exact job')
    checks = finding.get('freshness_checks', [])
    if checks:
        row = conn.execute('SELECT type FROM records WHERE id=?', (rid,)).fetchone()
        if row['type'] == 'person':
            if any(c.get('scope')!='contact_route' or c.get('person_id')!=rid for c in checks):
                raise ValueError('A person can check only their own contact route')
        elif row['type'] != 'job':
            if row['type'] not in ('company','agency','opportunity','tender') or any(
                    c.get('scope')!='contact_route' for c in checks):
                raise ValueError('Only a job can carry a vacancy check')
            # Work may cite an already-linked person or an already-linked organisation's route.
            # It cannot use a model response to invent a relationship to unrelated work.
            linked={r[0] for r in conn.execute(
                'SELECT CASE WHEN from_id=? THEN to_id ELSE from_id END FROM links WHERE from_id=? OR to_id=?',
                (rid,rid,rid))}
            for check in checks:
                organisation=check.get('organisation_id')
                if organisation:
                    if organisation!=rid and organisation not in linked:
                        raise ValueError('A work contact route needs an existing organisation link')
                    related=conn.execute('SELECT 1 FROM links WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)',
                        (check.get('person_id'),organisation,organisation,check.get('person_id'))).fetchone()
                    if not related:raise ValueError('A work contact route needs an existing person link')
                elif check.get('person_id') not in linked:
                    raise ValueError('A work contact route needs an existing person link')
        targets = {check[key] for check in checks for key in ('person_id', 'organisation_id') if key in check}
        active = {row['id']: row['type'] for row in conn.execute(
            'SELECT id,type FROM records WHERE archived_at IS NULL AND id IN (' +
            ','.join('?' for _ in targets) + ')', tuple(targets))} if targets else {}
        for check in checks:
            if not evidence_checks.valid_targets(check, active):
                raise ValueError('A contact_route check needs an active person and, when named, an active company or agency')
    if 'approach' in finding and conn.execute('SELECT type FROM records WHERE id=?', (rid,)).fetchone()['type'] != 'person':
        raise ValueError('A prepared approach belongs on a person')
    signals = finding.get('outreach_signals') or []
    if not signals:
        return
    row = conn.execute('SELECT type FROM records WHERE id=?', (rid,)).fetchone()
    if row['type'] != 'company':
        raise ValueError('Outreach signals belong on the company research record')
    for signal in signals:
        if signal['kind'] != 'leadership_change':
            continue
        pid = signal['person_id']
        person = conn.execute("SELECT id FROM records WHERE id=? AND type='person' AND archived_at IS NULL", (pid,)).fetchone()
        linked = conn.execute("SELECT 1 FROM links WHERE relation='works_at' AND "
                              "((from_id=? AND to_id=?) OR (from_id=? AND to_id=?))", (pid, rid, rid, pid)).fetchone()
        if not person or not linked:
            raise ValueError('A leadership change needs an active person linked to this company with works_at')


def suggest_and_link(conn, rid, finding):
    """A suggestion fills an untouched empty slot; later suggestions stay as evidence."""
    for target in finding.get('related_ids', []):
        if target == rid: continue
        row = conn.execute('SELECT archived_at FROM records WHERE id=?', (target,)).fetchone()
        if row is None or row['archived_at']:
            raise ValueError(f'Related record {target!r} must be active')
        # An existing link's notes belong to their author.
        if conn.execute("SELECT 1 FROM links WHERE from_id=? AND to_id=? AND relation='related'", (rid, target)).fetchone():
            continue
        revision = conn.execute('SELECT revision FROM records WHERE id=?', (rid,)).fetchone()[0]
        store.link(conn, dict(from_id=rid, to_id=target, relation='related', revision=revision), actor='agent')
    task = finding.get('suggested_task', '').strip()
    if not task: return 'none'
    row = conn.execute('SELECT revision,next_action,working_state,status,stage,relationship FROM records WHERE id=?', (rid,)).fetchone()
    protected = conn.execute("SELECT 1 FROM record_edits WHERE record_id=? AND field IN ('next_action','working_state','owner','status','stage','relationship')", (rid,)).fetchone()
    established = (row['status'] not in (None, '', 'candidate', 'not_contacted') or
                   row['stage'] not in (None, '', 'lead') or row['relationship'] == 'do_not_contact')
    excluded = store.record_views.record_exclusions(conn).get(rid)
    if (protected or established or excluded or row['next_action'] or row['working_state'] not in ('', 'needs_review') or
            conn.execute('SELECT 1 FROM followups WHERE record_id=?', (rid,)).fetchone() or
            conn.execute("SELECT 1 FROM decisions WHERE record_id=? AND status IN ('open','answered')", (rid,)).fetchone()):
        return 'preserved'
    store.followup(conn, dict(id=rid, revision=row['revision'], action=task, owner='agent', status='needs_review'), actor='agent')
    return 'suggested'
