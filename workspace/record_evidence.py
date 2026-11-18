"""Source evidence on records: reviewed research findings, and merges that fold a duplicate into its
survivor while keeping every observation as evidence.
"""
from __future__ import annotations

import json
import re

import db
import normalise
import states
from record_core import (
    Conflict, PERSON_CHOICES, _values, add_observation, bump, checked, event, get, mark_edited, safe_url,
    text, transaction,
)


def evidence_links(urls, previous='', limit=4000):
    """The record's link field is a bounded view: this finding's sources in their given order, then
    the most recent earlier links that still fit. Each observation keeps its complete source list."""
    new = list(dict.fromkeys(safe_url(u) for u in urls))
    older = [u for u in dict.fromkeys(v.strip() for v in re.split(r';\s+', previous or '')) if u and u not in new]
    kept, earlier, size = [], [], -2
    for link in new:
        if size + 2 + len(link) > limit:
            break
        kept.append(link)
        size += 2 + len(link)
    for link in reversed(older):
        if size + 2 + len(link) > limit:
            break
        earlier.append(link)
        size += 2 + len(link)
    return '; '.join([*reversed(earlier), *kept])


def research_observation(conn, rid, source, payload, *, append_evidence=False):
    """Retain reviewed evidence, applying only newer stated facts through the shared boundary.

    The owner's edits always win. Research can fill a field an agent deliberately left empty, but an
    older or same-time review cannot rewind current facts. Raw evidence remains unchanged.
    """
    at = text(payload.get('reviewed_at'), 50)
    clock = normalise.observation_time(at)
    if clock == normalise.observation_time(None):
        raise ValueError('Research needs an ISO review date or timestamp')
    values = {}
    summary = payload.get('desk_summary') or payload.get('summary')
    if summary:
        values['summary'] = summary
    urls = payload.get('source_urls')
    if not isinstance(urls, list) or not urls or not all(isinstance(u, str) and u.strip() for u in urls):
        raise ValueError('Research needs a list of source URLs')
    values['evidence_urls'] = evidence_links(urls)
    for key, field in (('verification', 'verification_level'), ('public_contact_route', 'contact')):
        if payload.get(key):
            values[field] = payload[key]
    values = {key: value for key, value in _values(normalise.normalise_values(values)).items() if value}
    with transaction(conn):
        old = conn.execute('SELECT * FROM records WHERE id=? AND archived_at IS NULL', (rid,)).fetchone()
        if old is None:
            raise ValueError('Research needs an active record')
        if append_evidence and old['evidence_urls']:
            # Preserve previous links without attributing them to the new observation's source.
            values['evidence_urls'] = evidence_links(urls, old['evidence_urls'])
        previous = max((normalise.observation_time(r[0]) for r in conn.execute(
            "SELECT observed_at FROM observations WHERE record_id=? AND source LIKE 'research:%'", (rid,))),
            default=normalise.observation_time(None))
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        # A replay is a true no-op, including SQLite's sequence counter. Keep identity identical
        # to add_observation's unique boundary; this transaction serializes the check and insert.
        if conn.execute('SELECT 1 FROM observations WHERE record_id=? AND source=? AND observed_at=? AND payload_sha=?',
                        (rid, source, at, db.observation_sha(source, payload_json))).fetchone():
            return dict(saved=False, stale=False, conflicts=[])
        added = add_observation(conn, rid, source, at, payload_json).rowcount
        result = dict(saved=bool(added), stale=bool(added and clock <= previous), conflicts=[])
        if not added or result['stale']:
            return result
        protected = {x['field'] for x in conn.execute('SELECT field,actor FROM record_edits WHERE record_id=?', (rid,))
                     if x['actor'] != 'agent' or old[x['field']]}
        changed = {}
        for key, value in values.items():
            if old[key] == value:
                continue
            if key in protected:
                result['conflicts'].append(dict(record_id=rid, field=key, observation=value))
            else:
                changed[key] = value
        if changed:
            conn.execute('UPDATE records SET ' + ','.join(k + '=?' for k in changed) + ' WHERE id=?',
                         [*changed.values(), rid])
        bump(conn, rid)
        event(conn, rid, 'evidence_review', None, None,
              'Evidence reviewed; ' + str(len(payload.get('decisive_unknowns', []))) + ' open questions.', actor='research', source_scope=source, observed_at=at,
              changes={k: dict(before=old[k], after=v) for k, v in changed.items()})
    return result


