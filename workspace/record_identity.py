"""Canonical source upserts and organisation identity shared by imports and manual changes.

Callers supply the transaction, provenance and timestamp. No import orchestration lives here.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

# The flat row reader is shared with CLI adapters; this project uses path-based modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import row_shape as ed
import normalise
import record_core as store


# The CSV derives related_ids/related_names from links; the store keeps links only (migration 006).
TEXT_COLUMNS = [c for c in ed.COLUMNS if c not in ("status", "action", "action_state", "action_owner", "action_due",
                                                   "related_ids", "related_names")]
STATE_COLUMNS = ["status_raw", "status", "stage", "sub_state", "owner", "blocked_on", "kind", "relationship",
                 "triage_reason", "fields_json"]
# An import observes the source; it never decides local state. `status_raw` and `fields_json`
# record what the source said, so they refresh on every import. The normalised state fields are
# written once, on insert: a re-import must not revert a status transitioned in the workspace,
# and the daily scan must not put a triaged job back to `candidate`.
IMPORT_INSERT_ONLY = ["status", "stage", "sub_state", "owner", "blocked_on", "kind", "relationship",
                      "triage_reason"]


def match_organisation(conn, name):
    """The active organisation a name refers to, by the shared name key; an agency wins over a company
    copy. A name merged into another organisation leads to where it went. Never creates one."""
    key=normalise.org_key(name)
    if not key: return None
    rows=[dict(r) for r in conn.execute("SELECT id,type,name,archived_at FROM records WHERE type IN ('company','agency')")
          if normalise.org_key(r['name'])==key]
    rows.sort(key=lambda r:(r['type']!='agency',r['id']))
    live=[r for r in rows if not r['archived_at']]
    if live: return {k:live[0][k] for k in ('id','type','name')}
    for r in rows:
        rid,seen=r['id'],set()
        while rid not in seen:
            seen.add(rid)
            into=conn.execute("SELECT to_value FROM state_events WHERE entity_id=? AND field='archived' ORDER BY id DESC LIMIT 1",(rid,)).fetchone()
            target=into and conn.execute("SELECT id,type,name,archived_at FROM records WHERE id=? AND type IN ('company','agency')",(into[0],)).fetchone()
            if not target: break
            if not target['archived_at']: return {k:target[k] for k in ('id','type','name')}
            rid=target['id']
    return None



def upsert_record(conn: sqlite3.Connection, r: dict, state: dict, at: str) -> bool:
    """Insert or merge one record. An empty incoming value never erases a stored one, so a
    partial import (scans) keeps what a fuller one (research, an edit) established."""
    existing = conn.execute("SELECT * FROM records WHERE id = ?", (r["id"],)).fetchone()
    values = normalise.normalise_values({c: r.get(c, "") or "" for c in TEXT_COLUMNS})
    values.update({k: v for k, v in state.items() if v is not None and k != 'cv_override'})
    if existing is None:
        values.update(id=r["id"], created_at=at, updated_at=at)
        cols = list(values)
        conn.execute(f"INSERT INTO records ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [values[c] for c in cols])
        store.event(conn, r['id'], 'created', None, r['type'], actor='research' if r.get('source_file','').removeprefix('source:').startswith('research:') else 'source',
                    source_scope=r.get('source_file','').removeprefix('source:'), observed_at=at)
        return True
    # Dated discovery TSVs and legacy Markdown are history once current observations own facts.
    if conn.execute('SELECT 1 FROM source_records WHERE record_id=?',(r['id'],)).fetchone():
        return False
    protected = {x[0] for x in conn.execute('SELECT field FROM record_edits WHERE record_id=?',(r['id'],))}
    conflicts = {k:v for k,v in values.items() if k in protected and v not in (None,'') and existing[k]!=v}
    if conflicts:
        store.add_observation(conn, r['id'], 'import conflict', at, json.dumps(conflicts, sort_keys=True))
    changed = {k: v for k, v in values.items()
               if v not in (None, "") and existing[k] != v and k not in IMPORT_INSERT_ONLY and k not in protected}
    if changed:
        conn.execute("UPDATE records SET " + ", ".join(f"{k} = ?" for k in changed) + ", updated_at = ?, revision = revision + 1 WHERE id = ?",
                     [*changed.values(), at, r["id"]])
        audited = {k: {'before': existing[k], 'after': v} for k,v in changed.items()
                   if k not in ('fields_json', 'status_raw', 'source_file')}
        if audited:
            store.event(conn, r['id'], 'import_update', None, None, actor='research' if r.get('source_file','').removeprefix('source:').startswith('research:') else 'source',
                        source_scope=r.get('source_file','').removeprefix('source:'), changes=audited, observed_at=at)
    return False



def ensure_organisation(conn: sqlite3.Connection, name: str, scope: str, at: str, typ: str = 'company',
                        *, resolver: dict | None = None, **facts) -> tuple[str | None, bool]:
    """The organisation a name refers to, created once when none matches: (id, created).

    Matching uses the shared organisation key, prefers an agency over a company copy and ignores
    archived records. A created id is deterministic (`CO-`/`AG-` plus a slug), so a repeated import is
    a no-op. Every import door that meets an organisation by name uses this. A resolver memo is
    valid only within one scan import transaction, which never renames, merges or archives orgs."""
    key = normalise.org_key(name)
    if not key:
        return None, False
    if resolver is not None and key in resolver:
        return resolver[key], False
    match = match_organisation(conn, name)
    if match:
        if resolver is not None: resolver[key] = match['id']
        return match['id'], False
    if any(normalise.org_key(row['name']) == key for row in conn.execute(
            "SELECT name FROM records WHERE type IN ('company','agency') AND archived_at IS NOT NULL")):
        if resolver is not None: resolver[key] = None
        return None, False
    import hashlib
    clean_name = normalise.clean(name)
    oid = ('AG-' if typ == 'agency' else 'CO-') + ed.slug(clean_name)
    collision = conn.execute('SELECT name FROM records WHERE id=?', (oid,)).fetchone()
    if collision and normalise.org_key(collision[0]) != key:
        oid += '-' + hashlib.sha256(clean_name.encode()).hexdigest()[:8]
    created = upsert_record(conn, ed.row(id=oid, type=typ, name=clean_name, source_file='source:' + scope, **facts), {'kind': typ}, at)
    if resolver is not None: resolver[key] = oid
    return oid, created



def link_organisation(conn: sqlite3.Connection, rid: str, name: str, relation: str, scope: str, at: str,
                      *, resolver: dict | None = None) -> str | None:
    """Link an advert or tender to the organisation it names, creating the organisation when none
    matches. Both scan import paths use this."""
    oid, _ = ensure_organisation(conn, name, scope, at, resolver=resolver)
    if oid:
        conn.execute('INSERT OR IGNORE INTO links(from_id,to_id,relation) VALUES(?,?,?)', (rid, oid, relation))
    return oid
