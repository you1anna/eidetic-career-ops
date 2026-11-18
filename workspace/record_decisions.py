"""The owner's decisions, his "Ask Claude…" card questions, and the drafts an approval is bound to.

The agent asks with a declared kind; the owner answers on the site; the agent processes the answer
later. An answer changes no record. Only the owner answers, through the workspace API.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import db
import machines
import states
from record_core import Conflict, _columns, bump, checked, event, get, safe_url, text, transaction
from record_mutations import followup
# Compatibility exports for existing callers; mutations and timelines share these projections.
from decision_views import (decision_choices, decision_view, get_decision, decisions_history,
                            decisions_state, _legacy_reconfirmable)


# A record with a live decision waits on it: its next step is the question, owned by the owner. Processing
# the decision closes that step, unless the agent has since given the record a different one.
DECISION_WAITING = states.DECISION_WAITING


def decision_marker(did):
    return f'Decision #{did} on the site'


def _record_waits(conn, rid, did, question, actor):
    rev = conn.execute('SELECT revision FROM records WHERE id=?', (rid,)).fetchone()[0]
    followup(conn, dict(id=rid, action=question, owner='owner', status='waiting', reason=decision_marker(did),
                        waiting_on=DECISION_WAITING, revision=rev), actor=actor)


def _record_decided(conn, rid, did, summary, actor):
    f = conn.execute('SELECT reason,waiting_on FROM followups WHERE record_id=?', (rid,)).fetchone()
    if f and f['reason'] == decision_marker(did) and f['waiting_on'] == DECISION_WAITING:
        rev = conn.execute('SELECT revision FROM records WHERE id=?', (rid,)).fetchone()[0]
        followup(conn, dict(id=rid, action=summary[:240], owner='agent', status='done', revision=rev), actor=actor)


def create_decision(conn, payload, actor='agent'):
    """Ask the owner something. Returns the live decision unchanged when one is already open for the
    same subject and kind, so asking again is safe."""
    kind = payload.get('kind')
    if kind not in states.DECISION_KINDS:
        raise ValueError('Unknown decision kind; choose one of ' + ', '.join(states.DECISION_KINDS))
    subject = text(payload.get('subject'), 120)
    record_id = None
    if subject.startswith('workspace:'):
        if not re.fullmatch(r'workspace:[a-z0-9]+(?:-[a-z0-9]+)*', subject):
            raise ValueError('A workspace decision subject is workspace:<lower-case-slug>')
    else:
        row = conn.execute('SELECT id FROM records WHERE id=? AND archived_at IS NULL', (subject,)).fetchone()
        if row is None:
            raise ValueError('A decision needs an active record or a workspace:<slug> subject')
        record_id = subject
    question = text(payload.get('question'), 240)
    if not question:
        raise ValueError('A decision needs a question')
    recommendation = payload.get('recommendation') or ''
    if recommendation and recommendation not in [c[0] for c in states.DECISION_KINDS[kind]['choices']]:
        raise ValueError(f'Recommend one of the {kind} choices, or none')
    evidence = '; '.join(safe_url(u) for u in re.split(r'\s*;\s*', text(payload.get('evidence_urls'), 2000)) if u)
    with transaction(conn):
        draft_version_id = None
        if kind == 'draft':
            current = conn.execute('SELECT version_id FROM drafts WHERE record_id=?', (record_id,)).fetchone()
            if not current or not current['version_id']:
                raise ValueError('Save a draft before asking the owner to approve it')
            draft_version_id = current['version_id']
        live = conn.execute("SELECT * FROM decisions WHERE subject=? AND kind=? AND status IN ('open','answered')", (subject, kind)).fetchone()
        if live:
            return {'saved': False, 'decision': decision_view(conn, live)}
        at = db.now()
        cur = conn.execute('INSERT INTO decisions(subject,record_id,kind,question,context,recommendation,reason,evidence_urls,created_at,updated_at,draft_version_id,draft_binding) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (subject, record_id, kind, question, text(payload.get('context'), 4000),
                           recommendation, text(payload.get('reason'), 1000), evidence, at, at, draft_version_id, 'exact' if draft_version_id else ''))
        if record_id:
            event(conn, record_id, 'decision', None, 'open', question, actor=actor)
            _record_waits(conn, record_id, cur.lastrowid, question, actor)
    return {'saved': True, 'decision': get_decision(conn, cur.lastrowid)}


def answer_decision(conn, did, payload, actor='owner'):
    """The owner's answer: one declared choice, a note where the choice asks for one. It may be changed
    until the agent has processed it."""
    if actor != 'owner':
        raise ValueError('Only the owner answers a decision')
    with transaction(conn):
        row = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
        if row is None:
            raise ValueError('Unknown decision')
        reconfirm=_legacy_reconfirmable(conn,row)
        if type(payload.get('revision')) is not int or payload['revision'] != row['revision']:
            raise Conflict('This decision changed. Reload it before answering again.')
        if not reconfirm:
            try:
                machines.decision_move(row['status'], 'answer')
            except machines.MachineError:
                raise Conflict('This decision changed. Reload it before answering again.') from None
        if row['execution_status'] in ('working', 'blocked'):
            raise Conflict('The agent has claimed this answer. Ask the agent to reopen it before changing it.')
        _check_decision_draft(conn, row)
        choice = next((c for c in decision_choices(row['kind']) if c['id'] == payload.get('choice')), None)
        if choice is None:
            raise ValueError('Choose one of the offered answers')
        note = text(payload.get('note'), 2000)
        if choice['note'] and not note:
            raise ValueError(f"Add a note: {choice['note'].lower()}")
        at = db.now()
        thread = json.loads(row['thread_json'] or '[]')
        if reconfirm:
            thread.append(dict(at=at,by='system',action='legacy_reconfirmation',text='Previous completion: '+(row['outcome'] or 'Historical approval'),
                               previous_processed_at=row['processed_at'],previous_answer=row['answer'],draft_version_id=row['draft_version_id']))
        thread.append(dict(at=at, by='owner', choice=choice['id'], label=choice['label'], text=note,
                           answer_revision=row['revision'] + 1, draft_version_id=row['draft_version_id']))
        conn.execute("UPDATE decisions SET status='answered',answer=?,note=?,answered_at=?,thread_json=?,updated_at=?,revision=revision+1,"
                     "answer_revision=revision+1,execution_status='queued',claimed_answer_revision=NULL,claimed_by='',claimed_at='',"
                     "outcome='',processed_at='',"
                     "draft_binding=CASE WHEN kind='draft' THEN 'exact' ELSE draft_binding END WHERE id=?",
                     (choice['id'], note, at, json.dumps(thread), at, did))
        if row['record_id']:
            event(conn, row['record_id'], 'decision', row['status'], 'answered', f"{row['question']} {choice['label']}", actor='owner')
    return {'saved': True, 'decision': get_decision(conn, did)}


def _check_decision_draft(conn, row, require_exact=False):
    if row['kind'] != 'draft':
        return
    current = conn.execute('SELECT version_id FROM drafts WHERE record_id=?', (row['record_id'],)).fetchone()
    if not current or not row['draft_version_id'] or current['version_id'] != row['draft_version_id']:
        raise Conflict('This draft changed or was removed. Ask for approval of its current version.')
    if require_exact and row['draft_binding'] != 'exact':
        raise Conflict('The earlier approval has no verified draft version. Ask the owner to approve the saved snapshot again.')


def process_decision(conn, did, action, message, actor='agent', *, revision=None):
    """The agent's side: `done` closes an answered decision with its outcome, `reply` answers the owner's
    question or asks for more and reopens it, `withdraw` retires a decision whose facts changed."""
    message = text(message, 2000)
    if action not in ('done', 'reply', 'withdraw', 'claim', 'block') or not message:
        raise ValueError('Process a decision with done, reply, withdraw, claim or block, and say what was done')
    with transaction(conn):
        row = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
        if row is None:
            raise ValueError('Unknown decision')
        try:
            machines.decision_move(row['status'], action)
        except machines.MachineError as error:
            raise ValueError(f'Decision {did}: {error}') from error
        if type(revision) is not int or revision != row['revision']:
            raise Conflict('This decision changed or its revision is missing. Read it again and pass its revision.')
        if action in ('claim', 'block', 'done'):
            _check_decision_draft(conn, row, require_exact=True)
        if row['claimed_answer_revision'] is not None and row['claimed_answer_revision'] != row['answer_revision']:
            raise Conflict('The claimed answer no longer matches the owner’s answer. Read the decision again.')
        if row['claimed_by'] and row['claimed_by'] != actor:
            raise Conflict('This answer is claimed by another actor.')
        if action == 'claim' and row['execution_status'] not in ('queued', 'blocked'):
            raise Conflict('This answer is already being worked on.')
        if action == 'block' and row['execution_status'] != 'working':
            raise ValueError('Claim the answer before recording a blocker')
        at = db.now()
        thread = json.loads(row['thread_json'] or '[]') + [dict(at=at, by=actor, action=action, text=message,
                        answer_revision=row['answer_revision'], draft_version_id=row['draft_version_id'])]
        if action == 'reply':
            conn.execute("UPDATE decisions SET status='open',answer='',note='',thread_json=?,updated_at=?,revision=revision+1,"
                         "execution_status='',answer_revision=NULL,claimed_answer_revision=NULL,claimed_by='',claimed_at='' WHERE id=?",
                         (json.dumps(thread), at, did))
        elif action in ('claim', 'block'):
            conn.execute('UPDATE decisions SET execution_status=?,claimed_answer_revision=answer_revision,claimed_by=?,claimed_at=?,thread_json=?,updated_at=?,revision=revision+1 WHERE id=?',
                         ('working' if action == 'claim' else 'blocked', actor, row['claimed_at'] or at, json.dumps(thread), at, did))
        else:
            status = 'done' if action == 'done' else 'withdrawn'
            conn.execute('UPDATE decisions SET status=?,outcome=?,processed_at=?,thread_json=?,updated_at=?,revision=revision+1,execution_status=? WHERE id=?',
                         (status, message, at, json.dumps(thread), at, 'completed' if action == 'done' else '', did))
        if row['record_id']:
            event(conn, row['record_id'], 'decision', row['status'], 'open' if action == 'reply' else action, message, actor=actor)
            if action in ('done', 'withdraw'):
                label = next((c['label'] for c in decision_choices(row['kind']) if c['id'] == row['answer']), 'Withdrawn')
                _record_decided(conn, row['record_id'], did, f"Decision #{did}: {label if action == 'done' else 'Withdrawn'}. {message}", actor)
    return {'saved': True, 'decision': get_decision(conn, did)}


PROGRESS_STAGES = ('investigating', 'fix_attempted', 'awaiting_verification', 'blocked')


def note_decision(conn, did, message, *, stage='investigating', owner='agent', next_step='', actor='agent'):
    """Append source-issue work without answering, closing or reopening the owner's decision."""
    message, next_step = text(message, 2000), text(next_step, 1000)
    if not message or stage not in PROGRESS_STAGES or owner not in ('agent', 'owner', 'provider'):
        raise ValueError('Give a progress note, a valid stage and owner')
    with transaction(conn):
        row = conn.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
        if row is None or row['kind'] != 'failure':
            raise ValueError('Progress notes belong to an existing failure decision')
        at = db.now()
        thread = json.loads(row['thread_json'] or '[]')
        thread.append(dict(at=at, by=actor, action='progress', text=message, stage=stage,
                           owner=owner, next_step=next_step))
        conn.execute('UPDATE decisions SET thread_json=?,updated_at=?,revision=revision+1 WHERE id=?',
                     (json.dumps(thread), at, did))
    return {'saved': True, 'decision': get_decision(conn, did)}


