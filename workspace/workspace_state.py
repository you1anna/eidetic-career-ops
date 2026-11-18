"""The workspace snapshot served at /api/state and printed by `wo state`.

`state` builds it inside one read snapshot; `cached_state` and `cached_summary_json` reuse it until
committed data, the London date or configuration changes, building one copy at a time.
`summary_json` also answers a page that already holds an earlier summary version with only what
changed since then.
Internal consumers use `state_context`/`cached_context` without the historical receipt archive;
`state_export` provides the unchanged full JSON contract through incremental file-backed encoding.
"""
from __future__ import annotations

from pathlib import Path
import datetime as dt
import gzip
import hashlib
import itertools
import json
import logging
import os
import secrets
import sqlite3
import threading
import time

import db
import normalise
import record_views
import relevance
import search_config
import states
import request_timings
import summary_transport
from record_views import LIVE_INTERACTIONS, outcome_view
from record_core import RELATIONS, WORKING, record_transitions
from record_decisions import decisions_history, decisions_state, questions
from record_review import person_matches, review_actions
from source_coverage import channel_health, describe_source, freshness


ROOT = Path(__file__).resolve().parent.parent


# Full workspace builds are the expensive read. Log the slow ones, never their contents.
STATE_BUILD_LOG_SECONDS = 0.5

# The page-load summary carries only what the browser reads; full state and timelines keep the rest.
# Work-item briefs: the browser shows recorded facts, unknowns and constraints.
_SUMMARY_BRIEF_KEYS = ('facts', 'unknowns', 'constraints')
# Assessment inputs that ranking and selection consume server-side.
_SUMMARY_OMITTED_ITEM_KEYS = ('confidence', 'trigger', 'work_evidence')
# Research fingerprints compared by the worker, never displayed.
_SUMMARY_OMITTED_BRIEF_KEYS = ('context_sha', 'researched_context_sha')


def _log_state_build(view, started, phases=None):
    held = time.monotonic() - started
    if held < STATE_BUILD_LOG_SECONDS:
        return
    split = ''.join(f' {name}_ms={round(ms)}' for name, ms in (phases or {}).items())
    logging.info('workspace state built view=%s dur_ms=%d cache=miss pid=%s run_id=%s%s',
                 view, round(held * 1000), os.getpid(), os.environ.get('WO_RUN_ID') or '-', split)


def state(conn, compact=False, *, view=None, receipt_history=True, phases=None):
    """`phases`, when given, receives each build stage's milliseconds (`request_timings` reads them)."""
    started = time.monotonic()
    phases = {} if phases is None else phases
    try:
        with record_views.snapshot(conn):
            return _state(conn, compact, receipt_history=receipt_history, phases=phases)
    finally:
        _log_state_build(view or ('compact' if compact else 'full'), started, phases)


_STATE_CACHE = {}
_JSON_CACHE = {}
_BUILD_LOCK = threading.RLock()
_WATCHERS = {}
_WATCH_LOCK = threading.Lock()


def _data_key(conn, compact):
    """What a derived state depends on: committed data (SQLite's data_version, read on a separate
    watching connection so that every other connection's commit counts), the London date and config."""
    # A caller-owned snapshot or uncommitted write cannot share a committed-state cache entry.
    if conn.in_transaction:
        return None
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        return None
    with _WATCH_LOCK:
        watcher = _WATCHERS.get(path)
        if watcher is None:
            watcher = _WATCHERS[path] = sqlite3.connect(path, check_same_thread=False)
        version = watcher.execute('PRAGMA data_version').fetchone()[0]
    import approaches
    configs = tuple(sorted((p.name, p.stat().st_mtime) for p in (ROOT / 'config').glob('*.yml')))
    return (path, version, approaches.london_today().isoformat(), configs, compact)


def cached_state(conn, compact=False):
    """`state`, reused until the data, the date or the configuration changes. Read-only callers only:
    the result is shared, so it must not be modified. One build at a time, and the stale copy is
    released first, so the server never holds two full states (it was killed at its memory cap)."""
    return _cached(conn, compact, lambda: state(conn, compact))


