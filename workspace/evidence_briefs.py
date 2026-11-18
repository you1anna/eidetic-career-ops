"""Read-only evidence completeness. Drafts and approval never establish factual readiness."""
import datetime as dt
import hashlib
import json
import re
from urllib.parse import urlsplit

import approaches
import evidence_checks

VERSION = 1
LABELS = {'available': 'Context available', 'needs_input': 'Needs your input',
          'pending': 'Research pending', 'blocked': 'Research blocked'}


def fresh(check, now=None, hours=24):
    try:
        age = (now or evidence_checks.utc_now()) - evidence_checks.checked_time(check['checked_at'], now)
        return check.get('status') == 'confirmed' and dt.timedelta(0) <= age < dt.timedelta(hours=hours)
    except (KeyError, TypeError, ValueError):
        return False


def safe_url(value):
    try:
        p = urlsplit(value or '')
        return bool(p.scheme in ('http', 'https') and p.hostname and not p.username and not p.password)
    except ValueError:
        return False


def route(record):
    channel = record.get('preferred_channel') or ''
    email = record.get('email') or ''
    phone = record.get('phone') or ''
    profile = record.get('url') or ''
    choices = {'email': email if re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}', email) else '',
               'call': phone if re.fullmatch(r'\+?\d{7,15}', re.sub(r'[\s().-]', '', phone)) else '',
               'linkedin': profile if safe_url(profile) and ((urlsplit(profile).hostname or '') == 'linkedin.com' or (urlsplit(profile).hostname or '').endswith('.linkedin.com')) else ''}
    choices['whatsapp'] = choices['text'] = choices['call']
    if record.get('type') != 'person':
        return dict(channel='application' if record.get('type')=='job' else 'source',
                    value=next((record.get(k) for k in ('apply_url', 'detail_url', 'url') if safe_url(record.get(k))), ''))
    if channel:
        return dict(channel=channel, value=choices.get(channel, ''))
    return next((dict(channel=k, value=v) for k, v in choices.items() if v), dict(channel='', value=''))


def context(record):
    """Stable inputs only; a source refresh or an unrelated draft edit is not new context."""
    if record.get('type') != 'person':
        return {k:record.get(k) for k in ('id','type','name','organisation','summary','notes','engagement',
                'location','pay','ir35','url','apply_url','detail_url','status','stage','next_action')} | {
                'incoming':{k:v for k,v in (record.get('incoming') or {}).items() if k!='undo_action'}}
    return {k: record.get(k) for k in ('id', 'type', 'name', 'organisation', 'role', 'known_context',
            'notes', 'preferred_ask', 'preferred_channel', 'url', 'email', 'phone', 'apply_url', 'detail_url',
            'relationship', 'status')} | {'incoming': {k:v for k,v in (record.get('incoming') or {}).items() if k != 'undo_action'}}


