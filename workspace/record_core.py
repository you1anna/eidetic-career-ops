"""The write boundary every record change goes through.

Transactions with lock-hold logging, revision checks, input validation, the state-event history,
edit ownership and the one place source observations are stored. Domain modules build on these
primitives; nothing here knows about decisions, outreach or the workspace view.
"""
from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlsplit
import json
import logging
import os
import sys
import time
import uuid

import db
import normalise
import record_views
import search_config
import states


WORKING = ('needs_review', 'ready', 'in_progress', 'waiting', 'blocked', 'paused', 'done')
TYPES = ('person', 'company', 'agency', 'opportunity', 'job', 'tender', 'event', 'source')
EDITABLE = {'name','organisation','role','summary','notes','known_context','url','email','phone','location',
            'workplace','engagement','pay','ir35','date','verification','verification_level','evidence_urls',
            'contact','record_url','detail_url','apply_url','subtype','published_deadline','cv_override',
            'preferred_channel','preferred_ask','priority'}
STATE_FIELDS = {'stage','status','relationship','kind'}
# The owner's list (migration 012): plain person fields whose values states.ENUMS['person'] declares.
PERSON_CHOICES = ('preferred_channel','preferred_ask','priority')
RELATIONS = ('related','works_at','formerly_at','former_colleague_at','consultant_at','introducer',
             'hiring_for','buyer','advertised_by','possible_match')


# A page save should be a short write. Longer holds are logged so a stuck click can be traced.
WRITE_LOCK_LOG_SECONDS = 1.0


def _caller_name():
    frame = sys._getframe(0)
    while frame is not None:
        name = frame.f_code.co_name
        module = frame.f_globals.get('__name__', '')
        if name not in ('_caller_name', 'transaction') and module != 'contextlib':
            return name
        frame = frame.f_back
    return 'transaction'


class Conflict(ValueError):
    """The client must reload before retrying a stale write."""


@contextmanager
def transaction(conn):
    # SAVEPOINT supports import/review operations that compose the same service functions.
    outer = conn.in_transaction
    name = 'sp_' + uuid.uuid4().hex
    caller = _caller_name() if not outer else ''
    started = time.monotonic()
    conn.execute('SAVEPOINT '+name if outer else 'BEGIN IMMEDIATE')
    try:
        yield
        conn.execute('RELEASE '+name if outer else 'COMMIT')
    except Exception:
        if outer:
            conn.execute('ROLLBACK TO '+name)
            conn.execute('RELEASE '+name)
        else:
            conn.execute('ROLLBACK')
        raise
    finally:
        if not outer:
            held = time.monotonic() - started
            if held >= WRITE_LOCK_LOG_SECONDS:
                logging.warning('write lock held caller=%s hold_ms=%d pid=%s run_id=%s',
                                caller, round(held * 1000), os.getpid(), os.environ.get('WO_RUN_ID') or '-')


def text(value, limit=4000):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'Use text no longer than {limit} characters')
    return value.strip()


def safe_url(value):
    value = text(value)
    if value:
        u = urlsplit(value)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password:
            raise ValueError('Use a complete http or https link')
    return value


def checked(conn, rid, revision):
    row = conn.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
    if row is None:
        raise ValueError('Unknown record')
    if type(revision) is not int or revision != row['revision']:
        raise Conflict('This record changed. Reload it before saving again.')
    return row