def state_context(conn):
    """Full operational inputs, with no receipt archive. Internal consumers do not use runs.

    Records, interactions, history, scoring, actions and research admission remain untrimmed.
    Public full-state APIs still include every receipt; this is an explicit internal projection.
    """
    return state(conn, receipt_history=False, view='context')


def cached_context(conn):
    """Read-only shared operational context, with the same invalidation as public state."""
    return _cached(conn, 'context', lambda: state_context(conn))


def _cached(conn, slot, build):
    key = _data_key(conn, slot)
    hit = _STATE_CACHE.get(slot)
    if key is not None and hit and hit[0] == key:
        return hit[1]
    with _BUILD_LOCK:
        key = _data_key(conn, slot)
        hit = _STATE_CACHE.get(slot)
        if key is not None and hit and hit[0] == key:
            return hit[1]
        _STATE_CACHE.pop(slot, None)
        hit = None
        value = build()
        if key is not None:
            _STATE_CACHE[slot] = (key, value)
        return value


def cached_summary_json(conn):
    """The summary state as JSON bytes and their gzip, reused like `cached_state`. Only text is
    kept, because the summary dict is needed only to encode it; the gzip is made on first use."""
    return summary_json(conn)


# A save reloads only what changed since the summary version the page holds. The current build
# keeps its JSON fragments; earlier versions keep only hashes, for the last few builds.
_SUMMARY_HISTORY = 8
_SUMMARY_INDEX = {}
_VERSION_PREFIX = secrets.token_hex(4)
_VERSIONS = itertools.count(1)
_KEYED = ('records', 'actions', 'research')


def summary_json(conn, since=None, encoding=None):
    """(JSON bytes, gzip or None). With `since`, a version this process issued and still remembers,
    only the changes since then; otherwise the full summary, which ends with its `version`."""
    if encoding not in (None, summary_transport.FORMAT):
        raise ValueError('Unknown summary encoding')
    key = _data_key(conn, True)
    hit = _JSON_CACHE.get(True)
    if key is None or not hit or hit[0] != key:
        with _BUILD_LOCK:
            key = _data_key(conn, True)
            hit = _JSON_CACHE.get(True)
            if key is None or not hit or hit[0] != key:
                _JSON_CACHE.pop(True, None)
                hit = None
                phases, started = {}, time.monotonic()
                summary = state(conn, True, view='summary', phases=phases)
                encoded = time.monotonic()
                raw, current = _encode_summary(summary)
                del summary
                hit = (key, (raw, current))
                done = time.monotonic()
                phases.update(encode=(done - encoded) * 1000)
                request_timings.record('build', (done - started) * 1000, phases)
                if key is None:
                    return _summary_full(*hit[1], encoding)
                _JSON_CACHE[True] = hit
                if current['index']['deltable']:
                    _SUMMARY_INDEX[current['version']] = current['index']
                    while len(_SUMMARY_INDEX) > _SUMMARY_HISTORY:
                        _SUMMARY_INDEX.pop(next(iter(_SUMMARY_INDEX)))
    raw, current = hit[1]
    base = _SUMMARY_INDEX.get(since) if since else None
    if base is None or not current['index']['deltable']:
        return _summary_full(raw, current, encoding)
    return _summary_delta(current, since, base), None


def _summary_full(raw, current, encoding):
    """One byte-only (body, gzip) pair per requested form per build, made on first request.

    A save is answered with a delta, so most builds never serve a full summary: compressing
    one eagerly spent time on every rebuild. Both forms share the build's version and
    invalidation and are released with it. Concurrent first requests are serialised; no extra
    database work or writes. The ordinary JSON contract and small deltas stay unchanged.
    """
    form = 'transport' if encoding else 'plain'
    pair = current.get(form)
    if pair is None:
        with _BUILD_LOCK:
            pair = current.get(form)
            if pair is None:
                # The parsed tree is private to this call, so the codec may pack it in place.
                body = summary_transport.encode(json.loads(raw), owned=True) if encoding else raw
                pair = current[form] = (body, gzip.compress(body, compresslevel=5, mtime=0))
    return pair


def _fragment(value):
    return json.dumps(value, separators=(',', ':')).encode()


def _digest(data):
    return hashlib.blake2b(data, digest_size=8).digest()


