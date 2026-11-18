#!/usr/bin/env python3
"""Data quality: invalid data (defects) and missing enrichment (gaps) in the working set.

A defect is data that is wrong or misleading and has a known fix; `wo quality --strict` fails while
any remain. A gap is something not yet known, such as a company with no description. It is listed
so the enrichment runbook can close it, and it never fails the check. Archived records are
evidence and are not checked.
"""
from __future__ import annotations

from pathlib import Path
import datetime as dt
import json
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workspace"))
import normalise  # noqa: E402
import profile_rules  # noqa: E402
import records  # noqa: E402
import relevance  # noqa: E402

ENTITY = re.compile(r"&(?:#\d+|#?x[0-9a-f]+|amp|quot|lt|gt|nbsp|apos);", re.I)
# Scanner and directory bookkeeping that once landed in `summary` instead of a description.
BOOKKEEPING = re.compile(r"^(?:Re-pointed \d{4}-\d{2}-\d{2}|Greenhouse slug 404|Existing directory links this company to|"
                         r"Workable board found|No board found)", re.I)
MARKUP = re.compile(r"</?(?:div|p|li|ul|ol|br|strong|em|span|a|h\d)\b[^>]*>|&lt;/?(?:div|p|li)\b", re.I)
CONTACT_IN_NAME = re.compile(r"^(?P<name>[^(]+?)\s*\((?P<note>[^()]*@[^()]*)\)\s*$")
EMAIL = re.compile(r"[^\s,;()]+@[^\s,;()]+\.[^\s,;()]+")
TITLE_FRAGMENT = re.compile(r"https?://|www\.|\bapply:\s|\.(?:com|io|dev|ai|co)/", re.I)

DEFECTS = {
    "date_free_text": "Date fields hold text, not a date. Keep the date (or clear it) and move the text to notes.",
    "html_entities": "HTML entities in names or text. Decode them; merge any duplicate this created.",
    "markup_in_text": "HTML markup stored as text. Strip it at import (web_read.strip_html).",
    "duplicate_organisation": "The same organisation recorded twice as the same type. `wo merge <duplicate> <keep>`.",
    "agency_company_overlap": "An agency also recorded as a company. `wo merge <company> <agency>`.",
    "advert_recorded_twice": "A scanner copy of an advert already recorded with the owner's work. `wo merge <scanner copy> <kept>`.",
    "title_fragment": "A job title containing a URL or apply link. Edit the title.",
    "bookkeeping_summary": "Scanner bookkeeping shown as a description. Move it to notes.",
    "truncated_action": "A next action that starts mid-sentence. Restore it from its source.",
    "unlinked_advert": "A job naming an existing organisation without a link to it. Link it (`hiring_for`).",
    "unlinked_person_organisation": "A person naming an existing organisation (or warm/priority person with an employer) without a link to it. Link it (`works_at` or `consultant_at`).",
    "broken_link": "A link to a record that does not exist.",
    "person_recorded_twice": "Two active people share a LinkedIn profile or email. `wo merge <duplicate> <keep>`.",
    "contact_in_name": "A person's name carries contact details, e.g. 'Jack (jack@x.com, on file)'. Move the email to its field.",
    "merged_evidence_unavailable": "An archived merge source has evidence missing from its active survivor. `wo repair` copies it, retaining originals and current fields.",
}
GAPS = {
    "company_without_description": "No description of what the company does. Add one through a research batch.",
    "person_without_summary": "No summary of who the person is. Add one through a research batch.",
    "advert_without_organisation": "A job with no organisation stated.",
    "pending_review": "Import candidates waiting for the owner's accept, merge or reject.",
    "people_sharing_a_name": "Active people with the same name. Check whether they are one person; merge if so.",
    "organisations_sharing_a_name": "One organisation name inside another ('Sanderson' / 'Sanderson Recruitment'). Check the adverts; `wo merge` if one firm.",
    "generic_research_signal": "The only research \"signal\" is a sentence shared word for word by three or more records, and nothing else on the record (a current advert, funding, a contact) shows a reason to approach. Add a sourced, specific one in a research batch if the company matters now.",
    "cross_source_fact_disagreement": "Attached advert sources state different locations or engagements. Review the evidence; repair does not split records or choose facts.",
}


