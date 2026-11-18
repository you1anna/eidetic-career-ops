"""Reported outcomes (sent, replied, submitted…), their corrections, and handover into Work.

Outcomes record what the owner reports; they never send anything or infer a commercial transition.
"""
from __future__ import annotations

import datetime as dt
import json

import db
import machines
import states
from approaches import NEXT_STEP_KINDS as _NEXT_STEP_KINDS
from record_views import outcome_view
from record_core import (
    Conflict, STATE_FIELDS, _columns, bump, checked, event, get, mark_edited, text, transaction,
)
from record_mutations import create, interaction, link, match_organisation
from workspace_state import cached_context


# A repeated tap within this window is the same fact, not a second send.
OUTCOME_REPEAT_SECONDS = 120


# A person's preferred channel as an interaction channel; the others are the same word.
OUTCOME_CHANNELS = {'in_person': 'meeting'}


def _step_value(value, actor):
    if actor != 'owner':
        raise ValueError('A next step records the owner’s choice; background work cannot replace it')
    if not isinstance(value, dict) or set(value) - {'kind', 'text', 'agreed_on'}:
        raise ValueError('Choose a next step, completion or explicit clearing')
    kind = value.get('kind')
    if kind not in ('mine', 'waiting', 'done', 'none'):
        raise ValueError('Choose whose next step this is')
    body = text(value.get('text'), 500)
    agreed = text(value.get('agreed_on'), 10)
    if kind in ('mine', 'waiting') and not body:
        raise ValueError('Describe the actual next step, or choose no next step')
    if agreed:
        try:
            if dt.date.fromisoformat(agreed).isoformat() != agreed: raise ValueError()
        except ValueError: raise ValueError('Use an ISO date actually agreed or promised') from None
    return dict(kind=kind, text='' if kind == 'none' else body, agreed_on=agreed if kind in ('mine', 'waiting') else '')


def _current_step(conn, rid, aid):
    row = conn.execute('SELECT step_json,revision FROM approach_next_steps WHERE record_id=? AND action_id=?', (rid, aid)).fetchone()
    return dict(value=json.loads(row['step_json']), revision=row['revision']) if row else None


def _write_step(conn, rid, aid, value, actor, action=None):
    """Inside the caller's short transaction. No contact/commercial state changes or queries per card."""
    before = _current_step(conn, rid, aid)
    if (before and before['value'] == value) or (not before and value['kind'] == 'none'):
        return None
    after = dict(value=value, revision=(before['revision'] if before else 0) + 1)
    context = {key:action.get(key) for key in ('route', 'company_id', 'person_id')} if action else {}
    if action and action.get('trigger'):
        context['trigger'] = {'kind':action['trigger'].get('kind')}
    conn.execute('INSERT INTO approach_next_steps(record_id,action_id,step_json,context_json,revision,updated_at,actor) VALUES(?,?,?,?,?,?,?) '
                 'ON CONFLICT(record_id,action_id) DO UPDATE SET step_json=excluded.step_json,revision=excluded.revision,'
                 'updated_at=excluded.updated_at,actor=excluded.actor', (rid, aid, json.dumps(value), json.dumps(context), after['revision'], db.now(), actor))
    label = {'mine':'Your next step', 'waiting':'Waiting for', 'done':'Next step completed', 'none':'Next step cleared'}[value['kind']]
    note = label + (': ' + value['text'] if value['text'] else '') + (f" · agreed {value['agreed_on']}" if value['agreed_on'] else '')
    event(conn, rid, 'approach_next_step', json.dumps(before['value']) if before else None, json.dumps(value), note,
          actor=actor, changes={'action_id':aid})
    return dict(before=before, after=after)


def _approach(conn, rid, aid):
    if not aid: raise ValueError('Choose the approach whose next step you are recording')
    intelligence = cached_context(conn)['intelligence']
    action = next((a for a in [*intelligence['actions'], *intelligence['research']]
                   if a.get('id') == aid and a.get('record_id') == rid), None)
    if not action: raise Conflict('This recommended action changed. Reload it before recording its outcome.')
    return action


def next_step(conn, rid, payload, actor='owner'):
    """Change a human commitment without manufacturing a send, reply or commercial transition."""
    aid = text(payload.get('action_id'), 240)
    value = _step_value(payload.get('next_step'), actor)
    action = _approach(conn, rid, aid)  # Derived work stays outside the write lock.
    if action.get('kind') not in _NEXT_STEP_KINDS:
        raise ValueError('Use the existing process or Work next step for this record')
    with transaction(conn):
        checked(conn, rid, payload.get('revision'))
        change = _write_step(conn, rid, aid, value, actor, action)
        if change: bump(conn, rid)
    return {'saved':bool(change), 'record':get(conn, rid), 'next_step':value}