def _keyed(items):
    """Entry fragments in list order by unique string id, or None when the list cannot be patched."""
    if not isinstance(items, list) or not all(isinstance(x, dict) and isinstance(x.get('id'), str) for x in items):
        return None
    entries = {x['id']: _fragment(x) for x in items}
    return entries if len(entries) == len(items) else None


def _encode_summary(summary):
    """The summary as `json.dumps(..., separators=(',', ':'))` writes it plus a trailing version,
    assembled from the per-entry fragments a later delta compares (bytes, never an extra copy)."""
    version = f'{_VERSION_PREFIX}-{next(_VERSIONS)}'
    intel = summary.get('intelligence')
    lists = {'records': _keyed(summary.get('records'))}
    if isinstance(intel, dict):
        lists.update(actions=_keyed(intel.get('actions')), research=_keyed(intel.get('research')))
    deltable = all(lists.get(name) is not None for name in _KEYED)
    top, sub, parts = {}, {}, []
    for key, value in summary.items():
        parts += [b',' if parts else b'{', _fragment(key), b':']
        if key == 'records' and deltable:
            parts += [b'[', b','.join(lists['records'].values()), b']']
        elif key == 'intelligence' and deltable:
            for n, (name, item) in enumerate(intel.items()):
                parts += [b',' if n else b'{', _fragment(name), b':']
                if name in _KEYED:
                    parts += [b'[', b','.join(lists[name].values()), b']']
                else:
                    sub[name] = _fragment(item)
                    parts.append(sub[name])
            parts.append(b'}' if intel else b'{}')
        else:
            top[key] = _fragment(value)
            parts.append(top[key])
    parts += [b',' if parts else b'{', b'"version":', _fragment(version), b'}']
    index = {'deltable': deltable, 'top': {k: _digest(t) for k, t in top.items()},
             'intelligence': {k: _digest(t) for k, t in sub.items()},
             'lists': {name: {'order': _digest(_fragment(list(lists[name]))),
                              'items': {i: _digest(t) for i, t in lists[name].items()}}
                       for name in _KEYED} if deltable else {}}
    current = {'version': version, 'top': top, 'intelligence': sub,
               'lists': {name: lists[name] for name in _KEYED} if deltable else {}, 'index': index}
    return b''.join(parts), current


def _summary_delta(current, since, base):
    """Changed and removed sections and entries since `base`; each list's order only if it moved."""
    now = current['index']

    def section(texts, digests, old):
        changed = [_fragment(k) + b':' + t for k, t in texts.items() if old.get(k) != digests[k]]
        return (b'"set":{' + b','.join(changed) + b'},"unset":'
                + _fragment([k for k in old if k not in digests]))

    lists = []
    for name in _KEYED:
        entries, digests, old = current['lists'][name], now['lists'][name], base['lists'][name]
        upsert = [t for i, t in entries.items() if old['items'].get(i) != digests['items'][i]]
        text = (b'"upsert":[' + b','.join(upsert) + b'],"remove":'
                + _fragment([i for i in old['items'] if i not in digests['items']]))
        if old['order'] != digests['order']:
            text += b',"order":' + _fragment(list(entries))
        lists.append(_fragment(name) + b':{' + text + b'}')
    return (b'{"delta":true,"base":' + _fragment(since) + b',"version":' + _fragment(current['version'])
            + b',' + section(current['top'], now['top'], base['top'])
            + b',"intelligence":{' + section(current['intelligence'], now['intelligence'], base['intelligence'])
            + b'},"collections":{' + b','.join(lists) + b'}}')


_BOARD_SCOPES = {}


def configured_board_scopes():
    """Enabled board identities shared by current health and issue views; None if unconfigured.
    Parsed again only when portals.yml changes: every workspace build asks."""
    portals = ROOT / 'config/portals.yml'
    if not portals.exists():
        return None
    stamp = portals.stat().st_mtime_ns
    cached = _BOARD_SCOPES.get('parsed')
    if cached is None or cached[0] != stamp:
        import yaml
        configured = yaml.safe_load(portals.read_text()) or {}
        scopes = {}
        for company in configured.get('tracked_companies', []):
            if company.get('enabled', True):
                provider, slug = normalise.board_identity(company.get('careers_url', ''))
                if provider:
                    scopes[f'board:{provider}:{slug}'] = company.get('name') or slug
        cached = _BOARD_SCOPES['parsed'] = (stamp, scopes)
    return dict(cached[1])