def fingerprint(record):
    return hashlib.sha256(json.dumps(context(record), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate(value, subject_id=None):
    """Validate the persisted evidence contract at intake and projection, including legacy reads."""
    if not isinstance(value, dict) or value.get('version') != VERSION:
        raise ValueError('Unsupported evidence brief version')
    if set(value) != {'version', 'items', 'gaps', 'context_sha'}:
        raise ValueError('Invalid evidence brief fields')
    if not isinstance(value['context_sha'], str) or len(value['context_sha']) > 64:
        raise ValueError('Invalid evidence context fingerprint')
    if not isinstance(value['items'], list) or len(value['items']) > 8:
        raise ValueError('Invalid evidence items')
    for item in value['items']:
        if not isinstance(item, dict) or set(item) != {'kind', 'value', 'quote', 'source_url', 'checked_at', 'subject_id'}:
            raise ValueError('Invalid evidence item fields')
        if item['kind'] not in ('employer', 'role', 'context', 'route', 'vacancy'):
            raise ValueError('Invalid evidence kind')
        if any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 2000 for k in item):
            raise ValueError('Invalid evidence text')
        if subject_id and item['subject_id'] != subject_id:
            raise ValueError('Evidence belongs to another subject')
        if not safe_url(item['source_url']) or evidence_checks.private_source(item['source_url']):
            raise ValueError('Public evidence needs a public URL')
        evidence_checks.checked_time(item['checked_at'])
    if not isinstance(value['gaps'], list) or len(value['gaps']) > 8 or any(
            not isinstance(g, str) or not g.strip() or len(g) > 400 for g in value['gaps']):
        raise ValueError('Invalid evidence gaps')


def build(record, edits=None, interactions=(), researched=None, now=None):
    if record.get('type') != 'person':
        return work_brief(record, researched, now)
    edits = edits or {}; now = now or evidence_checks.utc_now()
    items, gaps = [], []
    def gap(code, text, owner='research', blocking=True):
        gaps.append(dict(code=code, text=text, owner=owner, blocking=blocking))
    def local(kind, field, text):
        if not text: return
        edit = edits.get(field, {})
        items.append(dict(kind=kind, value=text, provenance='owner_confirmed' if edit.get('actor') == 'owner' else 'recorded',
                          at=edit.get('edited_at', ''), source_url='', quote='', reference=f"record:{record['id']}:{field}"))
    # Generated approach wording is historical content, never factual evidence.
    plan = {}
    relationship = record.get('known_context') or approaches._own_note(record) or ''
    ask = record.get('preferred_ask') or plan.get('ask') or ''
    job = record.get('type') == 'job'
    incoming = record.get('incoming') or {}
    current_process = bool(incoming.get('current') and incoming.get('status') == record.get('status') and record.get('status') in ('responded','interview','offer'))
    if current_process:
        ask = incoming['label'] + ': ' + (incoming.get('next_step') or 'Outcome and agreed next step not yet recorded.')
        items.append(dict(kind='process', value=incoming['label'], provenance='recorded', at=incoming['stage_date'], source_url='', quote='', reference=f"record:{record['id']}:incoming"))
    if job and not ask and record.get('status') in ('pursue', 'applied', 'responded', 'interview', 'offer'):
        ask = 'Recorded application: ' + record['status']
    if not job:
        local('relationship', 'known_context' if record.get('known_context') else 'notes', relationship)
        if not relationship: gap('relationship', 'Record how you know this person.', 'owner')
    local('ask', 'preferred_ask' if record.get('preferred_ask') else 'approach.ask', ask)
    local('objective', 'approach.goal', plan.get('goal'))
    local('last_context', 'approach.last', plan.get('last'))
    if not ask:
        gap('ask', 'Choose Shortlist under More application options if you want to pursue this job.' if job
            else 'Record what you want from this approach.', 'owner')
    for field in ('role', 'organisation'):
        local(field, field, record.get(field))
    contact = max((i for i in interactions if (i.get('person_id') or i.get('record_id')) == record['id']
                   and i.get('direction') in ('in', 'out') and i.get('at')), key=lambda i: i['at'], default=None)
    threads = ((record.get('linkedin') or {}).get('evidence') or {}).get('threads') or {}
    last = contact.get('at') if contact else threads.get('last_two_way') or record.get('last_touch') or ''
    if last:
        items.append(dict(kind='last_contact', value=last, provenance='recorded', at=last, source_url='', quote='',
                          reference=f"interaction:{contact['id']}" if contact and contact.get('id') else f"record:{record['id']}:last_contact"))
    elif not job: gap('last_contact', 'No last-contact date is recorded.', 'owner', False)
    selected = route(record)
    checks = record.get('evidence_checks') or {}
    verified = [c for c in checks.get('contacts', []) if fresh(c, now)]
    # A current profile check does not confirm a different email/phone selected on the card.
    selected['verified'] = bool(selected['value']) and any(not c.get('organisation_id') and
        (selected['value'].casefold() in (c.get('basis') or '').casefold() or
         (selected['channel'] == 'linkedin' and selected['value'].rstrip('/') == c.get('source_url','').rstrip('/')))
        for c in verified)
    if not selected['value']: gap('route', 'Find or record a usable contact route for the chosen channel.', blocking=not current_process)
    elif not job and not selected['verified']: gap('route_check', 'The recorded route has no current confirmation.', blocking=False)
    employer_required = not job and plan.get('role') in ('decision_maker', 'internal_advocate') and record.get('preferred_ask') != 'catch_up'
    if employer_required and not any(c.get('organisation_id') for c in verified):
        gap('employer', 'Check the current employer before using this employer-specific approach.')
    if job and not fresh(checks.get('vacancy') or {}, now):
        gap('vacancy', 'Public vacancy availability is unverified; the recorded hiring conversation remains active.' if current_process else 'Check whether this exact vacancy is still open.', blocking=not current_process)
    if researched:
        for item in researched.get('items', []):
            items.append(dict(kind=item['kind'], value=item['value'], provenance='public_source', at=item['checked_at'],
                              source_url=item['source_url'], quote=item['quote'], reference=item['source_url']))
            if item['kind'] == 'employer' and record.get('organisation') and item['value'].casefold() != record['organisation'].casefold():
                gap('employer_conflict', 'Public evidence and the recorded employer disagree. Review the sources before changing the record.', 'owner', employer_required)
            if item['kind'] == 'employer' and item['value'].casefold() == (record.get('organisation') or '').casefold() and fresh(dict(item,status='confirmed'), now):
                gaps = [g for g in gaps if g['code'] != 'employer']
            if item['kind'] == 'route' and not selected['value'] and not record.get('preferred_channel') and fresh(dict(item,status='confirmed'), now):
                selected.update(channel='public profile',value=item['value'],verified=False)
                gaps = [g for g in gaps if g['code'] != 'route']
        for text in researched.get('gaps', []):
            gap('source_gap', text, blocking=False)
    sufficient = not any(g['blocking'] for g in gaps)
    status = 'available' if sufficient else 'needs_input' if any(g['blocking'] and g['owner'] == 'owner' for g in gaps) else 'pending'
    return dict(version=VERSION, status=status, label=LABELS[status], sufficient=sufficient, ask=ask,
                objective=plan.get('goal') or '', items=items, route=selected, gaps=gaps,
                next_action=next((g['text'] for g in gaps if g['blocking']), ''), context_sha=fingerprint(record),
                researched_context_sha=(researched or {}).get('context_sha', ''))


def work_brief(record, researched=None, now=None):
    """Work evidence is about a remit and terms, never a personal ask or relationship."""
    # The runtime loads its shared tools before projections run; importing here
    # keeps record_views importable during that entry-point initialization.
    from job_rules import substantive_role_action
    if researched:
        try: validate(researched,record['id'])
        except (ValueError,TypeError,KeyError): researched = None
    # Completeness recognises literal attributed work text. It does not assess
    # suitability, confirm an opening, or promote quotes into recorded fields.
    sourced_work = any(item['kind'] in ('context','role') and len(item['quote']) >= 18 and
                       (item['value']==item['quote'] if item['kind']=='context'
                        else item['value'].casefold() in item['quote'].casefold()) and
                       any(substantive_role_action(clause.strip()) for clause in re.split(r'(?<=[.!?])\s+',item['quote']))
                       for item in (researched or {}).get('items',[]))
    items, gaps = [], []
    incoming = record.get('incoming') or {}
    current_process = bool(incoming.get('current') and incoming.get('status') == record.get('status'))
    if current_process:
        items.append(dict(kind='process', value=incoming.get('label') or record.get('status'),
            provenance='recorded', at=incoming.get('stage_date',''), source_url='', quote='',
            reference=f"record:{record['id']}:incoming"))
    for key in ('summary','engagement','location','pay','ir35'):
        if record.get(key):
            items.append(dict(kind=key,value=record[key],provenance='recorded',at='',source_url='',quote='',
                              reference=f"record:{record['id']}:{key}"))
    if not record.get('summary') and not record.get('advert') and not current_process and not sourced_work:
        gaps.append(dict(code='remit',text='Establish the specific work and requirements.',owner='research',blocking=True))
    if record.get('type')=='job' and evidence_checks.advert_availability(record, now)['status'] != 'open':
        gaps.append(dict(code='vacancy',text='Public vacancy availability is unverified; the recorded hiring conversation remains active.'
                         if current_process else 'Check whether this exact vacancy is still open.',owner='research',blocking=not current_process))
    if not record.get('engagement'):
        gaps.append(dict(code='terms',text='Engagement terms are not stated.',owner='research',blocking=False))
    for item in (researched or {}).get('items',[]):
        items.append(dict(kind=item['kind'],value=item['value'],provenance='public_source',at=item['checked_at'],
                          source_url=item['source_url'],quote=item['quote'],reference=item['source_url']))
    for value in (researched or {}).get('gaps',[]):
        gaps.append(dict(code='source_gap',text=value,owner='research',blocking=False))
    sufficient=not any(g['blocking'] for g in gaps)
    status='available' if sufficient else 'pending'
    return dict(version=VERSION,status=status,label=LABELS[status],sufficient=sufficient,ask='',objective='',
                items=items,route=route(record),gaps=gaps,next_action=next((g['text'] for g in gaps if g['blocking']),''),
                context_sha=fingerprint(record),researched_context_sha=(researched or {}).get('context_sha',''))


def attach(conn, records, *, attempts=None):
    """Batched projection, independent of the three-recent-reviews window."""
    records = [r for r in records if r['type'] in ('person','job','tender','opportunity','company','agency')]
    if not records: return
    # One JSON list, not one bound value per record: SQLite caps bound values (32,766 by default),
    # and the interactions read names every record twice.
    ids = (json.dumps([r['id'] for r in records]),)
    slots = 'SELECT value FROM json_each(?)'
    edits, latest, interactions, prepared = {}, {}, {}, {}
    if attempts is None:
        import research_scope
        attempts = research_scope.latest_attempts(conn)
    for row in conn.execute(f'SELECT record_id,field,actor FROM record_edits WHERE record_id IN ({slots})', ids):
        edits.setdefault(row['record_id'], {})[row['field']] = dict(row)
    # record_edits records ownership, which survives later agent corrections. Actual change events
    # determine authorship of the current wording; ownership alone must not relabel that correction.
    for row in conn.execute(f'SELECT entity_id,field,actor,at,changes_json FROM state_events WHERE entity_id IN ({slots}) ORDER BY id', ids):
        fields = [row['field'], *json.loads(row['changes_json'] or '{}').keys()]
        for field in fields:
            if field in ('known_context','notes','preferred_ask','role','organisation'):
                edits.setdefault(row['entity_id'], {})[field] = dict(actor=row['actor'], edited_at=row['at'])
    corrected = 'corrected_at' in {r['name'] for r in conn.execute('PRAGMA table_info(outcomes)')}
    live = " AND id NOT IN (SELECT interaction_id FROM outcomes WHERE corrected_at IS NOT NULL AND interaction_id IS NOT NULL)" if corrected else ''
    for row in conn.execute(f'SELECT * FROM interactions WHERE (person_id IN ({slots}) OR record_id IN ({slots}))' + live + ' ORDER BY id', ids+ids):
        interactions.setdefault(row['person_id'] or row['record_id'], []).append(dict(row))
    for row in conn.execute(f"SELECT record_id,payload_json FROM observations WHERE record_id IN ({slots}) AND source LIKE 'research:%' AND json_valid(payload_json) "
                            "AND json_type(payload_json,'$.evidence_brief')='object' ORDER BY wo_observation_time(observed_at),id", ids):
        value = json.loads(row['payload_json'])['evidence_brief']
        try: validate(value, row['record_id'])
        except (ValueError, TypeError): continue
        latest[row['record_id']] = value
    for row in conn.execute(f"SELECT record_id,source,observed_at,payload_json FROM observations WHERE record_id IN ({slots}) AND source LIKE 'research:%' "
                            "AND json_valid(payload_json) AND json_type(payload_json,'$.approach')='object' "
                            "ORDER BY wo_observation_time(observed_at),id", ids):
        prepared[row['record_id']] = dict(json.loads(row['payload_json']), source=row['source'], observed_at=row['observed_at'])
    for r in records:
        if r['type'] in ('person','job','tender','opportunity','company','agency'):
            if r['id'] in prepared: r['prepared_approach'] = prepared[r['id']]
            r['evidence_brief'] = build(r, edits.get(r['id']), interactions.get(r['id'], ()), latest.get(r['id']))
            b = r['evidence_brief']; attempt = attempts.get(r['id'], {})
            if attempt.get('brief') and attempt.get('context_sha') == b['context_sha']:
                b['research_attempt'] = {k:attempt.get(k, '') for k in ('attempted_at','outcome','reason','source_checked_at')}
                if not b['sufficient'] and b['status'] != 'needs_input' and attempt.get('outcome') in ('failed','access_blocked','missing_context'):
                    b.update(status='blocked', label=LABELS['blocked'])