# "Ask Claude…" on a card (migration 015). The owner asks on the site; an agent answers at its next
# session through wo. Neither side changes the record, so a form left open on it stays current.
QUESTION_DAYS = 14


def ask_question(conn, rid, payload, actor='owner'):
    if actor != 'owner':
        raise ValueError('The owner asks on the site; an agent answers with wo question answer')
    question = text(payload.get('question'), 1000)
    if not question:
        raise ValueError('Write your question first')
    with transaction(conn):
        if conn.execute('SELECT 1 FROM records WHERE id=? AND archived_at IS NULL', (rid,)).fetchone() is None:
            raise ValueError('Questions go on an active record')
        cur = conn.execute('INSERT INTO questions(record_id,question,asked_at,asked_by) VALUES(?,?,?,?)',
                           (rid, question, db.now(), actor))
    return {'saved': True, 'question': get_question(conn, cur.lastrowid)}


def answer_question(conn, qid, answer, actor='agent'):
    if actor != 'agent':
        raise ValueError('An agent answers; the owner asks on the site')
    answer = text(answer, 4000)
    if not answer:
        raise ValueError('Write the answer')
    with transaction(conn):
        row = conn.execute('SELECT answered_at FROM questions WHERE id=?', (qid,)).fetchone()
        if row is None:
            raise ValueError('Unknown question')
        if row['answered_at']:
            raise Conflict(f'Question {qid} is already answered')
        conn.execute('UPDATE questions SET answer=?,answered_at=?,answered_by=? WHERE id=?', (answer, db.now(), actor, qid))
    return {'saved': True, 'question': get_question(conn, qid)}