def _state(conn, compact, *, receipt_history=True, phases=None):
    import research_scope
    phases = {} if phases is None else phases
    clock = [time.monotonic()]
    def mark(name):
        now = time.monotonic()
        phases[name] = (now - clock[0]) * 1000
        clock[0] = now
    attempts = research_scope.latest_attempts(conn)
    # One read of the exclusion policy serves records and both decision projections: the
    # caller's snapshot makes every later read of it identical.
    policy = record_views.record_policy(conn)
    rs = record_views.load_records(conn, brief_attempts=attempts, policy=policy)
    for r in rs:
        r['transitions']=record_transitions(r)
        r['outcome_actions']=states.outcome_actions(r)
    mark('records')
    queue=[]
    people = sorted((r for r in rs if r['type']=='person'), key=lambda r:r['id'])
    for row in conn.execute('SELECT * FROM review_queue WHERE resolved_at IS NULL ORDER BY id'):
        q=dict(row); q['payload']=json.loads(q.pop('payload_json'))
        # Matches are read live: an earlier accept can make a later card a duplicate.
        q['matches']=person_matches(conn,q['payload'],people); q['actions']=review_actions(q['matches']); queue.append(q)
    expected_boards = configured_board_scopes()
    source_rows={r['scope']:describe_source(dict(r)) for r in conn.execute('SELECT * FROM source_health')}
    actors={r['id']:r['actor'] for r in conn.execute('SELECT id,actor FROM draft_versions')} if 'draft_versions' in {
        r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} else {}
    drafts=[]
    for r in conn.execute('SELECT * FROM drafts ORDER BY record_id'):
        d=dict(r);d['actor']=actors.get(d.get('version_id'),'');drafts.append(d)
    out=dict(records=rs,research_attempts=attempts,cvs=search_config.cvs(),links=[dict(r) for r in conn.execute('SELECT * FROM links ORDER BY from_id,to_id,relation')],
                company_logos=record_views.company_logos(conn, rs),
                interactions=[dict(r) for r in conn.execute(LIVE_INTERACTIONS+' ORDER BY at DESC,id DESC')],
                history=record_views.history(conn,recent=compact),
                drafts=drafts,
                outcomes=[outcome_view(r) for r in conn.execute('SELECT * FROM outcomes ORDER BY at DESC,id DESC')],
                approach_next_steps=[dict(record_id=r['record_id'], action_id=r['action_id'], value=json.loads(r['step_json']), context=json.loads(r['context_json']))
                    for r in conn.execute('SELECT s.record_id,s.action_id,s.step_json,s.context_json FROM approach_next_steps s '
                                          'JOIN records r ON r.id=s.record_id WHERE r.archived_at IS NULL ORDER BY s.record_id,s.action_id')],
                review_queue=queue,decisions=decisions_state(conn,policy=policy),questions=questions(conn),**record_views.run_statuses(conn),
                loaded=db.now(),mode='database',vocabularies=states.ENUMS,working_states=WORKING,relations=RELATIONS,decision_areas=states.DECISION_AREAS,
                freshness=freshness(conn,source_rows,expected_boards))
    if not compact:
        # Source coverage for the CLI: each channel's state is a measured receipt, never inferred from
        # configuration alone. The browser no longer draws it.
        channels=[]
        path=ROOT/'config/channels.yml'
        if path.exists():
            import yaml
            channels=(yaml.safe_load(path.read_text()) or {}).get('channels',[])
        for c in channels:
            c.update(channel_health(conn,c,source_rows,dt.date.today(),
                                    expected_board_scopes=expected_boards if c.get('id') == 'employer-ats' else None))
        out['coverage']={'channels':channels,'runs':record_views.receipts(conn) if receipt_history else [],'sources':list(source_rows.values())}
    # A broken template file leaves the note box without templates; it never takes the workspace down.
    try:
        out['templates']=search_config.templates()
    except Exception as error:
        out['templates']=None
        out['templates_error']=f'{type(error).__name__}: {error}'
    # Derived on read and never stored; a broken relevance config must not take the workspace down.
    all_decisions = decisions_history(conn, policy=policy)
    mark('reads')
    try:
        scores=relevance.score(dict(out,decisions=all_decisions,sightings=record_views.last_sightings(conn)))
        for r in rs: r['relevance']=scores.get(r['id'])
    except Exception as error:
        out['relevance_error']=f'{type(error).__name__}: {error}'
    mark('relevance')
    today = dt.date.today()
    try:
        import approaches, decision_intelligence
        today=approaches.london_today()
        week=approaches.week_of(today)
        derived=dict(out,decisions=all_decisions,
                     week_items=[dict(r) for r in conn.execute('SELECT * FROM week_items WHERE week=?',(week,))],
                     status_since={r[0]:r[1] for r in conn.execute(
                         "SELECT e.entity_id,MAX(e.at) FROM state_events e JOIN records r ON r.id=e.entity_id "
                         "WHERE r.type='job' AND e.field='status' AND e.to_value=r.status GROUP BY e.entity_id")})
        out['intelligence']=approaches.annotate(decision_intelligence.build(derived, today=today), derived, today)
    except Exception as error:
        out['intelligence']={'actions':[], 'research':[], 'outcomes_summary':{}, 'sections':{}, 'followups':[], 'week':None}
        out['intelligence_error']=f'{type(error).__name__}: {error}'
    mark('intelligence')
    import work_selection
    try:
        out['discovery_capacity'] = work_selection.capacity_policy(relevance.load_config())
        out['opportunities'] = work_selection.build(out, today=today)
    except Exception as error:
        out['opportunities'] = None
        out['intelligence_error'] = f'{type(error).__name__}: {error}'
    research_scope.attach(conn,out,attempts)
    mark('selection')
    if compact:
        out.pop('research_attempts', None)
        out.pop('drafts', None)
        out.pop('templates', None)
        out.pop('templates_error', None)
        # Historical outgoing wording is fetched only through timeline/full export.
        for decision in out['decisions']:
            decision.pop('draft', None)
        for outcome in out['outcomes']:
            outcome.get('snapshot', {}).pop('message', None)
        for item in out['intelligence'].get('actions', []) + out['intelligence'].get('research', []):
            (item.get('last_outcome') or {}).get('snapshot', {}).pop('message', None)
            progress = item.get('progress') or {}
            progress.update(say='', points=[], plan=None, message=None, sent=None)
            # Selection, ordering and progress above already consumed the full assessment.
            brief = item.get('brief') or {}
            item['brief'] = {k: brief[k] for k in _SUMMARY_BRIEF_KEYS if k in brief}
            for key in _SUMMARY_OMITTED_ITEM_KEYS:
                item.pop(key, None)
        out['intelligence'].pop('eligible_advert_ids', None)
        # Current steps have been projected onto their approaches; history is fetched on demand.
        out.pop('approach_next_steps', None)
        # Scoring above uses the full interaction evidence. Timelines are fetched on demand.
        out['interactions']=[]
        # The browser uses sourced brief excerpts and qualification, not full scanner bodies.
        # Keep full evidence available through the CLI/API while avoiding repeated mobile transfer.
        # Outcome actions repeat one of four lists on every record and the browser reads only the
        # progress view's actions, which approaches.annotate derived from them above.
        for r in out['records']:
            r.pop('prepared_approach', None)
            for review in r.get('research') or []:
                review.pop('approach', None)
            r.pop('advert',None)
            r.pop('outcome_actions',None)
            # The compared clauses in fit retain source/proof references. Raw extraction is
            # still available in full state, but would duplicate it in the mobile summary.
            relevance_view = r.get('relevance') or {}
            (relevance_view.get('qualification') or {}).pop('requirements', None)
            for reason in relevance_view.get('reasons') or []:
                reason.pop('evidence', None)  # fit.evidence is the disclosure's single source.
            (relevance_view.get('fit') or {}).pop('optional_unknowns', None)  # compared clauses retain these.
            if r.get('evidence_brief'):
                r['evidence_brief'] = {k: v for k, v in r['evidence_brief'].items() if k not in _SUMMARY_OMITTED_BRIEF_KEYS}
    mark('trim')
    return out