def merge(conn, source_id, target_id, payload, actor='owner'):
    """Fold a duplicate into the record that stays: links, interactions and source identities move,
    then the duplicate is archived. Observations are also copied to the survivor through the normal
    deduplication boundary; the originals and history remain on the archived record as evidence."""
    reason=text(payload.get('reason',''),1000)
    with transaction(conn):
        src=checked(conn,source_id,payload.get('revision'));dst=conn.execute('SELECT * FROM records WHERE id=?',(target_id,)).fetchone()
        if dst is None or dst['archived_at'] or src['archived_at'] or source_id==target_id:
            raise ValueError('Merge needs two different active records')
        if states.TYPE_ENTITY.get(src['type'],src['type'])!=states.TYPE_ENTITY.get(dst['type'],dst['type']):
            raise ValueError('Only records of the same kind can be merged')
        if conn.execute('SELECT 1 FROM followups WHERE record_id=? UNION SELECT 1 FROM drafts WHERE record_id=?',(source_id,source_id)).fetchone():
            raise ValueError('The duplicate carries a follow-up or draft; move it by hand first')
        if conn.execute('SELECT 1 FROM outcomes WHERE record_id=?',(source_id,)).fetchone():
            raise ValueError('The duplicate has recorded outcomes; reconcile their provenance before merging it')
        if conn.execute('SELECT 1 FROM approach_next_steps WHERE record_id=?',(source_id,)).fetchone():
            raise ValueError('The duplicate has a recorded approach next step; reconcile its history before merging it')
        if src['type'] == 'job' and conn.execute("SELECT 1 FROM sqlite_master WHERE name='incoming_receipts'").fetchone() and conn.execute('SELECT 1 FROM incoming_receipts WHERE record_id=?',(source_id,)).fetchone():
            raise Conflict('The duplicate has a recorded hiring process. Reconcile its stage and private evidence before merging.')
        if src['type'] == 'job':
            source_choice = conn.execute("SELECT actor FROM record_edits WHERE record_id=? AND field='cv_override'", (source_id,)).fetchone()
            target_choice = conn.execute("SELECT actor FROM record_edits WHERE record_id=? AND field='cv_override'", (target_id,)).fetchone()
            if (source_choice or src['cv_override'] is not None) and (target_choice or dst['cv_override'] is not None) and src['cv_override'] != dst['cv_override']:
                raise Conflict('These jobs have different manual CV choices. Reconcile them before merging.')
            if (source_choice or src['cv_override'] is not None) and not target_choice and dst['cv_override'] is None:
                conn.execute('UPDATE records SET cv_override=? WHERE id=?', (src['cv_override'], target_id))
                mark_edited(conn, target_id, ['cv_override'], source_choice['actor'] if source_choice else actor)
                event(conn, target_id, 'cv_override', dst['cv_override'], src['cv_override'], f'Preserved from merged {source_id}', actor=actor)
        if src['type'] == 'person':
            # A place on the owner's list and how he reaches someone survive a merge; the survivor's own answer wins.
            kept = [k for k in PERSON_CHOICES if src[k] and not dst[k]]
            for k in kept:
                conn.execute(f'UPDATE records SET {k}=? WHERE id=?', (src[k], target_id))
                event(conn, target_id, k, None, src[k], f'Preserved from merged {source_id}', actor=actor)
            if kept:
                mark_edited(conn, target_id, kept, actor)
        for a,b in (('from_id','to_id'),('to_id','from_id')):
            for link_row in conn.execute(f'SELECT * FROM links WHERE {a}=?',(source_id,)).fetchall():
                if link_row[b]!=target_id:
                    ends={a:target_id,b:link_row[b]}
                    conn.execute('INSERT OR IGNORE INTO links(from_id,to_id,relation,notes) VALUES(?,?,?,?)',
                                 (ends['from_id'],ends['to_id'],link_row['relation'],link_row['notes']))
            conn.execute(f'DELETE FROM links WHERE {a}=?',(source_id,))
        for column in ('record_id','person_id','org_id'):
            conn.execute(f'UPDATE interactions SET {column}=? WHERE {column}=?',(target_id,source_id))
        conn.execute('UPDATE source_records SET record_id=? WHERE record_id=?',(target_id,source_id))
        for observation in conn.execute('SELECT source,observed_at,payload_json FROM observations WHERE record_id=?', (source_id,)).fetchall():
            add_observation(conn, target_id, observation['source'], observation['observed_at'], observation['payload_json'])
        note=f'Merged into {target_id}'+(f': {reason}' if reason else '')
        conn.execute('UPDATE records SET archived_at=? WHERE id=?',(db.now(),source_id))
        event(conn,source_id,'archived',None,target_id,note,actor=actor);bump(conn,source_id)
        event(conn,target_id,'merged',source_id,target_id,reason or f'Merged {source_id}',actor=actor);bump(conn,target_id)
    return {'saved':True,'record':get(conn,target_id)}