def event(conn, rid, field, before, after, note='', actor='owner', *, source_scope='', changes=None, observed_at=None):
    now = db.now()
    conn.execute('INSERT INTO state_events(entity_id,field,from_value,to_value,at,actor,note,source_scope,changes_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                 (rid,field,before,after,observed_at or now,actor,note,source_scope,json.dumps(changes or {}),now))


def mark_edited(conn, rid, fields, actor='owner'):
    """Protect edited fields from source refreshes. A human edit (the owner's, or his accept/merge decision
    on a review card, actor 'review') takes ownership of a field an agent corrected; an agent
    correction never takes a field back from a human."""
    conn.executemany("INSERT INTO record_edits(record_id,field,actor) VALUES(?,?,?) ON CONFLICT(record_id,field) "
                     "DO UPDATE SET actor=CASE WHEN excluded.actor<>'agent' THEN excluded.actor ELSE record_edits.actor END",
                     [(rid,f,actor) for f in fields])


def bump(conn, rid):
    conn.execute('UPDATE records SET revision=revision+1,updated_at=? WHERE id=?',(db.now(),rid))


def get(conn, rid):
    return record_views.load_records(conn, rid)[0]


def record_transitions(r):
    """The moves `transition` accepts for this record, by field. A field with none is left out."""
    entity=states.TYPE_ENTITY.get(r['type'],'record')
    moves={f:list(states.allowed_from(entity,f,r.get(f))) for f in states.ENUMS.get(entity,{}) if f in STATE_FIELDS}
    if r.get('exclusion') and 'status' in moves:
        moves['status'] = [v for v in moves['status'] if v != 'pursue']
    return {f:v for f,v in moves.items() if v}


def _values(payload):
    unknown=set(payload)-EDITABLE-STATE_FIELDS-{'revision','type'}
    if unknown:
        raise ValueError('Unsupported fields: '+', '.join(sorted(unknown)))
    result={k:text(v,20000 if k=='notes' else 4000) for k,v in payload.items()
            if k in EDITABLE and k != 'cv_override' and k not in PERSON_CHOICES}
    if 'cv_override' in payload:
        result['cv_override'] = search_config.cv_key(payload['cv_override'])
    for k in set(PERSON_CHOICES) & payload.keys():
        result[k] = states.validate('person', k, payload[k])
    if 'name' in result and not result['name']:
        raise ValueError('A name is required')
    for k in ('url','record_url','detail_url','apply_url'):
        if k in result: result[k]=safe_url(result[k])
    for k in set(normalise.DATE_FIELDS) & result.keys():
        if not normalise.is_factual_date(result[k]):
            raise ValueError(f'{k.replace("_"," ").capitalize()} must be a date (YYYY-MM-DD, YYYY-MM or YYYY); put other detail in Notes')
    return result


def _columns(conn, table):
    return {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}


def add_observation(conn, record_id, source, observed_at, payload_json):
    """Keep one source fact. A scanner or award with unchanged content updates that row's timing
    (never backwards); any other observation is added once and a repeat is ignored. Returns the cursor,
    so a caller can tell a new fact (rowcount 1) from a repeat."""
    sha=db.observation_sha(source,payload_json)
    if source.startswith('award:'):
        # The existing content index covers scanners only. Serialize award lookup/write instead
        # of changing that index's predicate or losing source identity from the content hash.
        with transaction(conn):
            prior = conn.execute('SELECT id FROM observations WHERE record_id=? AND source=? AND payload_sha=? '
                                 'ORDER BY wo_observation_time(observed_at) DESC,id DESC LIMIT 1',
                                 (record_id, source, sha)).fetchone()
            if prior:
                return conn.execute('UPDATE observations SET observed_at=?,payload_json=? WHERE id=? '
                                    'AND wo_observation_time(?)>wo_observation_time(observed_at)',
                                    (observed_at, payload_json, prior['id'], observed_at))
            return conn.execute('INSERT INTO observations(record_id,source,observed_at,payload_json,payload_sha) VALUES(?,?,?,?,?)',
                                (record_id, source, observed_at, payload_json, sha))
    if source.startswith(db.SCANNER_SOURCES):
        return conn.execute('INSERT INTO observations(record_id,source,observed_at,payload_json,payload_sha) VALUES(?,?,?,?,?) '
                            'ON CONFLICT(record_id,source,payload_sha) WHERE '+db.SCANNER_SQL+' DO UPDATE SET '
                            'observed_at=excluded.observed_at,payload_json=excluded.payload_json '
                            'WHERE wo_observation_time(excluded.observed_at)>wo_observation_time(observations.observed_at)',
                            (record_id,source,observed_at,payload_json,sha))
    return conn.execute('INSERT OR IGNORE INTO observations(record_id,source,observed_at,payload_json,payload_sha) VALUES(?,?,?,?,?)',
                        (record_id,source,observed_at,payload_json,sha))
