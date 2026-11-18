#!/usr/bin/env python3
"""Activity report: what the owner's recorded use shows, from history already in the database.

Three measured sections, each tied to a decision in archive/plans/2026-10-26-usage-activity.md:

- outreach results by segment, for the 6 November review;
- preparation and continuity for each send: how far the sent words moved from the agent's draft,
  whether the sent text had to be entered again, and whether a next step was recorded afterwards;
- research yield: reviews that changed nothing, Codex use, and which sources' records the owner acted on.

Read-only: it never writes. It runs on a backup whose schema is behind the code, so tables added
later are optional. Output holds record ids, counts and ratios only: no names and no message text.

Usage: wo activity [--since YYYY-MM-DD | --weeks N] [--json]
"""
from __future__ import annotations

import collections
import datetime as dt
import difflib
import json
import re
import sqlite3
import statistics
from zoneinfo import ZoneInfo

LONDON = ZoneInfo('Europe/London')
# The 15 October list (archive/plans/2026-10-14-outreach-focus.md, "First batch"): people going out in waves,
# by segment. D, "asked about a named live role", is not stored on records, so it is not derived.
LIST_DAY = dt.date(2026, 10, 15)
LIST_SIZES = {'A': 17, 'B': 13, 'C': 2}
SEGMENTS = {'A': 'know well or in touch', 'B': 'know from before', 'C': 'agency recruiters',
            '-': 'not on the list'}
OUTCOMES = ('sent', 'followed_up', 'replied', 'conversation', 'introduction', 'referral', 'submitted')
COPY_BACK = dt.timedelta(minutes=10)  # the owner's own draft version saved this soon before logging a send
PASTED, RESAVED = 'pasted into the contact form', 'saved as a new draft just before logging'
LABELS = {'people_contacted': 'people', 'followed_up': 'followed up', 'conversation': 'conversations',
          'introduction': 'intros', 'referral': 'referrals', 'submitted': 'submitted'}
UNCHANGED, EDITED = 0.9, 0.6          # word-sequence similarity to the agent's draft
NOT_PREPARED = ('owner', 'migration')  # draft-version actors that are not an agent's preparation


def when(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=LONDON)


def london_start(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(), LONDON)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace('’', "'"))


def similarity(prepared: str, sent: str) -> float:
    # autojunk=False: the default ignores common words once a text passes 200 words, which
    # under-scores long messages.
    return difflib.SequenceMatcher(None, words(prepared), words(sent), autojunk=False).ratio()


def bucket(ratio: float) -> str:
    return 'unchanged' if ratio >= UNCHANGED else 'edited' if ratio >= EDITED else 'rewritten'


def segment(kind: str | None, relationship: str | None, priority: str | None) -> str:
    if not priority:
        return '-'
    if kind == 'recruiter':
        return 'C'
    return 'A' if relationship in ('warm', 'active') else 'B'


def has_table(conn: sqlite3.Connection, name: str, *columns: str) -> bool:
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}
    return bool(have) and set(columns) <= have


def _as_of(conn, rid, field, cutoff, current):
    """A field's value at cutoff: its last change before then, else the current value if it never
    changed after (else unknown)."""
    rows = [(when(r[0]), r[1]) for r in conn.execute(
        "SELECT COALESCE(recorded_at, at), to_value FROM state_events WHERE entity_id=? AND field=? ORDER BY id",
        (rid, field))]
    before = [value for at, value in rows if at < cutoff]
    if before:
        return before[-1]
    return None if rows else current


def list_check(conn, people, sends):
    """Rebuild the 15 October segment sizes from history, to show whether the mapping holds."""
    cutoff = london_start(LIST_DAY + dt.timedelta(days=1))
    under_way = {o['record_id'] for o in sends if o['kind'] == 'sent' and o['when'] < london_start(LIST_DAY)}
    sizes = collections.Counter()
    for p in people.values():
        if when(p['created_at']) >= cutoff or p['id'] in under_way:
            continue
        priority = _as_of(conn, p['id'], 'priority', cutoff, p['priority'])
        if priority not in ('first', 'next'):
            continue
        sizes[segment(_as_of(conn, p['id'], 'kind', cutoff, p['kind']),
                      _as_of(conn, p['id'], 'relationship', cutoff, p['relationship']), priority)] += 1
    derived = {s: sizes.get(s, 0) for s in LIST_SIZES}
    return {'derived': derived, 'recorded': LIST_SIZES, 'matches': derived == LIST_SIZES,
            'excluded_already_under_way': len(under_way)}