def _active(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM records WHERE archived_at IS NULL ORDER BY id")]


def url_duplicates(conn, rows) -> list[tuple[str, str]]:
    """(duplicate, keep) for active jobs sharing one advert URL. The copy carrying the owner's work
    (follow-up or draft) is kept, then the one mapped to a source identity, then the oldest id."""
    worked = {r[0] for r in conn.execute("SELECT record_id FROM followups UNION SELECT record_id FROM drafts")}
    mapped = {r[0] for r in conn.execute("SELECT DISTINCT record_id FROM source_records")}
    by_url = {}
    for r in rows:
        if r["type"] == "job" and r["url"]:
            by_url.setdefault(r["url"], []).append(r["id"])
    pairs = []
    for ids in by_url.values():
        if len(ids) > 1:
            ids.sort(key=lambda i: (i not in worked, i not in mapped, i))
            pairs += [(i, ids[0]) for i in ids[1:]]
    return pairs


def organisation_name_variants(firms) -> list[str]:
    """Whole-word prefix matches, indexed once instead of comparing every pair of firms.

    Equal keys belong to the duplicate-organisation check. Original pair order is retained so
    the quality report does not churn. Cost follows names and actual matches, not all pairs.
    """
    keys = [normalise.org_key(r['name']) for r in firms]
    index = {}
    for i, key in enumerate(keys):
        if key:
            index.setdefault(key, []).append(i)
    pairs = []
    for i, key in enumerate(keys):
        words = key.split(' ')
        for end in range(1, len(words)):
            for j in index.get(' '.join(words[:end]), ()):
                pairs.append((min(i, j), max(i, j)))
    return [' ~ '.join(sorted((firms[i]['id'], firms[j]['id']))) for i, j in sorted(pairs)]


def reasons_shown(conn: sqlite3.Connection, loaded: list[dict]) -> dict[str, bool]:
    """Whether the ranking shows each record a reason to approach, read-only.

    It scores the same inputs `records.state()` gives the ranking, without composing This week or
    writing anything. A ranking failure reports every record as showing no reason.
    """
    import record_views
    try:
        scores = relevance.score(dict(
            records=loaded,
            links=[dict(r) for r in conn.execute('SELECT * FROM links ORDER BY from_id,to_id,relation')],
            interactions=[dict(r) for r in conn.execute(records.LIVE_INTERACTIONS + ' ORDER BY at DESC,id DESC')],
            drafts=[dict(r) for r in conn.execute('SELECT * FROM drafts ORDER BY record_id')],
            decisions=records.decisions_history(conn), sightings=record_views.last_sightings(conn)))
    except Exception:
        return {}
    return {rid: bool(s.get('approach') or s.get('band') in ('strong', 'possible')) for rid, s in scores.items()}


def check(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    rows = _active(conn)
    by_id = {r["id"]: r for r in rows}
    found = {k: [] for k in (*DEFECTS, *GAPS)}
    orgs = [r for r in rows if r["type"] in ("company", "agency")]
    org_by_key = {}
    for r in rows:
        for f in ("date", "published_deadline", "last_touch"):
            if not normalise.is_factual_date(r[f] or ""):
                found["date_free_text"].append(f"{r['id']}.{f}={r[f]!r}")
        if any(ENTITY.search(r[f] or "") for f in ("name", "organisation", "role", "summary")):
            found["html_entities"].append(r["id"])
        if any(MARKUP.search(r[f] or "") for f in ("name", "summary", "notes")):
            found["markup_in_text"].append(r["id"])
        if r["type"] == "job" and TITLE_FRAGMENT.search(r["name"] or ""):
            found["title_fragment"].append(r["id"])
        if BOOKKEEPING.search(r["summary"] or ""):
            found["bookkeeping_summary"].append(r["id"])
        if re.match(r"^or\s", r["next_action"] or ""):
            found["truncated_action"].append(r["id"])
        if r["type"] == "person" and CONTACT_IN_NAME.match(r["name"] or ""):
            found["contact_in_name"].append(r["id"])
    for r in orgs:
        org_by_key.setdefault((r["type"], normalise.org_key(r["name"])), []).append(r["id"])
    for (kind, key), ids in org_by_key.items():
        if len(ids) > 1:
            found["duplicate_organisation"].append(" = ".join(ids))
        if kind == "agency" and ("company", key) in org_by_key:
            found["agency_company_overlap"].append(f"{org_by_key[('company', key)][0]} -> {ids[0]}")
    linked_org = {r[0] for r in conn.execute(
        "SELECT l.from_id FROM links l JOIN records o ON o.id=l.to_id WHERE o.type IN ('company','agency') AND o.archived_at IS NULL")}
    names = {normalise.org_key(o["name"]) for o in orgs}
    for r in rows:
        if r["type"] != "job":
            continue
        if not r["organisation"]:
            found["advert_without_organisation"].append(r["id"])
        elif r["id"] not in linked_org and normalise.org_key(r["organisation"]) in names:
            found["unlinked_advert"].append(r["id"])
    for dup, keep in url_duplicates(conn, rows):
        found["advert_recorded_twice"].append(f"{dup} -> {keep}")
    all_ids = {r[0] for r in conn.execute("SELECT id FROM records")}
    for a, b in conn.execute("SELECT from_id, to_id FROM links"):
        if a not in all_ids or b not in all_ids:
            found["broken_link"].append(f"{a} -> {b}")
    people = [r for r in rows if r["type"] == "person"]
    for field in ("url", "email"):
        seen = {}
        for r in people:
            key = (r[field] or "").rstrip("/").casefold()
            if key:
                seen.setdefault(key, []).append(r["id"])
        found["person_recorded_twice"] += [" = ".join(ids) for ids in seen.values() if len(ids) > 1]
    by_name = {}
    for r in people:
        by_name.setdefault(normalise.org_key(r["name"]), []).append(r["id"])
    found["people_sharing_a_name"] = [" = ".join(ids) for ids in by_name.values() if len(ids) > 1]
    firms = [r for r in orgs if (r["kind"] or r["type"]) in ("company", "agency")]
    found['organisations_sharing_a_name'] = organisation_name_variants(firms)
    for r in rows:
        if r["type"] == "company" and not r["summary"]:
            found["company_without_description"].append(r["id"])
        if r["type"] == "person" and not r["summary"]:
            found["person_without_summary"].append(r["id"])
    found["pending_review"] = [str(r[0]) for r in conn.execute("SELECT id FROM review_queue WHERE resolved_at IS NULL")]
    import record_views
    loaded = record_views.load_records(conn)
    linked_people = {r[0] for r in conn.execute(
        "SELECT l.from_id FROM links l JOIN records o ON o.id=l.to_id "
        "WHERE o.type IN ('company','agency') AND o.archived_at IS NULL AND l.relation IN ('works_at','consultant_at','former_colleague_at','formerly_at') "
        "UNION SELECT l.to_id FROM links l JOIN records o ON o.id=l.from_id "
        "WHERE o.type IN ('company','agency') AND o.archived_at IS NULL AND l.relation IN ('works_at','consultant_at','former_colleague_at','formerly_at')")}
    interactions = [dict(r) for r in conn.execute(records.LIVE_INTERACTIONS + " ORDER BY at DESC,id DESC")]
    today = dt.date.today()
    for r in loaded:
        if r["type"] != "person" or r["id"] in linked_people or r.get("relationship") == "do_not_contact":
            continue
        li = r.get("linkedin") or {}
        if profile_rules.current().flagged(li):
            continue
        comp = r.get("organisation") or li.get("observed_company") or ""
        k = normalise.employer_key(comp)
        if not k:
            continue
        tier = relevance.reach_of(r, interactions, today).get("tier")
        is_warm = bool(r.get("priority") or r.get("relationship") in ("warm", "active", "dormant") or tier in ("close", "in_touch", "known"))
        if k in names or is_warm:
            found["unlinked_person_organisation"].append(r["id"])
    studied = [r for r in loaded if r['research']]
    generic = relevance.generic_signals(studied)
    # A record is short of a reason to approach only when every signal it has is generic and the
    # ranking shows no other reason either (a current advert, funding, a contact or a strong fit).
    signals = lambda r: [x['signal'] for x in r['research'] if x.get('signal')]
    bare = [r['id'] for r in studied if signals(r) and all(s in generic for s in signals(r))]
    shown = reasons_shown(conn, loaded) if bare else {}
    found['generic_research_signal'] = [rid for rid in bare if not shown.get(rid)]
    found['merged_evidence_unavailable'] = [f"{g['source_id']} -> {g['target_id']}" for g in records.merged_evidence_gaps(conn)]
    found['cross_source_fact_disagreement'] = cross_source_disagreements(conn)
    section = lambda names: {k: dict(count=len(found[k]), fix=names[k], items=found[k]) for k in names}
    defects, gaps = section(DEFECTS), section(GAPS)
    return dict(records=len(by_id), defects=defects, gaps=gaps,
                defect_total=sum(v["count"] for v in defects.values()),
                gap_total=sum(v["count"] for v in gaps.values()))


def cross_source_disagreements(conn):
    """Latest facts from each native identity attached to an active advert; missing stays neutral."""
    latest = {}
    for row in conn.execute("SELECT o.record_id,o.observed_at,o.id,o.payload_json FROM observations o "
                            "JOIN records r ON r.id=o.record_id WHERE r.type='job' AND r.archived_at IS NULL"):
        payload = json.loads(row['payload_json'])
        if not isinstance(payload, dict) or not payload.get('source_scope') or not payload.get('source_id'):
            continue
        key = (row['record_id'], payload['source_scope'], str(payload['source_id']))
        order = (normalise.observation_time(row['observed_at']), row['id'])
        if key not in latest or order > latest[key][0]:
            current = payload.get('current') or {}
            latest[key] = (order, {**current, 'engagement': current.get('engagement') or current.get('employment_type')})
    grouped = {}
    for (rid, _, _), (_, facts) in latest.items():
        grouped.setdefault(rid, []).append(facts)
    return sorted(rid for rid, facts in grouped.items()
                  if any(not normalise.compatible_adverts(a, b) for i, a in enumerate(facts) for b in facts[i + 1:]))


def format_report(report: dict, limit: int = 8) -> str:
    out = [f"{report['records']} active records: {report['defect_total']} defects, {report['gap_total']} gaps"]
    for title, part in (("Defects", report["defects"]), ("Gaps", report["gaps"])):
        out.append(f"\n{title}")
        for name, v in part.items():
            mark = "ok" if not v["count"] else str(v["count"])
            out.append(f"  {name:<30} {mark:>4}" + (f"  {v['fix']}" if v["count"] else ""))
            for item in v["items"][:limit]:
                out.append(f"      {item}")
            if v["count"] > limit:
                out.append(f"      … {v['count'] - limit} more (--json for all)")
    return "\n".join(out)


MANUAL = ("truncated_action", "broken_link", "markup_in_text", "person_recorded_twice")


def _clean_title(title: str) -> str:
    parts = [p.strip() for p in title.split("|")]
    kept = [p for p in parts if p and not TITLE_FRAGMENT.search(p)]
    return " | ".join(kept) or title


def repair(conn: sqlite3.Connection, actor: str = "agent", dry_run: bool = False) -> dict:
    """Apply every deterministic fix through the record service. Truncated actions need their source
    and broken links need a decision, so they are reported only."""
    conn.row_factory = sqlite3.Row
    report = check(conn)
    done = {k: [] for k in DEFECTS}
    rev = lambda rid: records.get(conn, rid)["revision"]
    if dry_run:
        return dict(dry_run=True, would_fix={k: v["count"] for k, v in report["defects"].items()
                                             if v["count"] and k not in MANUAL})
    for r in _active(conn):
        changed = records.normalise_record(conn, r["id"], actor=actor)
        dated = any(f in normalise.DATE_FIELDS for f in changed)
        for field in changed:
            bucket = "date_free_text" if field in normalise.DATE_FIELDS or (field == "notes" and dated) else "html_entities"
            done[bucket].append(f"{r['id']}.{field}")
    fresh = check(conn)
    for item in fresh["defects"]["duplicate_organisation"]["items"]:
        ids = item.split(" = ")
        ids.sort(key=lambda i: (bool(ENTITY.search(records.get(conn, i)["name"])),
                                -conn.execute("SELECT count(*) FROM links WHERE from_id=? OR to_id=?", (i, i)).fetchone()[0], i))
        for dup in ids[1:]:
            records.merge(conn, dup, ids[0], dict(revision=rev(dup), reason="Same organisation recorded twice"), actor=actor)
            done["duplicate_organisation"].append(f"{dup} -> {ids[0]}")
    for item in fresh["defects"]["agency_company_overlap"]["items"]:
        company, agency = item.split(" -> ")
        records.merge(conn, company, agency, dict(revision=rev(company), reason="Agency also recorded as a company"), actor=actor)
        done["agency_company_overlap"].append(item)
    for dup, keep in url_duplicates(conn, _active(conn)):
        records.merge(conn, dup, keep, dict(revision=rev(dup), reason="Same advert URL recorded twice"), actor=actor)
        done["advert_recorded_twice"].append(f"{dup} -> {keep}")
    for rid in check(conn)["defects"]["title_fragment"]["items"]:
        r = records.get(conn, rid)
        records.edit(conn, rid, dict(revision=r["revision"], name=_clean_title(r["name"])), actor=actor)
        done["title_fragment"].append(rid)
    for rid in check(conn)["defects"]["bookkeeping_summary"]["items"]:
        r = records.get(conn, rid)
        notes = r["notes"] if r["summary"] in (r["notes"] or "") else (f"{r['notes']} {r['summary']}".strip())
        records.edit(conn, rid, dict(revision=r["revision"], summary="", notes=notes), actor=actor)
        done["bookkeeping_summary"].append(rid)
    orgs = [r for r in _active(conn) if r["type"] in ("company", "agency")]
    for rid in check(conn)["defects"]["unlinked_advert"]["items"]:
        r = records.get(conn, rid)
        match = sorted((o for o in orgs if normalise.org_key(o["name"]) == normalise.org_key(r["organisation"])),
                       key=lambda o: (o["type"] != "agency", o["id"]))[0]
        records.link(conn, dict(from_id=rid, to_id=match["id"], relation="hiring_for", revision=r["revision"],
                                notes="Linked by wo repair: the advert names this organisation"), actor=actor)
        done["unlinked_advert"].append(f"{rid} -> {match['id']}")
    for rid in check(conn)["defects"]["unlinked_person_organisation"]["items"]:
        r = records.get(conn, rid)
        li = r.get("linkedin") or {}
        comp = r.get("organisation") or li.get("observed_company") or ""
        match = records.match_organisation(conn, comp)
        if not match:
            from record_imports import ensure_organisation
            typ = "agency" if r.get("kind") == "recruiter" else "company"
            org_id, _ = ensure_organisation(conn, comp, "repair", records.db.now(), typ=typ)
            match = records.get(conn, org_id) if org_id else None
        if match:
            rel = "consultant_at" if match.get("type") == "agency" or r.get("kind") == "recruiter" else "works_at"
            note = "Linked by wo repair: the person record names this organisation" if r.get("organisation") else "LinkedIn export; unverified"
            records.link(conn, dict(from_id=rid, to_id=match["id"], relation=rel, revision=r["revision"], notes=note), actor=actor)
            done["unlinked_person_organisation"].append(f"{rid} -> {match['id']}")
    for rid in check(conn)["defects"]["contact_in_name"]["items"]:
        r = records.get(conn, rid)
        m = CONTACT_IN_NAME.match(r["name"])
        email = EMAIL.search(m["note"])
        rest = EMAIL.sub("", m["note"]).strip(" ,;")
        changes = dict(revision=r["revision"], name=m["name"].strip())
        if email and not r["email"]:
            changes["email"] = email[0].lower()
        if rest:
            changes["notes"] = f"{r['notes'] or ''}\nName note: {rest}".strip()
        records.edit(conn, rid, changes, actor=actor)
        done["contact_in_name"].append(rid)
    done['merged_evidence_unavailable'] = records.restore_merged_evidence(conn, actor=actor)
    after = check(conn)
    return dict(fixed={k: len(v) for k, v in done.items() if v}, details={k: v for k, v in done.items() if v},
                remaining={k: v["count"] for k, v in after["defects"].items() if v["count"]})