_QUESTION_SQL = ('SELECT q.*,r.name AS record_name,r.type AS record_type FROM questions q '
                 'JOIN records r ON r.id=q.record_id')


def get_question(conn, qid):
    row = conn.execute(_QUESTION_SQL + ' WHERE q.id=?', (qid,)).fetchone()
    if row is None:
        raise ValueError('Unknown question')
    return dict(row)


def questions(conn, unanswered=False, days=QUESTION_DAYS):
    """Unanswered questions, oldest first; otherwise those plus any asked in the last `days`."""
    since = (dt.datetime.now().astimezone() - dt.timedelta(days=days)).isoformat(timespec='seconds')
    where = "q.answered_at=''" if unanswered else "(q.answered_at='' OR q.asked_at>=?)"
    return [dict(r) for r in conn.execute(_QUESTION_SQL + f' WHERE {where} ORDER BY q.asked_at,q.id',
                                          () if unanswered else (since,))]


DRAFT_PURPOSES = ('approval', 'message')


def draft(conn, payload, actor='owner'):
    """One current draft per record, backed by immutable versions. Nothing sends it.

    `purpose` is `message` (the note on a card) or `approval` (a note that needs a decision).
    A database that has not yet added the column keeps the original approval-note write.
    """
    rid=payload.get('record_id');title=text(payload.get('title',''),200);body=text(payload.get('text',''),8000)
    purpose=payload.get('purpose') or 'approval'
    if purpose not in DRAFT_PURPOSES: raise ValueError('A note is a message or an approval draft')
    if not body: raise ValueError('A draft needs text')
    with transaction(conn):
        checked(conn,rid,payload.get('revision'))
        before=conn.execute('SELECT * FROM drafts WHERE record_id=?',(rid,)).fetchone()
        has_purpose='purpose' in _columns(conn,'drafts')
        same=before and before['title']==title and before['text']==body and (not has_purpose or before['purpose']==purpose)
        if same:
            return {'saved':False,'record':get(conn,rid),'version_id':before['version_id']}
        if before:
            _invalidate_draft_decisions(conn,rid,'The draft was replaced; approval is needed for the new text.',actor)
        if has_purpose:
            version=conn.execute('INSERT INTO draft_versions(record_id,title,text,created_at,actor,purpose) VALUES(?,?,?,?,?,?)',
                                 (rid,title,body,db.now(),actor,purpose)).lastrowid
            conn.execute('INSERT INTO drafts(record_id,title,text,version_id,purpose) VALUES(?,?,?,?,?) '
                         'ON CONFLICT(record_id) DO UPDATE SET title=excluded.title,text=excluded.text,'
                         'version_id=excluded.version_id,purpose=excluded.purpose',(rid,title,body,version,purpose))
        else:
            version=conn.execute('INSERT INTO draft_versions(record_id,title,text,created_at,actor) VALUES(?,?,?,?,?)',
                                 (rid,title,body,db.now(),actor)).lastrowid
            conn.execute('INSERT INTO drafts(record_id,title,text,version_id) VALUES(?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET title=excluded.title,text=excluded.text,version_id=excluded.version_id',(rid,title,body,version))
        event(conn,rid,'draft','saved' if before else None,'saved',f"Draft {'replaced' if before else 'saved'}: {title or 'untitled'}",actor=actor);bump(conn,rid)
    return {'saved':True,'record':get(conn,rid),'version_id':version}