def outreach(conn, people, outcomes, start, end):
    table = {s: collections.Counter() for s in SEGMENTS}
    contacted = {s: set() for s in SEGMENTS}
    for o in outcomes:
        if not start <= o['when'] < end or o['record_id'] not in people:
            continue
        p = people[o['record_id']]
        s = segment(p['kind'], p['relationship'], p['priority'])
        table[s][o['kind']] += 1
        if o['kind'] in ('sent', 'followed_up'):
            contacted[s].add(o['record_id'])
    rows = {s: {'people_contacted': len(contacted[s]), **{k: table[s].get(k, 0) for k in OUTCOMES},
                'other': sum(v for k, v in table[s].items() if k not in OUTCOMES)} for s in SEGMENTS}
    return {'by_segment': rows, 'list_check': list_check(conn, people, outcomes),
            'not_derived': 'D (asked about a named live role) is not stored on records'}


def _next_step_after(conn, rid, sent_at, steps_table):
    times = [when(r[0]) for r in conn.execute(
        "SELECT COALESCE(recorded_at, at) FROM state_events WHERE entity_id=? AND field='next_action'", (rid,))]
    if steps_table:
        times += [when(r[0]) for r in conn.execute(
            "SELECT updated_at FROM approach_next_steps WHERE record_id=?", (rid,))]
    return any(t and t > sent_at for t in times)


def preparation(conn, people, outcomes, start, end):
    steps_table = has_table(conn, 'approach_next_steps', 'record_id', 'updated_at')
    versions = collections.defaultdict(list)
    for v in conn.execute("SELECT id, record_id, text, actor, purpose, created_at FROM draft_versions ORDER BY id"):
        versions[v['record_id']].append({**dict(v), 'when': when(v['created_at'])})
    sends = []
    for o in outcomes:
        if o['kind'] != 'sent' or not start <= o['when'] < end or o['record_id'] not in people:
            continue
        rid, at = o['record_id'], o['when']
        mine = versions.get(rid, [])
        snapshot = json.loads(o['snapshot_json'] or '{}')
        marked = next((v for v in mine if v['id'] == o['draft_version_id']), None)
        if snapshot.get('message_source') == 'reported' and snapshot.get('message'):
            text, entry = snapshot['message'], PASTED
        elif marked:
            text = marked['text']
            resaved = marked['actor'] == 'owner' and dt.timedelta(0) <= at - marked['when'] <= COPY_BACK
            entry = RESAVED if resaved else 'saved draft'
        else:
            text, entry = None, 'no wording recorded'
        prepared = [v for v in mine if v['actor'] not in NOT_PREPARED and v['purpose'] == 'message' and v['when'] < at]
        if text is None:
            ratio, result = None, 'unknown'
        elif prepared:
            ratio = round(similarity(prepared[-1]['text'], text), 2)
            result = bucket(ratio)
        else:
            ratio, result = None, 'no agent draft'
        p = people[rid]
        sends.append({'record_id': rid, 'at': o['at'], 'segment': segment(p['kind'], p['relationship'], p['priority']),
                      'entry': entry, 'reentered': entry in (PASTED, RESAVED),
                      'similarity': ratio, 'prepared': result,
                      'next_step_recorded': _next_step_after(conn, rid, at, steps_table)})
    return {'sends': sends,
            'prepared': dict(collections.Counter(s['prepared'] for s in sends)),
            'reentered': sum(s['reentered'] for s in sends),
            'next_step_recorded': sum(s['next_step_recorded'] for s in sends),
            'next_step_source': 'next_action edits and approach next steps' if steps_table else
                                'next_action edits only (approach next steps not in this database)'}


def research(conn, people_and_jobs_acted, start, end):
    reviews = [r for r in conn.execute(
        "SELECT entity_id, COALESCE(recorded_at, at) AS at, changes_json FROM state_events "
        "WHERE actor='research' AND field='evidence_review'") if start <= when(r['at']) < end]
    per_record = collections.Counter(r['entity_id'] for r in reviews)
    unchanged = sum((r['changes_json'] or '{}') == '{}' for r in reviews)
    codex = collections.defaultdict(collections.Counter)
    if has_table(conn, 'codex_invocations', 'lane', 'day', 'status'):
        for r in conn.execute("SELECT lane, day, status FROM codex_invocations"):
            if start.date() <= dt.date.fromisoformat(r['day']) < end.date():
                codex[r['lane']][r['status']] += 1
    sources = collections.defaultdict(lambda: {'created': 0, 'acted_on': 0})
    for r in conn.execute("SELECT entity_id, COALESCE(recorded_at, at) AS at, source_scope FROM state_events "
                          "WHERE field='created'"):
        if not start <= when(r['at']) < end:
            continue
        key = source_key(r['source_scope'])
        sources[key]['created'] += 1
        sources[key]['acted_on'] += r['entity_id'] in people_and_jobs_acted
    return {'reviews': len(reviews), 'records_reviewed': len(per_record), 'reviews_without_change': unchanged,
            'most_reviews_on_one_record': max(per_record.values(), default=0),
            'median_reviews_per_record': round(statistics.median(per_record.values()), 1) if per_record else 0,
            'codex_invocations': {lane: dict(c) for lane, c in sorted(codex.items())},
            'sources': dict(sorted(sources.items(), key=lambda kv: -kv[1]['created']))}


