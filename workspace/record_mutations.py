"""Ordinary record changes: create, edit, state transitions, working state, interactions, links,
archive, retype, company numbers and saved jobs, plus organisation and advert identity lookups.

Every function revision-checks inside `record_core.transaction`; nothing sends or schedules anything.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

import db
import normalise
import record_views
import states
from record_identity import match_organisation, ensure_organisation, link_organisation
from record_core import (
    Conflict, PERSON_CHOICES, RELATIONS, STATE_FIELDS, TYPES, WORKING, _values, bump, checked, event, get,
    mark_edited, safe_url, text, transaction,
)


def _sync_person_organisation(conn, rid, actor='owner'):
    row = conn.execute('SELECT * FROM records WHERE id=? AND type=? AND archived_at IS NULL', (rid, 'person')).fetchone()
    if not row:
        return
    comp = (row['organisation'] or '').strip()
    if not normalise.employer_key(comp):
        return
    match = match_organisation(conn, comp)
    if not match and (row['priority'] or row['relationship'] in ('warm', 'active', 'dormant')):
        typ = 'agency' if row['kind'] == 'recruiter' else 'company'
        oid, _ = ensure_organisation(conn, comp, 'manual', db.now(), typ=typ)
        if oid:
            match = dict(conn.execute('SELECT id,type,name FROM records WHERE id=?', (oid,)).fetchone())
    if not match:
        return
    rel = 'consultant_at' if match['type'] == 'agency' or row['kind'] == 'recruiter' else 'works_at'
    exists = conn.execute('SELECT 1 FROM links WHERE ((from_id=? AND to_id=?) OR (from_id=? AND to_id=?)) AND relation IN (?,?)',
                          (rid, match['id'], match['id'], rid, 'works_at', 'consultant_at')).fetchone()
    if not exists:
        conn.execute('INSERT OR IGNORE INTO links(from_id,to_id,relation,notes) VALUES(?,?,?,?)',
                     (rid, match['id'], rel, 'Linked from person organisation'))
        event(conn, rid, 'link', None, match['id'], rel, actor=actor)
        bump(conn, rid)


def create(conn, payload, actor='owner'):
    values=_values(payload)
    typ=payload.get('type')
    if typ not in TYPES or not values.get('name'):
        raise ValueError('Choose a record type and enter a name')
    if 'cv_override' in values and typ != 'job':
        raise ValueError('Only jobs have a CV choice')
    if any(values.get(k) for k in PERSON_CHOICES) and typ != 'person':
        raise ValueError('Only people have a channel, an ask or a place on your list')
    for k in STATE_FIELDS & payload.keys():
        values[k]=states.validate(states.TYPE_ENTITY.get(typ,'record'),k,payload[k])
    if typ == 'person' and values.get('relationship') == 'do_not_contact' and values.get('priority'):
        raise ValueError('Promote this person to current contacts before placing them on your priority list')
    rid={'person':'CP','company':'CO','agency':'AG','opportunity':'OP','job':'JOB','tender':'TN','event':'CE','source':'CS'}[typ]+'-'+uuid.uuid4().hex[:12]
    at=db.now()
    with transaction(conn):
        row=dict(id=rid,type=typ,created_at=at,updated_at=at,**values)
        conn.execute('INSERT INTO records('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
        mark_edited(conn,rid,values,actor)
        event(conn,rid,'created',None,typ,actor=actor)
        if typ == 'person' and values.get('organisation'):
            _sync_person_organisation(conn, rid, actor=actor)
        if typ == 'job' and values.get('status') == 'pursue' and record_views.record_exclusions(conn).get(rid):
            raise Conflict('This employer is excluded from your job search')
    return {'saved':True,'record':get(conn,rid)}


def job_url_index(conn):
    """All stored advert URLs, including archived evidence; used once per import transaction."""
    index = {}
    def remember(rid, value):
        if not value:
            return
        try:
            url = normalise.job_url(value)
        except ValueError:
            return  # Retain malformed legacy evidence without using it as identity.
        index.setdefault(url, set()).add(rid)
    for row in conn.execute("SELECT id,url,detail_url,apply_url FROM records WHERE type='job'"):
        for field in ('url', 'detail_url', 'apply_url'):
            remember(row['id'], row[field])
    # A title/company attachment can preserve the owner's preferred URL while retaining another
    # source's advert link. That observed URL already belongs to this same job as well.
    for row in conn.execute("SELECT o.record_id,COALESCE(json_extract(o.payload_json,'$.current.url'),"
                            "json_extract(o.payload_json,'$.url')) AS url FROM observations o "
                            "JOIN records r ON r.id=o.record_id WHERE r.type='job' "
                            "AND (json_extract(o.payload_json,'$.kind')='job' OR o.source LIKE 'alert:%' OR "
                            "o.source LIKE 'board:%' OR o.source LIKE 'feed:%' OR o.source LIKE 'hn:%' OR o.source='incoming')"):
        remember(row['record_id'], row['url'])
    return index


def active_job_survivor(conn, rid):
    """Follow only evidenced merge edges. A plain archive is a deliberate stop, never a new job."""
    seen = set()
    while rid not in seen:
        seen.add(rid)
        row = conn.execute("SELECT id,archived_at FROM records WHERE id=? AND type='job'", (rid,)).fetchone()
        if row is None:
            break
        if not row['archived_at']:
            return rid
        edge = conn.execute("SELECT to_value FROM state_events WHERE entity_id=? AND field='archived' ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        if not edge or not conn.execute("SELECT 1 FROM state_events WHERE entity_id=? AND field='merged' AND from_value=?", (edge[0], rid)).fetchone():
            break
        rid = edge[0]
    raise Conflict('This job was archived. Review its history before saving it again.')


def match_job_url(conn, url, index=None):
    """One canonical URL may resolve to only one active survivor; ambiguity needs human review."""
    canonical = normalise.job_url(url)
    matches = (job_url_index(conn) if index is None else index).get(canonical, set())
    survivors = {active_job_survivor(conn, rid) for rid in matches}
    if len(survivors) > 1:
        raise Conflict('More than one job has this URL. Review the existing records before saving.')
    return next(iter(survivors), None)


def save_job(conn, payload, actor='owner'):
    """Manually save a URL to the shortlist with its organisation in one writer transaction."""
    unknown = set(payload) - {'url', 'name', 'organisation', 'summary', 'notes'}
    if unknown:
        raise ValueError('Unsupported fields: ' + ', '.join(sorted(unknown)))
    values = normalise.normalise_values({k: text(v, 20000 if k == 'notes' else 4000) for k, v in payload.items()})
    if not values.get('name'):
        raise ValueError('Enter the job title')
    if not values.get('organisation'):
        # An unknown employer is not a manual correction: later source evidence may fill it.
        values.pop('organisation', None)
    values['url'] = normalise.job_url(values.get('url', ''))
    with transaction(conn):
        rid = match_job_url(conn, values['url'])
        if rid:
            record = get(conn, rid)
            if record.get('exclusion'):
                raise Conflict(record['exclusion']['reason'])
            return dict(created=False, record=record)
        # Imports and manual saves share organisation identity, including archived names.
        record = create(conn, dict(type='job', status='pursue', **values), actor=actor)['record']
        if values.get('organisation'):
            organisation = link_organisation(conn, record['id'], values['organisation'], 'hiring_for', 'manual', db.now())
            if not organisation:
                raise Conflict('This organisation was archived. Review it before saving a new job.')
        record = get(conn, record['id'])
        if record.get('exclusion'):
            raise Conflict(record['exclusion']['reason'])
        return dict(created=True, record=record)


def edit(conn, rid, payload, actor='owner'):
    if STATE_FIELDS & payload.keys() or 'type' in payload:
        raise ValueError('Use a validated transition to change stage or relationship')
    values=_values(payload)
    with transaction(conn):
        row=checked(conn,rid,payload.get('revision'))
        if 'cv_override' in values and row['type'] != 'job':
            raise ValueError('Only jobs have a CV choice')
        if any(values.get(k) for k in PERSON_CHOICES) and row['type'] != 'person':
            raise ValueError('Only people have a channel, an ask or a place on your list')
        if row['type'] == 'person' and values.get('priority') and row['relationship'] == 'do_not_contact':
            raise ValueError('Promote this person to current contacts before placing them on your priority list')
        changed={k:v for k,v in values.items() if row[k]!=v}
        if changed:
            conn.execute('UPDATE records SET '+','.join(k+'=?' for k in changed)+' WHERE id=?',[*changed.values(),rid])
            mark_edited(conn,rid,changed,actor)
            for k,v in changed.items(): event(conn,rid,k,row[k],v,actor=actor)
            bump(conn,rid)
            if row['type'] == 'person' and ('organisation' in changed or 'priority' in changed):
                _sync_person_organisation(conn, rid, actor=actor)
    return {'saved':True,'record':get(conn,rid)}


def transition(conn, rid, payload, actor='owner'):
    field=payload.get('field');value=payload.get('to')
    if field not in STATE_FIELDS: raise ValueError('Unknown state field')
    with transaction(conn):
        row=checked(conn,rid,payload.get('revision'))
        entity=states.TYPE_ENTITY.get(row['type'],'record')
        value=states.validate(entity,field,value)
        allowed=states.allowed_from(entity,field,row[field])
        if value not in allowed:
            raise ValueError(states.transition_refusal(entity, field, row[field], value, allowed))
        if row['type'] == 'job' and field == 'status' and value == 'pursue':
            exclusion = record_views.record_exclusions(conn).get(rid)
            if exclusion:
                raise Conflict(exclusion['reason'])
        changes = {}
        if row['type'] == 'person' and field == 'relationship' and value == 'do_not_contact' and row['priority']:
            conn.execute('UPDATE records SET priority=NULL WHERE id=?', (rid,))
            mark_edited(conn, rid, ['priority'], actor)
            event(conn, rid, 'priority', row['priority'], None, 'Cleared when moved to Not contacting', actor=actor)
            changes['priority'] = dict(before=row['priority'], after=None)
        conn.execute('UPDATE records SET '+field+'=? WHERE id=?',(value,rid))
        mark_edited(conn,rid,[field],actor);event(conn,rid,field,row[field],value,text(payload.get('note','')),actor=actor,changes=changes or None);bump(conn,rid)
        if row['type'] == 'person' and field in ('relationship', 'kind'):
            _sync_person_organisation(conn, rid, actor=actor)
    return {'saved':True,'record':get(conn,rid)}


def followup(conn, payload, actor='owner'):
    if 'due' in payload: raise ValueError('Task deadlines have been retired; use a working state')
    rid=payload.get('id');action=text(payload.get('action',''),240)
    status=payload.get('status','needs_review');owner=payload.get('owner','owner')
    reason=text(payload.get('reason',''),2000);waiting=text(payload.get('waiting_on',''),240);notes=text(payload.get('notes',''),2000)
    if not action or status not in WORKING or owner not in ('owner','agent'):
        raise ValueError('Enter an action and choose a valid working state and owner')
    if status in ('waiting','blocked') and not (reason and waiting):
        raise ValueError('Waiting and blocked items need a reason and who or what they depend on')
    with transaction(conn):
        row=checked(conn,rid,payload.get('revision'))
        old_followup=conn.execute('SELECT notes FROM followups WHERE record_id=?',(rid,)).fetchone()
        conn.execute("INSERT INTO followups(record_id,action,owner,status,status_raw,title,notes,source,updated_at,reason,waiting_on) "
                     "VALUES(?,?,?,?,?,?,?,'local',?,?,?) ON CONFLICT(record_id) DO UPDATE SET action=excluded.action,owner=excluded.owner,"
                     "status=excluded.status,status_raw=excluded.status_raw,notes=excluded.notes,source='local',"
                     'updated_at=excluded.updated_at,reason=excluded.reason,waiting_on=excluded.waiting_on',
                     (rid,action,owner,status,status,row['name'],notes,db.now(),reason,waiting))
        conn.execute('UPDATE records SET working_state=?,status_reason=?,waiting_on=?,next_action=?,owner=? WHERE id=?',
                     (status,reason,waiting,action,owner,rid))
        mark_edited(conn,rid,['next_action','owner','working_state','status_reason','waiting_on'],actor)
        after=dict(working_state=status,status_reason=reason,waiting_on=waiting,next_action=action,owner=owner)
        changes={k:dict(before=row[k],after=v) for k,v in after.items() if row[k]!=v}
        old_notes=old_followup['notes'] if old_followup else ''
        if old_notes!=notes:changes['followup_notes']=dict(before=old_notes,after=notes)
        event(conn,rid,'working_state',row['working_state'],status,reason or action,actor=actor,changes=changes);bump(conn,rid)
    return {'saved':True,'record':get(conn,rid),'destination':'work-ops database'}


def interaction(conn, payload, actor='owner'):
    rid=payload.get('record_id');at=text(payload.get('at',''),50)
    try: dt.datetime.fromisoformat(at.replace('Z','+00:00'))
    except ValueError: raise ValueError('Use an ISO date or timestamp') from None
    if at[:10]>dt.date.today().isoformat(): raise ValueError('An interaction records something that already happened')
    channel=states.validate('interaction','channel',payload.get('channel'))
    direction=states.validate('interaction','direction',payload.get('direction'))
    summary=text(payload.get('summary',''),4000)
    if not channel or not direction or not summary: raise ValueError('Channel, direction and summary are required')
    evidence=text(payload.get('evidence_ref',''))
    nested=conn.in_transaction
    with transaction(conn):
        row=checked(conn,rid,payload.get('revision'))
        cur=conn.execute('INSERT INTO interactions(at,channel,direction,person_id,org_id,record_id,summary,evidence_ref,source,created_at) '
                         "VALUES(?,?,?,?,?,?,?,?,'manual',?)",(at,channel,direction,rid if row['type']=='person' else None,
                         rid if row['type'] in ('company','agency') else None,rid,summary,evidence,db.now()))
        changes={}
        if direction in ('in','out') and (not re.match(r'^\d{4}-\d{2}-\d{2}',row['last_touch']) or at>row['last_touch']):
            conn.execute('UPDATE records SET last_touch=? WHERE id=?',(at,rid));mark_edited(conn,rid,['last_touch'],actor)
            changes['last_touch']=dict(before=row['last_touch'],after=at)
        iid=cur.lastrowid
        event(conn,rid,'interaction',None,str(iid),summary,actor=actor,changes=changes);bump(conn,rid)
    if nested:
        return {'saved':True,'interaction_id':iid}
    return {'saved':True,'record':get(conn,rid),'interaction_id':iid}


def link(conn, payload, actor='owner'):
    rid=payload.get('from_id');target=payload.get('to_id');relation=payload.get('relation')
    if relation not in RELATIONS or rid==target: raise ValueError('Choose a relationship and a different record')
    with transaction(conn):
        checked(conn,rid,payload.get('revision'));get(conn,target)
        previous=conn.execute('SELECT notes FROM links WHERE from_id=? AND to_id=? AND relation=?',(rid,target,relation)).fetchone()
        if previous and previous['notes']==text(payload.get('notes','')):
            return {'saved':True,'record':get(conn,rid)}
        conn.execute('INSERT INTO links(from_id,to_id,relation,notes) VALUES(?,?,?,?) ON CONFLICT(from_id,to_id,relation) DO UPDATE SET notes=excluded.notes',
                     (rid,target,relation,text(payload.get('notes',''))))
        event(conn,rid,'link',None,target,relation,actor=actor);bump(conn,rid)
    return {'saved':True,'record':get(conn,rid)}


def normalise_record(conn, rid, actor='agent'):
    """Apply the shared import normalisation to a stored record (entities, whitespace, factual dates).
    It restates the same facts, so the fields are not marked as edits and later imports still refresh them."""
    with transaction(conn):
        row=conn.execute('SELECT * FROM records WHERE id=?',(rid,)).fetchone()
        if row is None: raise ValueError('Unknown record')
        fields=[*normalise.TEXT_FIELDS,*normalise.DATE_FIELDS]
        before={k:row[k] or '' for k in fields}
        after=normalise.normalise_values(before)
        changed={k:v for k,v in after.items() if v!=before[k]}
        if changed:
            conn.execute('UPDATE records SET '+','.join(k+'=?' for k in changed)+' WHERE id=?',[*changed.values(),rid])
            for k,v in changed.items(): event(conn,rid,k,before[k],v,'Normalised: entities, whitespace or a non-date in a date field',actor=actor)
            bump(conn,rid)
    return changed


def archive(conn, rid, payload, actor='owner'):
    """Take a record out of the working set. It stays in the database with its history."""
    reason=text(payload.get('reason',''),1000)
    if not reason: raise ValueError('Say why the record is archived')
    with transaction(conn):
        row=checked(conn,rid,payload.get('revision'))
        if row['archived_at']: raise ValueError('The record is already archived')
        conn.execute('UPDATE records SET archived_at=? WHERE id=?',(db.now(),rid))
        event(conn,rid,'archived',None,payload.get('into') or 'archived',reason,actor=actor);bump(conn,rid)
    return {'saved':True,'record':get(conn,rid)}


def retype(conn, rid, payload, actor='owner'):
    """Correct an organisation's type in place (company and agency only), e.g. a recruiter recorded as an
    employer. Its id, links and history stay; its adverts then route via the agency."""
    to=payload.get('type');reason=text(payload.get('reason',''),1000)
    if to not in ('company','agency'): raise ValueError('An organisation is a company or an agency')
    if not reason: raise ValueError('Say why the type is wrong')
    with transaction(conn):
        row=checked(conn,rid,payload.get('revision'))
        if row['type'] not in ('company','agency'): raise ValueError('Only a company or an agency can change type')
        if row['type']==to: raise ValueError(f'The record is already a {to}')
        kind=to if (row['kind'] or row['type']) in ('company','agency') else row['kind']
        conn.execute('UPDATE records SET type=?,kind=? WHERE id=?',(to,kind,rid))
        event(conn,rid,'type',row['type'],to,reason,actor=actor);bump(conn,rid)
    return {'saved':True,'record':get(conn,rid)}


REGISTRY = 'companies-house'
COMPANY_NUMBER = re.compile(r'^(?:\d{8}|[A-Z]{2}\d{6})$')


def company_number(conn, rid, payload, actor='owner'):
    """Record which Companies House company an organisation is, on evidence that states it (its own
    site's footer or terms, for example). The daily reader reads only numbers recorded here; a name is
    never matched automatically. `number` None removes a wrong one."""
    number=(text(payload.get('number','')) or '').upper().replace(' ','') or None
    evidence=safe_url(payload.get('evidence',''))
    if number and not COMPANY_NUMBER.match(number): raise ValueError('A company number is 8 digits, or 2 letters and 6 digits')
    if not evidence: raise ValueError('Give the page that states the company number')
    with transaction(conn):
        row=checked(conn,rid,payload.get('revision'))
        if row['type'] not in ('company','agency') or row['archived_at']: raise ValueError('Only an active company or agency has a company number')
        other=conn.execute('SELECT s.record_id FROM source_records s JOIN records r ON r.id=s.record_id WHERE s.source_scope=? '
                           'AND s.source_id=? AND s.record_id<>? AND r.archived_at IS NULL',(REGISTRY,number,rid)).fetchone() if number else None
        if other: raise ValueError(f'Company {number} is already recorded for {other[0]}')
        old=conn.execute('SELECT source_id FROM source_records WHERE source_scope=? AND record_id=?',(REGISTRY,rid)).fetchone()
        old=old[0] if old else None
        if old==number: raise ValueError('That company number is already recorded')
        conn.execute('DELETE FROM source_records WHERE source_scope=? AND record_id=?',(REGISTRY,rid))
        if number: conn.execute('INSERT OR REPLACE INTO source_records VALUES(?,?,?)',(REGISTRY,number,rid))
        event(conn,rid,'company_number',old,number,evidence,actor=actor);bump(conn,rid)
    return {'saved':True,'record':get(conn,rid)}