def merged_evidence_gaps(conn):
    """Legacy merges whose active survivor lacks an original sighting or its latest timing.

    Follow only recorded merge edges, including chains. Plain archives have no survivor.
    This read-only audit never reapplies old sourced facts to the survivor's fields.
    """
    active = {r[0] for r in conn.execute('SELECT id FROM records WHERE archived_at IS NULL')}
    edges, merges = {}, set()
    for row in conn.execute("SELECT entity_id,field,from_value,to_value FROM state_events WHERE field IN ('archived','merged') ORDER BY id"):
        if row['field'] == 'archived':
            edges[row['entity_id']] = row['to_value']
        else:
            merges.add((row['from_value'], row['entity_id']))
    edges = {src: dst for src, dst in edges.items() if src not in active and (src, dst) in merges}
    observations, available = {}, {}
    for row in conn.execute('SELECT id,record_id,source,observed_at,payload_sha FROM observations'):
        scanner = row['source'].startswith(db.SCANNER_SOURCES)
        key = (row['source'], row['payload_sha'], '' if scanner else row['observed_at'])
        observations.setdefault(row['record_id'], []).append((row, key))
        available.setdefault(row['record_id'], {})[key] = row['observed_at']
    out = []
    for src, target in sorted(edges.items()):
        seen = {src}
        while target not in active and target in edges and target not in seen:
            seen.add(target)
            target = edges[target]
        if target not in active:
            continue
        missing = [row['id'] for row, key in observations.get(src, [])
                   if key not in available.get(target, {}) or row['observed_at'] > available[target][key]]
        if missing:
            out.append(dict(source_id=src, target_id=target, observation_ids=missing))
    return out


def restore_merged_evidence(conn, actor='agent'):
    """Repair audited legacy merges through the same evidence boundary as new merges."""
    restored = []
    with transaction(conn):
        for gap in merged_evidence_gaps(conn):
            count = 0
            for oid in gap['observation_ids']:
                row = conn.execute('SELECT source,observed_at,payload_json FROM observations WHERE id=?', (oid,)).fetchone()
                count += add_observation(conn, gap['target_id'], row['source'], row['observed_at'], row['payload_json']).rowcount
            if count:
                event(conn, gap['target_id'], 'merge_evidence', gap['source_id'], gap['target_id'],
                      f'Restored {count} observations from the archived merge source; record fields unchanged.', actor=actor)
                bump(conn, gap['target_id'])
                restored.append(f"{gap['source_id']} -> {gap['target_id']}")
    return restored