def _invalidate_draft_decisions(conn,rid,message,actor):
    rows=conn.execute("SELECT * FROM decisions WHERE record_id=? AND kind='draft' AND (status IN ('open','answered') OR (status='done' AND answer='approve'))",(rid,)).fetchall()
    for row in rows:
        at=db.now()
        machines.decision_move(row['status'], 'supersede')
        thread=json.loads(row['thread_json'] or '[]')+[dict(at=at,by=actor,action='superseded',text=message,draft_version_id=row['draft_version_id'])]
        conn.execute("UPDATE decisions SET status='withdrawn',execution_status='',outcome=?,processed_at=?,updated_at=?,thread_json=?,revision=revision+1 WHERE id=?",
                     (message,at,at,json.dumps(thread),row['id']))
        event(conn,rid,'decision',row['status'],'withdrawn',message,actor)
        _record_decided(conn,rid,row['id'],message,actor)


def clear_draft(conn, payload, actor='owner'):
    rid=payload.get('record_id');reason=text(payload.get('reason',''),240)
    with transaction(conn):
        checked(conn,rid,payload.get('revision'))
        if not conn.execute('DELETE FROM drafts WHERE record_id=?',(rid,)).rowcount: raise ValueError('This record has no draft')
        _invalidate_draft_decisions(conn,rid,'The draft was removed; its approval no longer applies.',actor)
        event(conn,rid,'draft','saved',None,'Draft removed'+(f': {reason}' if reason else ''),actor=actor);bump(conn,rid)
    return {'saved':True,'record':get(conn,rid)}