def outcome(conn, rid, payload, actor='owner'):
    """Record a reported event; never send anything or infer a commercial state transition."""
    kind=payload.get('kind');note=text(payload.get('note'),2000);version=payload.get('draft_version_id')
    action_id=text(payload.get('action_id'),240)
    step_value = _step_value(payload['next_step'], actor) if 'next_step' in payload else None
    if step_value is not None and not action_id:
        raise ValueError('Choose the approach whose next step you are recording')
    # A reported sent message is evidence, not a replacement for the current draft.
    # Validate before taking the write lock; preserve the supplied whitespace exactly.
    sent_message = payload.get('sent_message')
    if 'sent_message' in payload:
        if kind not in ('sent', 'followed_up') or version is not None:
            raise ValueError('Use reported wording or a saved draft for a contact, not both')
        if not text(sent_message, 8000):
            raise ValueError('Paste the sent message, or record contact without wording')
    # How the owner got in touch: a person's channel (Release 2) or any declared interaction channel.
    channel=payload.get('channel') or None
    if channel:
        channel=states.validate('interaction','channel',OUTCOME_CHANNELS.get(channel,channel))
    # Where a CV went (the owner's rule, 15 October): the company behind a submission, for the referral clash warning.
    company=text(payload.get('company'),120)
    if kind=='submitted' and not company:
        raise ValueError('Enter the company your CV was submitted to')
    if company and kind!='submitted':
        raise ValueError('A company applies only to a CV submission')
    snapshot={}
    if action_id:
        # The recommendation is derived. Read it before the write lock; the transaction re-checks the revision.
        action = _approach(conn, rid, action_id)
        if step_value is not None and action.get('kind') not in _NEXT_STEP_KINDS:
            raise ValueError('Use the existing process or Work next step for this record')
        expected=action.get('message_version_id') or action.get('draft_version_id')
        if version is not None and expected!=version:
            raise Conflict('This action now refers to a different draft. Reload it before recording its outcome.')
        snapshot={k:action[k] for k in ('route','company_id','person_id','fit','access','confidence','brief') if k in action}
        snapshot.update(action_id=action_id,hypothesis=action.get('brief',{}).get('hypothesis',''),
                        playbook=action.get('playbook') or action.get('fit',{}).get('label',''))
    if sent_message is not None:
        snapshot.update(message=sent_message, message_source='reported')
    early=None
    oid=None
    with transaction(conn):
        # A second tap on the same fact is the same event, even from a page that has not reloaded.
        repeat=conn.execute("SELECT * FROM outcomes WHERE record_id=? AND kind=? AND corrected_at IS NULL "
                            "AND COALESCE(json_extract(snapshot_json,'$.action_id'),'')=? AND at>=? "
                            "AND COALESCE(json_extract(snapshot_json,'$.target.name'),'')=? ORDER BY id DESC LIMIT 1",
                            (rid,kind,action_id,(dt.datetime.now().astimezone()-dt.timedelta(seconds=OUTCOME_REPEAT_SECONDS)).isoformat(timespec='seconds'),
                             company)).fetchone()
        if repeat:
            early=('repeat', outcome_view(repeat))
        else:
            row=checked(conn,rid,payload.get('revision'))
            if kind not in {a['kind'] for a in states.outcome_actions(dict(row))}:
                raise ValueError('Choose an offered outcome for this record')
            if version is not None:
                draft_owner=conn.execute('SELECT record_id FROM draft_versions WHERE id=?',(version,)).fetchone() if type(version) is int else None
                related=False
                if draft_owner and draft_owner['record_id']!=rid and row['type']=='person':
                    related=bool(conn.execute("SELECT 1 FROM links l JOIN records r ON r.id=? AND r.type IN ('company','agency') "
                        "WHERE l.relation IN ('works_at','consultant_at') AND ((l.from_id=? AND l.to_id=?) OR (l.to_id=? AND l.from_id=?))",
                        (draft_owner['record_id'],rid,draft_owner['record_id'],rid,draft_owner['record_id'])).fetchone())
                if not draft_owner or (draft_owner['record_id']!=rid and not related):
                    raise ValueError('The draft version must belong to this record or its explicitly linked company or agency')
                existing=conn.execute("SELECT * FROM outcomes WHERE record_id=? AND draft_version_id=? AND kind=? AND corrected_at IS NULL "
                                      "AND COALESCE(json_extract(snapshot_json,'$.action_id'),'')=? "
                                      "AND COALESCE(json_extract(snapshot_json,'$.target.name'),'')=?",
                                      (rid,version,kind,action_id,company)).fetchone()
                if existing:
                    early=('existing', outcome_view(existing))
            if early is None:
                if action_id:
                    machines.require_progress_event(action.get('kind'), (action.get('progress') or {}).get('status'), kind)
                if version is not None and 'purpose' in _columns(conn,'draft_versions'):
                    ver=conn.execute('SELECT text,purpose FROM draft_versions WHERE id=?',(version,)).fetchone()
                    if ver and ver['purpose']=='message':
                        snapshot['message']=ver['text']
                at=db.now()
                label=states.OUTCOME_KINDS[kind]
                if channel:
                    snapshot['channel']=channel
                if company:
                    org=match_organisation(conn,company)
                    snapshot['target']=dict(name=company,**({'id':org['id']} if org else {}))
                # Share the existing factual interaction history, including last_touch for sent/replied.
                result=interaction(conn,dict(record_id=rid,revision=row['revision'],at=at,channel=channel or 'other',
                                   direction='out' if kind in ('sent','followed_up') else 'in' if kind=='replied' else 'note',
                                   summary=label+(f' to {company}' if company else '')+(f': {note}' if note else '')),actor=actor)
                if step_value is not None:
                    changed = _write_step(conn, rid, action_id, step_value, actor, action)
                    if changed: snapshot['next_step_change'] = changed
                oid=conn.execute('INSERT INTO outcomes(record_id,kind,at,note,draft_version_id,actor,interaction_id,snapshot_json) VALUES(?,?,?,?,?,?,?,?)',
                                (rid,kind,at,note,version,actor,result['interaction_id'],json.dumps(snapshot,ensure_ascii=False))).lastrowid
                event(conn,rid,'outcome',None,kind,note or label,actor)
    if early:
        body={'saved':False,'record':get(conn,rid),'outcome':early[1]}
        if early[0]=='repeat':
            body['duplicate']=True
        return body
    return {'saved':True,'record':get(conn,rid),'outcome':outcome_view(conn.execute('SELECT * FROM outcomes WHERE id=?',(oid,)).fetchone())}