def source_key(scope: str | None) -> str:
    if not scope:
        return 'entered by hand'
    if scope.startswith('research:'):
        return 'research'
    return ':'.join(scope.split(':')[:2])


def acted_on(conn) -> set[str]:
    """Records the owner did something with: his edits and transitions, outcomes or weekly choices."""
    ids = {r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM state_events WHERE actor='owner'")}
    ids |= {r[0] for r in conn.execute("SELECT DISTINCT record_id FROM outcomes WHERE actor='owner'")}
    if has_table(conn, 'week_items', 'record_id', 'actor'):
        ids |= {r[0] for r in conn.execute("SELECT DISTINCT record_id FROM week_items WHERE actor='owner'")}
    return ids


def report(conn: sqlite3.Connection, since: dt.date, until: dt.date | None = None) -> dict:
    conn.row_factory = sqlite3.Row
    start = london_start(since)
    end = london_start((until or dt.datetime.now(LONDON).date()) + dt.timedelta(days=1))
    people = {r['id']: dict(r) for r in conn.execute(
        "SELECT id, kind, relationship, priority, created_at FROM records WHERE type='person'")}
    outcomes = [{**dict(r), 'when': when(r['at'])} for r in conn.execute(
        "SELECT record_id, kind, at, actor, draft_version_id, snapshot_json FROM outcomes "
        "WHERE corrected_at IS NULL ORDER BY at, id")]
    return {'window': {'from': start.date().isoformat(), 'to': (end - dt.timedelta(days=1)).date().isoformat()},
            'outreach': outreach(conn, people, outcomes, start, end),
            'preparation': preparation(conn, people, outcomes, start, end),
            'research': research(conn, acted_on(conn), start, end)}


def format_report(r: dict) -> str:
    w = r['window']
    out = [f"Activity {w['from']} to {w['to']} (measured from stored history; counts, not percentages)", '']
    o = r['outreach']
    out.append('Outreach by segment (uncorrected outcomes; source: outcomes, records)')
    cols = ('people_contacted', *OUTCOMES, 'other')
    out.append('  ' + 'segment'.ljust(28) + ' '.join(LABELS.get(c, c).rjust(13) for c in cols))
    for s, label in SEGMENTS.items():
        row = o['by_segment'][s]
        out.append('  ' + f'{s} {label}'.ljust(28) + ' '.join(str(row[c]).rjust(13) for c in cols))
    lc = o['list_check']
    sizes = ', '.join(f"{s} {lc['derived'][s]}/{lc['recorded'][s]}" for s in LIST_SIZES)
    out.append(f"  15 October list rebuilt from history (derived/recorded): {sizes} — "
               + ('mapping verified' if lc['matches'] else 'mapping unverified'))
    out += [f"  {o['not_derived']}.", '']
    p = r['preparation']
    n = len(p['sends'])
    out.append(f'Preparation and continuity: {n} send(s) (source: outcomes, draft_versions, state_events)')
    out.append(f"  agent draft against sent words: {', '.join(f'{k} {v}' for k, v in sorted(p['prepared'].items())) or 'none'}"
               f" (unchanged >= {UNCHANGED}, edited >= {EDITED})")
    out.append(f"  sent text entered again on the site: {p['reentered']} of {n}")
    out.append(f"  next step recorded after the send: {p['next_step_recorded']} of {n} ({p['next_step_source']})")
    for s in p['sends']:
        ratio = '' if s['similarity'] is None else f" {s['similarity']:.2f}"
        out.append(f"  {s['at'][:16]} {s['record_id']} [{s['segment']}] {s['prepared']}{ratio}; {s['entry']}; "
                   + ('next step recorded' if s['next_step_recorded'] else 'no next step recorded'))
    out.append('')
    q = r['research']
    out.append('Research yield (source: state_events, codex_invocations)')
    out.append(f"  reviews {q['reviews']} of {q['records_reviewed']} records; {q['reviews_without_change']} recorded no change; "
               f"median {q['median_reviews_per_record']} and at most {q['most_reviews_on_one_record']} per record")
    for lane, statuses in q['codex_invocations'].items():
        out.append(f"  codex {lane}: " + ', '.join(f'{k} {v}' for k, v in sorted(statuses.items())))
    out.append('  new records by source, and how many the owner later acted on:')
    for key, v in list(q['sources'].items())[:15]:
        out.append(f"    {key}: {v['created']} new, {v['acted_on']} acted on")
    return '\n'.join(out)