def _take_back_contact(conn, rid, row, iid):
    """A corrected outcome's own interaction stops counting as contact (live_interactions hides it), and
    last_touch goes back to its value before that interaction, unless a later contact has moved it since."""
    hit=conn.execute('SELECT at FROM interactions WHERE id=? AND record_id=?',(iid,rid)).fetchone() if iid else None
    if not hit or row['last_touch']!=hit['at']:
        return {}
    made=conn.execute("SELECT changes_json FROM state_events WHERE entity_id=? AND field='interaction' AND to_value=? "
                      'ORDER BY id DESC LIMIT 1',(rid,str(iid))).fetchone()
    before=(json.loads(made['changes_json'] or '{}').get('last_touch') or {}) if made else {}
    if 'before' not in before:
        return {}
    conn.execute('UPDATE records SET last_touch=? WHERE id=?',(before['before'],rid))
    return {'last_touch':dict(before=row['last_touch'],after=before['before'])}


def _take_back_handover(conn, snap, actor):
    """A corrected handover archives the opportunity it made, so handing over again never leaves two. Once
    that opportunity has changed in Work, the handover stands and the opportunity is changed there."""
    opp=conn.execute('SELECT * FROM records WHERE id=?',(snap.get('opportunity_id') or '',)).fetchone()
    if not opp or opp['archived_at']:
        return
    if opp['revision']!=snap.get('opportunity_revision'):
        raise ValueError('The opportunity in Work has changed since the handover. Change or close it there.')
    conn.execute('UPDATE records SET archived_at=? WHERE id=?',(db.now(),opp['id']))
    event(conn,opp['id'],'archived',None,'archived','Handover undone',actor=actor);bump(conn,opp['id'])


def correct(conn, rid, payload, actor='owner'):
    """Undo a mistake without erasing it: mark one outcome corrected (its recorded contact and
    last_touch go with it), or revert the latest change of a state field to its previous value.
    Both stay in the history with the reason."""
    note=text(payload.get('note',''),2000)
    with transaction(conn):
        row=checked(conn,rid,payload.get('revision'))
        if payload.get('outcome_id') is not None:
            oid=payload['outcome_id']
            hit=conn.execute('SELECT * FROM outcomes WHERE id=? AND record_id=?',(oid,rid)).fetchone() if type(oid) is int else None
            if not hit:
                raise ValueError('Choose an outcome recorded on this record')
            if hit['corrected_at']:
                raise ValueError('This entry is already corrected')
            if hit['kind']=='handed_over':
                _take_back_handover(conn,json.loads(hit['snapshot_json'] or '{}'),actor)
            snap = json.loads(hit['snapshot_json'] or '{}')
            step_change = snap.get('next_step_change')
            if step_change:
                current = _current_step(conn, rid, snap['action_id'])
                # Later human edits win over correction of an earlier contact.
                if current and current['revision'] == step_change['after']['revision']:
                    previous = (step_change.get('before') or {}).get('value') or dict(kind='none', text='', agreed_on='')
                    _write_step(conn, rid, snap['action_id'], previous, actor)
            conn.execute('UPDATE outcomes SET corrected_at=?,corrected_by=?,correction_note=? WHERE id=?',(db.now(),actor,note,oid))
            label=states.OUTCOME_KINDS.get(hit['kind'],hit['kind'])
            event(conn,rid,'outcome',hit['kind'],None,f'Corrected: {label}'+(f' — {note}' if note else ''),actor=actor,
                  changes=_take_back_contact(conn,rid,row,hit['interaction_id']))
        else:
            field=payload.get('field')
            if field not in STATE_FIELDS:
                raise ValueError('Choose an outcome or a state field to correct')
            last=conn.execute('SELECT * FROM state_events WHERE entity_id=? AND field=? ORDER BY id DESC LIMIT 1',(rid,field)).fetchone()
            if not last or last['to_value']!=row[field] or (last['note'] or '').startswith('Corrected'):
                raise ValueError('There is no recent change of this field to correct')
            conn.execute('UPDATE records SET '+field+'=? WHERE id=?',(last['from_value'],rid))
            mark_edited(conn,rid,[field],actor)
            prior_changes = json.loads(last['changes_json'] or '{}')
            priority_before = (prior_changes.get('priority') or {}).get('before')
            if field == 'relationship' and priority_before and not row['priority']:
                conn.execute('UPDATE records SET priority=? WHERE id=?', (priority_before, rid))
                mark_edited(conn, rid, ['priority'], actor)
                event(conn, rid, 'priority', None, priority_before, 'Restored when relationship change was undone', actor=actor)
            event(conn,rid,field,row[field],last['from_value'],'Corrected'+(f': {note}' if note else ''),actor=actor)
        bump(conn,rid)
    return {'saved':True,'record':get(conn,rid)}


def handover(conn, rid, payload, actor='owner'):
    """A qualified conversation becomes an opportunity in Work, linked to the person and company,
    with the agreed next step. Recorded once per approach; nothing is sent."""
    next_step=text(payload.get('next_step',''),500);company_id=text(payload.get('company_id',''),80)
    action_id=text(payload.get('action_id',''),240)
    with transaction(conn):
        existing=conn.execute("SELECT * FROM outcomes WHERE record_id=? AND kind='handed_over' AND corrected_at IS NULL "
                              "AND COALESCE(json_extract(snapshot_json,'$.action_id'),'')=? ORDER BY id DESC LIMIT 1",(rid,action_id)).fetchone()
        if existing:
            opp=json.loads(existing['snapshot_json']).get('opportunity_id')
            return {'saved':False,'record':get(conn,rid),'opportunity':get(conn,opp)}
        row=checked(conn,rid,payload.get('revision'))
        company=conn.execute("SELECT * FROM records WHERE id=? AND type IN ('company','agency')",(company_id,)).fetchone() if company_id else None
        if company_id and not company:
            raise ValueError('Choose the company this conversation is with')
        if row['type']=='opportunity':
            raise ValueError('This is already an opportunity in Work')
        who=company['name'] if company else row['organisation'] or row['name']
        person=row['name'] if row['type']=='person' else ''
        opp=create(conn,dict(type='opportunity',name=f"{who}: conversation{' with '+person if person and person!=who else ''}",
                             stage='conversation',organisation=who if company else row['organisation'] or '',
                             summary=text(payload.get('summary',''),4000)),actor=actor)['record']
        if next_step:
            conn.execute('UPDATE records SET next_action=? WHERE id=?',(next_step,opp['id']))
            mark_edited(conn,opp['id'],['next_action'],actor)
            event(conn,opp['id'],'next_action','',next_step,'Agreed at handover',actor=actor);bump(conn,opp['id'])
            opp=get(conn,opp['id'])
        for target in [rid]+([company['id']] if company and company['id']!=rid else []):
            opp=link(conn,dict(from_id=opp['id'],to_id=target,relation='related',revision=opp['revision'],
                               notes='Handed over from outreach'),actor=actor)['record']
        snapshot=dict(action_id=action_id,opportunity_id=opp['id'],company_id=company['id'] if company else None,
                      opportunity_revision=opp['revision'])
        result=interaction(conn,dict(record_id=rid,revision=row['revision'],at=db.now(),channel='other',direction='note',
                                     summary=f"Handed over to Work as {opp['name']}"+(f': {next_step}' if next_step else '')),actor=actor)
        conn.execute('INSERT INTO outcomes(record_id,kind,at,note,actor,interaction_id,snapshot_json) VALUES(?,?,?,?,?,?,?)',
                     (rid,'handed_over',db.now(),next_step,actor,result['interaction_id'],json.dumps(snapshot)))
        event(conn,rid,'outcome',None,'handed_over',f"Handed over to Work: {opp['name']}",actor)
    return {'saved':True,'record':get(conn,rid),'opportunity':get(conn,opp['id'])}
