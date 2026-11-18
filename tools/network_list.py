#!/usr/bin/env python3
"""The owner's list: the spreadsheet he fills in, and the import of his answers
(archive/plans/2026-10-14-outreach-focus.md, Release 2).

  network_list.py sheet --db <pulled backup> --out data/research/<date>-network-review.csv
  network_list.py check <returned.csv> --key <key.json> --out data/research/<date>-list.json
  wo import-list data/research/<date>-list.json [--dry-run]

The sheet, its key and every file naming people stay in data/research/ and are never committed (#30).
LinkedIn people reach the site only through linkedin_export.card and records.linkedin_card, the one
LinkedIn path (archive/plans/2026-09-22-linkedin-export.md). Commands print counts and row numbers, never names.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workspace"))
sys.path.insert(0, str(ROOT / "tools"))
import linkedin_export  # noqa: E402
import normalise  # noqa: E402
import profile_rules  # noqa: E402
import records  # noqa: E402
import states  # noqa: E402

SNAPSHOT = ROOT / "data/imports/linkedin/2026-09-21.json"
NEVER = ROOT / "data/research/network-never.json"
COLUMNS = ["#", "Group", "Name", "Role", "Company", "Connected", "Last two-way", "How well (W/T/K/N)",
           "Priority (1/2)", "Who", "Channel", "Ask", "Notes", "Two-way threads", "Endorsement", "On site",
           "Live adverts at company", "LinkedIn"]
# Columns are found by the start of their heading, ignoring case, so a column the owner adds or renames
# slightly in a spreadsheet still counts.
HEADINGS = {"n": "#", "group": "group", "name": "name", "how_well": "how well", "priority": "priority", "who": "who",
            "channel": "channel", "ask": "ask", "notes": "note", "linkedin": "linkedin"}
EDITABLE = ("how_well", "priority", "who", "channel", "ask", "notes")

HOW_WELL = {"w": "warm", "t": "active", "k": "dormant", "n": "do_not_contact", "know well": "warm",
            "in touch": "active", "know from before": "dormant", "never": "do_not_contact",
            "never contact": "do_not_contact"}
LETTER = {"warm": "W", "active": "T", "dormant": "K", "do_not_contact": "N"}
PRIORITY = {"1": "first", "2": "next", "first": "first", "next": "next", "p1": "first", "p2": "next"}
WHO = {"colleague": "former_colleague", "former colleague": "former_colleague", "ex colleague": "former_colleague",
       "hired me": "former_manager", "manager": "former_manager", "former manager": "former_manager",
       "boss": "former_manager", "recruiter": "recruiter", "agency": "recruiter", "talent": "in_house_talent",
       "in-house talent": "in_house_talent", "in house talent": "in_house_talent", "founder": "founder",
       "peer": "peer", "friend": "friend", "introducer": "introducer", "hiring manager": "hiring_manager",
       "other": "other"}
CHANNEL = {"call": "call", "phone": "call", "whatsapp": "whatsapp", "wa": "whatsapp", "text": "text", "sms": "text",
           "email": "email", "e-mail": "email", "linkedin": "linkedin", "li": "linkedin", "in person": "in_person",
           "in-person": "in_person", "meet": "in_person", "coffee": "in_person"}
# Three kinds of ask (14 October). The sheet's earlier "referral" and "view" still read.
ASK = {"catch-up": "catch_up", "catch up": "catch_up", "catchup": "catch_up", "view": "catch_up", "inside view": "catch_up",
       "advice": "catch_up", "intro": "introduction", "introduction": "introduction", "referral": "introduction",
       "availability": "availability", "available": "availability"}
WORD = {"who": {v: k for k, v in reversed(list(WHO.items()))},
        "channel": {"call": "call", "whatsapp": "WhatsApp", "text": "text", "email": "email", "linkedin": "LinkedIn",
                    "in_person": "in person"},
        "ask": {"catch_up": "catch-up", "introduction": "intro", "availability": "availability"}}
# Notes marked `private:` stay in the local file. A note that touches a private topic from the profile
# (`profile_rules`) is never processed or sent to the site: only its row number is reported, so it can be
# removed.
PRIVATE = re.compile(r"^\s*private\s*:", re.I)

RECRUITER = re.compile(r"recruit|talent|resourcing|sourcing|headhunt|\bsearch\b", re.I)
SENIOR = re.compile(r"\b(cto|ceo|coo|cio|cpo|chief|founder|co-founder|cofounder|vp|vice president|director|head of|partner)\b", re.I)
TECH = re.compile(r"engineer|technolog|\btech\b|software|quality|\bqa\b|\btest|product|\bdata\b|\bai\b|platform|delivery|"
                  r"digital|\bit\b|\bcto\b|\bcio\b|\bcpo\b|architect|devops|security|machine learning|\bml\b", re.I)
# A recorded shared employer is history the owner has with someone even without LinkedIn messages
# (tools/mail_colleagues.py records it from mail headers).
COLLEAGUE_KINDS = ("former_colleague", "former_manager")
RELEVANT_DESK = re.compile(r"\bqa\b|quality|\btest|interim|engineer|technolog|\btech\b|software|\bit\b|digital|contract", re.I)


def canonical(url: str | None) -> str:
    return (url or "").strip().rstrip("/").lower()


def _cell(value) -> str:
    return " ".join(str(value or "").split())


def _token(value: str) -> str:
    text = _cell(value).lower()
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".")[0]  # a spreadsheet's 2.0 is the 2 the owner typed
    return re.sub(r"[.!?]+$", "", text)


# --- the sheet ----------------------------------------------------------------------------------

def _read_site(conn: sqlite3.Connection) -> dict:
    """What the sheet needs from a database copy: people, organisations, live adverts and the review
    history. Tolerates a copy from before migration 012."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM records WHERE archived_at IS NULL")]
    people = {canonical(r["url"]): r for r in rows if r["type"] == "person" and r.get("url")}
    listed = [r for r in rows if r["type"] == "person" and r.get("priority")]
    orgs = {normalise.org_key(r["name"]) for r in rows if r["type"] in ("company", "agency")}
    adverts = {}
    for r in rows:
        if r["type"] == "job" and r.get("source_lifecycle") == "active" and r.get("organisation"):
            adverts[normalise.org_key(r["organisation"])] = adverts.get(normalise.org_key(r["organisation"]), 0) + 1
    reviewed = {}
    for payload, resolution in conn.execute("SELECT payload_json,resolution FROM review_queue WHERE kind='linkedin_person'"):
        reviewed[canonical(json.loads(payload).get("url"))] = (resolution or "").split(":")[0]
    return dict(people=people, listed=listed, orgs=orgs, adverts=adverts, reviewed=reviewed)


def _evidence(p: dict) -> tuple[int, str, bool]:
    t = p["evidence"].get("threads") or {}
    two_way = t.get("two_way") or 0
    endorsed = bool(p["evidence"].get("endorsed_you") or p["evidence"].get("you_endorsed") or p["evidence"].get("recommendations"))
    return two_way, (t.get("last_two_way") or "") if two_way else "", endorsed


def group_of(position: str, history: bool) -> str | None:
    """Who someone is, from their title alone: a recruiter, a senior tech or other leader, or someone
    The owner has history with. Titles match on word boundaries ("cto" is not inside "Director")."""
    if RECRUITER.search(position):
        return "recruiter"
    if SENIOR.search(position):
        return "senior tech" if TECH.search(position) else "senior other"
    return "history" if history else None


def score(p: dict, group: str, record: dict | None, adverts: int, company_on_site: bool) -> float:
    """Evidence that the owner knows someone, how recent it is, and how useful they could be."""
    two_way, last, endorsed = _evidence(p)
    year = int(last[:4] or 0)
    recency = 3 if year >= 2025 else 2 if year >= 2023 else 1 if year >= 2019 else 0
    role = {"senior tech": 2, "senior other": 0.5}.get(group, 0)
    if group == "recruiter":
        role = 1 if RELEVANT_DESK.search((p.get("position") or "") + " " + (p.get("company") or "")) else 0
    colleague = bool(record) and record.get("kind") in COLLEAGUE_KINDS
    return (3 * endorsed + 3 * colleague + 2 * min(two_way, 3) + recency + role + bool(record)
            + bool(adverts or company_on_site))


def sheet_rows(snapshot: dict, site: dict) -> tuple[list[dict], list[dict]]:
    """The sheet's rows and their key: the user's current list first, then the ranked pool, then everyone
    else outside the former employer. People already turned down or tagged are left out."""
    rows, key = [], []

    def add(group, p, record, adverts):
        two_way, last, endorsed = _evidence(p) if p else (0, "", False)
        n = len(rows) + 1
        key.append(dict(n=n, url=(p or {}).get("url") or (record or {}).get("url") or "", record_id=(record or {}).get("id")))
        row = {c: "" for c in COLUMNS}
        row.update({"#": n, "Group": group, "Name": (p or {}).get("name") or (record or {}).get("name") or "",
                    "Role": _cell((p or {}).get("position") or (record or {}).get("role")),
                    "Company": _cell((p or {}).get("company") or (record or {}).get("organisation")),
                    "Connected": ((p or {}).get("connected_date") or "")[:4], "Last two-way": last[:4],
                    "Two-way threads": two_way or "", "Endorsement": "yes" if endorsed else "",
                    "On site": "yes" if record else "", "Live adverts at company": adverts or "",
                    "LinkedIn": (p or {}).get("url") or (record or {}).get("url") or ""})
        if record:
            row.update({"How well (W/T/K/N)": LETTER.get(record.get("relationship") or "", ""),
                        "Priority (1/2)": {"first": "1", "next": "2"}.get(record.get("priority") or "", ""),
                        "Who": WORD["who"].get(record.get("kind") or "", ""),
                        "Channel": WORD["channel"].get(record.get("preferred_channel") or "", ""),
                        "Ask": WORD["ask"].get(record.get("preferred_ask") or "", "")})
        rows.append(row)

    people = {canonical(p["url"]): p for p in snapshot["people"].values()}
    for record in sorted(site["listed"], key=lambda r: (r["priority"] != "first", r["name"].casefold())):
        add("Your list", people.get(canonical(record.get("url"))), record, 0)
    shown = {canonical(r.get("url")) for r in site["listed"]}
    pool, rest = [], []
    for url, p in people.items():
        record = site["people"].get(url)
        if url in shown or p.get("flags") or site["reviewed"].get(url) == "reject":
            continue  # on the list already, at the former employer, or turned down in a review
        if record and record.get("relationship"):
            continue  # already tagged
        two_way, _, endorsed = _evidence(p)
        colleague = bool(record) and record.get("kind") in COLLEAGUE_KINDS
        group = group_of(p.get("position") or "", bool(two_way or endorsed or colleague))
        key_ = normalise.org_key(p.get("company") or "")
        adverts = site["adverts"].get(key_, 0)
        if group is None:
            rest.append((p, record))
            continue
        last = _evidence(p)[1]
        pool.append((-score(p, group, record, adverts, key_ in site["orgs"]), -min(two_way, 99), -int(last[:4] or 0),
                     not record, -int((p.get("connected_date") or "0")[:4] or 0), p["name"].casefold(), p, record, adverts))
    pool.sort(key=lambda x: x[:6])
    for i, item in enumerate(pool):
        add("Top 30" if i < 30 else "Ranked", item[6], item[7], item[8])
    rest.sort(key=lambda x: (x[0].get("connected_date") or "", x[0]["name"].casefold()), reverse=True)
    for p, record in rest:
        add("Others", p, record, 0)
    return rows, key


def write_sheet(rows: list[dict], key: list[dict], out: Path, export: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:  # a byte-order mark keeps accents right in Excel
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    out.with_suffix(".json").write_text(json.dumps(dict(
        purpose=f"Row numbers (#) in {out.name} → profile URL and site record. Never committed (#30).",
        export=export, made=dt.date.today().isoformat(), rows=key), indent=1, ensure_ascii=False))


# --- checking a returned sheet ------------------------------------------------------------------

def _headings(fieldnames: list[str]) -> dict:
    found = {}
    for name in fieldnames or []:
        low = name.strip().lower()
        for field, start in HEADINGS.items():
            if field not in found and (low == start if field == "n" else low.startswith(start)):
                found[field] = name
    return found


def _read(sheet: Path) -> tuple[list[str], list[dict], str]:
    """The sheet's headings, its rows and how the heading row was repaired ("", "blank" or "missing").
    Google Sheets can lose the `#` heading: left blank, or deleted with the rest of the heading row
    moved one column left (the owner's 15 October copy). When the first column holds row numbers, the
    heading is put back; the #↔LinkedIn check in `check` still stops a wrong repair."""
    with sheet.open(encoding="utf-8-sig", newline="") as f:
        table = list(csv.reader(f))
    if not table:
        return [], [], ""
    header, body = [h.strip() for h in table[0]], table[1:]
    filled = [r for r in body if any(c.strip() for c in r)]
    numbered = bool(filled) and any(r[0].strip() for r in filled) and all(
        re.fullmatch(r"\d+(?:\.0+)?|", r[0].strip()) for r in filled)
    width, repaired = max((len(r) for r in filled), default=len(header)), ""
    if "#" not in header and numbered:
        if header and not header[0] and len(header) == width:
            header[0], repaired = "#", "blank"
        elif len(header) == width - 1:
            header, repaired = ["#", *header], "missing"
    return header, [dict(zip(header, r)) for r in body], repaired


def _near(text: str, table: dict) -> str | None:
    """The declared word a misspelt cell plainly means ("colleauge" → colleague), or None. Short words
    and cells close to words with different meanings are never guessed."""
    if len(text) < 5:
        return None
    close = sorted(((difflib.SequenceMatcher(None, text, k).ratio(), k) for k in table), reverse=True)
    close = [(ratio, k) for ratio, k in close if ratio >= 0.85]
    return close[0][1] if close and len({table[k] for _, k in close}) == 1 else None


def _pick(raw: str, table: dict, near: bool = False) -> tuple[str | None, bool, str]:
    """The declared value for a cell, whether the cell held more than one value, and the word a
    misspelling was read as (empty when the cell was spelt as declared)."""
    text = _token(raw)
    if not text:
        return None, False, ""
    if text in table:
        return table[text], False, ""
    parts = [p.strip() for p in re.split(r"\s*(?:,|/|;|\bor\b|\band\b|\+)\s*", text) if p.strip()]
    for part in parts:
        if part in table:
            return table[part], len(parts) > 1, ""
    for part in parts if near else ():
        key = _near(part, table)
        if key:
            return table[key], len(parts) > 1, key
    return "?", False, ""


def check(sheet: Path, key_path: Path, snapshot: dict) -> tuple[dict, dict]:
    """Turn the owner's returned sheet into an import file. Returns (doc, report). Rows that cannot be
    imported as they stand are listed by row number in the report, never imported silently."""
    key = json.loads(key_path.read_text(encoding="utf-8"))
    by_n = {int(r["n"]): r for r in key["rows"]}
    people = {canonical(p["url"]): p for p in snapshot["people"].values()}
    fieldnames, lines, repaired = _read(sheet)
    head = _headings(fieldnames)
    report = dict(rows=len(lines), untouched=0, imported=0, by=dict(how_well={}, priority={}, who={}, channel={}, ask={}),
                  notes=0, private_notes=0, withheld_notes=[], never_off_site=0, problems=[], several_values=[],
                  corrections=[], priority_column=bool(head.get("priority")), header=repaired, stopped="")
    missing = [h for h in ("n", "linkedin", "how_well", "name") if h not in head]
    if missing:
        report["stopped"] = "the sheet has lost its " + ", ".join(HEADINGS[m] for m in missing) + " column"
        return dict(rows=[]), report
    rows, never = [], []
    for line_no, line in enumerate(lines, 2):
        raw = {k: _cell(line.get(head[k])) if k in head else "" for k in HEADINGS}
        if not any(raw[k] for k in EDITABLE):
            report["untouched"] += 1
            continue
        where = f"row {raw['n'] or '(added, line ' + str(line_no) + ')'}"
        entry = None
        if raw["n"]:
            try:
                n = int(float(raw["n"]))
            except ValueError:
                report["problems"].append(f"{where}: the # is not a number")
                continue
            entry = by_n.get(n)
            if not entry:
                report["problems"].append(f"{where}: not in the sheet's key")
                continue
            if canonical(raw["linkedin"]) != canonical(entry["url"]):
                report["stopped"] = f"{where}: its LinkedIn link does not match the key, so rows may have moved"
                return dict(rows=[]), report
        values, bad, fixed = {}, [], []
        for field, table in (("relationship", HOW_WELL), ("kind", WHO), ("preferred_channel", CHANNEL), ("preferred_ask", ASK)):
            column = {"relationship": "how_well", "kind": "who", "preferred_channel": "channel", "preferred_ask": "ask"}[field]
            value, several, read_as = _pick(raw[column], table, near=True)
            if value == "?":
                bad.append(field.replace("preferred_", ""))
            elif value:
                values[field] = value
                if several:
                    report["several_values"].append(f"{where}: {field.replace('preferred_', '')}")
                if read_as:
                    fixed.append(f"{where}: {column.replace('_', ' ')} '{_token(raw[column])}' read as '{read_as}'")
        if head.get("priority"):
            value, _, _ = _pick(raw["priority"], PRIORITY)
            if value == "?":
                bad.append("priority")
            values["priority"] = value if value != "?" else None
        elif {"relationship", "preferred_channel", "preferred_ask"} & values.keys() and values.get("relationship") != "do_not_contact":
            values["priority"] = "next"  # the earlier copy had no Priority column: everyone he tagged is on the list
        if bad:
            report["problems"].append(f"{where}: unrecognised {', '.join(bad)}")
            continue
        report["corrections"] += fixed
        # Only the flag is used from a withheld or private note: the obligations warning matters most there.
        rules = profile_rules.current()
        note, former_employer = raw["notes"], rules.mentions_employer(raw["notes"])
        if note and PRIVATE.match(note):
            report["private_notes"] += 1
            note = ""
        elif rules.is_private(note):
            report["withheld_notes"].append(where)
            note = ""
        elif note:
            report["notes"] += 1
        tagged = {k: v for k, v in values.items() if v}
        if not tagged:
            if raw["notes"]:
                report["problems"].append(f"{where}: a note but no tags, so it is not on the list")
            else:
                report["untouched"] += 1
            continue
        if values.get("relationship") == "do_not_contact" and values.get("priority"):
            report["problems"].append(f"{where}: N with a priority")
            continue
        url = entry["url"] if entry else raw["linkedin"]
        person = people.get(canonical(url))
        if values.get("relationship") == "do_not_contact" and not (entry and entry.get("record_id")):
            never.append(canonical(url) or raw["name"].casefold())
            report["never_off_site"] += 1
            continue
        if not entry and not raw["name"]:
            report["problems"].append(f"{where}: an added row needs a name")
            continue
        row = dict(n=entry["n"] if entry else None, line=line_no, name=(person or {}).get("name") or raw["name"],
                   url=url or "", record_id=(entry or {}).get("record_id"), note=note, former_employer=former_employer,
                   linkedin=_linkedin(person) if person and not (entry or {}).get("record_id") else None, **values)
        rows.append(row)
        report["imported"] += 1
        for field, name in (("relationship", "how_well"), ("priority", "priority"), ("kind", "who"),
                            ("preferred_channel", "channel"), ("preferred_ask", "ask")):
            if values.get(field):
                report["by"][name][values[field]] = report["by"][name].get(values[field], 0) + 1
    doc = dict(source=f"The owner's list ({sheet.name})", sheet=sheet.name, export=snapshot["export"].get("folder", ""),
               made=dt.date.today().isoformat(), rows=rows, never=never)
    return doc, report


def _linkedin(p: dict) -> dict:
    """The snapshot fields a review card needs: relationship metadata only, never message text."""
    threads = {k: v for k, v in (p["evidence"].get("threads") or {}).items() if k != "conversation_ids"}
    evidence = {k: p["evidence"].get(k) for k in ("endorsed_you", "you_endorsed", "recommendations", "invitation")}
    return dict({k: p.get(k) or "" for k in ("first_name", "last_name", "name", "url", "email", "company", "position",
                                              "connected_on", "connected_date")},
                flags=list(p.get("flags") or []), evidence=dict(evidence, threads=threads))


def remember_never(never: list[str], path: Path = NEVER) -> int:
    """People the owner marked N who are not on the site: kept locally so later sheets leave them out."""
    if not never:
        return 0
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else dict(
        purpose="People the owner marked never to contact who are not on the site. Never committed (#30).", people=[])
    added = [x for x in never if x not in known["people"]]
    known["people"] += added
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(known, indent=1, ensure_ascii=False))
    return len(added)


# --- the import ---------------------------------------------------------------------------------

class _Undo(Exception):
    pass


FIELDS = {"relationship": "relationship", "kind": "kind", **{k: k for k in records.PERSON_CHOICES}}
ALLOWED = {"n", "line", "name", "url", "record_id", "note", "former_employer", "linkedin", "merge_into", "as_new", *FIELDS}


def _validate(doc: dict) -> list[dict]:
    rows = doc.get("rows")
    if not isinstance(rows, list):
        raise ValueError("An import file needs a list of rows")
    problems = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - ALLOWED:
            problems.append(f"row {i}: unexpected fields")
            continue
        for field in FIELDS:
            try:
                states.validate("person", field, row.get(field))
            except ValueError as error:
                problems.append(f"row {i}: {error}")
        li = row.get("linkedin")
        if li is not None and (not isinstance(li, dict) or not li.get("url") or "messages" in li
                               or "conversation_ids" in (li.get("evidence") or {}).get("threads", {})):
            problems.append(f"row {i}: the LinkedIn block must be metadata from the snapshot")
    if problems:
        raise ValueError("Nothing was imported: " + "; ".join(problems))
    return rows


def _where(row: dict, i: int) -> str:
    return f"row {row['n']}" if row.get("n") else f"added row {row.get('line') or i}"


def _person(conn, row: dict, export: str, actor: str, report: dict) -> dict:
    if row.get("record_id"):
        found = conn.execute("SELECT id,type,url,archived_at FROM records WHERE id=?", (row["record_id"],)).fetchone()
        if not found or found["type"] != "person":
            raise ValueError("the site has no such person")
        if found["archived_at"]:
            raise ValueError("this person was archived or merged on the site")
        if row.get("url") and found["url"] and canonical(found["url"]) != canonical(row["url"]):
            raise ValueError("the person on the site has a different LinkedIn profile")
        report["matched"] += 1
        return records.get(conn, found["id"])
    raw_url = row.get("url") or ""
    try:
        url = canonical(records.canonical_profile(raw_url)) if raw_url else ""
    except ValueError:
        url = canonical(raw_url)
    if url:
        found = [r["id"] for r in conn.execute("SELECT id,url FROM records WHERE type='person' AND archived_at IS NULL")
                 if canonical(r["url"]) == url]
        if found:
            report["matched"] += 1
            return records.get(conn, found[0])
    if row.get("linkedin"):
        p = row["linkedin"]
        card, extra = linkedin_export.card(conn, dict(p, evidence=dict(p["evidence"])), ["owners_list"], export)
        qid, _ = records.linkedin_card(conn, card, source="LinkedIn export " + export, extra=extra)
        q = conn.execute("SELECT * FROM review_queue WHERE id=?", (qid,)).fetchone()
        if q["resolved_at"]:
            outcome, _, rid = (q["resolution"] or "").partition(":")
            if outcome == "reject":
                raise ValueError("turned down in an earlier review; add them only on the owner's word")
            if not rid or not conn.execute("SELECT 1 FROM records WHERE id=? AND archived_at IS NULL", (rid,)).fetchone():
                raise ValueError("an earlier review of this person left no active record")
            report["matched"] += 1
            return records.get(conn, rid)
        payload = dict(resolution="accept", revision=q["revision"], ensure_organisation=True)
        if row.get("merge_into"):
            payload.update(resolution="merge", target_id=row["merge_into"])
        elif row.get("as_new"):
            payload["accept_as_new"] = True
        result = records.resolve_review(conn, qid, payload, actor=actor)
        report["merged" if row.get("merge_into") else "accepted"] += 1
        return result["record"]
    if row.get("merge_into"):
        report["matched"] += 1
        return records.get(conn, row["merge_into"])
    if records.person_matches(conn, dict(name=row.get("name") or "")) and not row.get("as_new"):
        raise ValueError("someone with this name is already on the site: merge into them, or add as new")
    report["created"] += 1
    return records.create(conn, dict(type="person", name=row.get("name") or "", url=raw_url), actor=actor)["record"]


def _apply(conn, row: dict, export: str, label: str, actor: str, report: dict) -> None:
    person = _person(conn, row, export, actor, report)
    rid, note = person["id"], f"The owner's list, {label}"
    changed = False
    for field in ("relationship", "kind"):
        value = row.get(field)
        if value and person.get(field) != value:
            person = records.transition(conn, rid, dict(field=field, to=value, note=note, revision=person["revision"]),
                                        actor=actor)["record"]
            changed = True
    values = {k: row[k] for k in records.PERSON_CHOICES if row.get(k)}
    if "priority" in row and not row.get("priority"):
        values["priority"] = None  # the sheet had a Priority column and he left it blank: not on the list now
    if row.get("relationship") == "do_not_contact":
        values["priority"] = None
    context, lines = person.get("known_context") or "", []
    if row.get("note"):
        lines.append(f"Your note ({label}): {row['note']}")
    if row.get("former_employer"):
        lines.append(profile_rules.current().warning)
    new = [x for x in lines if x not in context]
    if new:
        values["known_context"] = "\n".join(filter(None, [context, *new]))
    values = {k: v for k, v in values.items() if person.get(k) != v}
    if values:
        person = records.edit(conn, rid, dict(values, revision=person["revision"]), actor=actor)["record"]
        changed = True
    report["changed" if changed else "unchanged"] += 1
    if person.get("priority"):
        report["listed"] += 1
    if person.get("relationship") == "do_not_contact":
        report["never"] += 1


def import_list(conn, doc: dict, actor: str = "agent", dry_run: bool = False) -> dict:
    """Apply the owner's list in one transaction: all of it, or nothing. Problems are collected for every
    row first, so one run reports them all."""
    rows = _validate(doc)
    export = doc.get("export") or SNAPSHOT.stem
    made = doc.get("made") or dt.date.today().isoformat()
    label = f"{dt.date.fromisoformat(made[:10]):%-d %b %Y}"
    report = dict(rows=len(rows), matched=0, accepted=0, merged=0, created=0, changed=0, unchanged=0, listed=0,
                  never=0, problems=[], saved=False, dry_run=dry_run)
    try:
        with records.transaction(conn):
            for i, row in enumerate(rows, 1):
                try:
                    with records.transaction(conn):
                        _apply(conn, row, export, label, actor, report)
                except (ValueError, records.Conflict) as error:
                    report["problems"].append(f"{_where(row, i)}: {error}")
            if report["problems"] or dry_run:
                raise _Undo()
            conn.execute("INSERT INTO import_runs (at, command, source, report_json) VALUES (?,?,?,?)",
                         (records.db.now(), "import-list", doc.get("source") or "The owner's list", json.dumps(report, ensure_ascii=False)))
    except _Undo:
        return report
    report["saved"] = True
    return report


# --- command line -------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sheet", help="write the sheet and its row key from a database copy; prints counts only")
    s.add_argument("--db", type=Path, required=True, help="a pulled backup (ops/pull-backup.sh) or the development copy")
    s.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    s.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("check", help="turn a returned sheet into an import file; prints counts and row numbers")
    c.add_argument("sheet", type=Path)
    c.add_argument("--key", type=Path, required=True)
    c.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--never", type=Path, default=NEVER)
    a = ap.parse_args(argv)
    snapshot = json.loads(a.snapshot.read_text(encoding="utf-8"))
    if a.command == "sheet":
        conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        site = _read_site(conn)
        never = set(json.loads(NEVER.read_text(encoding="utf-8"))["people"]) if NEVER.exists() else set()
        snapshot = dict(snapshot, people={k: p for k, p in snapshot["people"].items() if canonical(p["url"]) not in never})
        rows, key = sheet_rows(snapshot, site)
        write_sheet(rows, key, a.out, snapshot["export"].get("folder", ""))
        groups = {}
        for r in rows:
            groups[r["Group"]] = groups.get(r["Group"], 0) + 1
        print(json.dumps(dict(sheet=str(a.out), rows=len(rows), groups=groups), indent=1))
        return 0
    doc, report = check(a.sheet, a.key, snapshot)
    if report["stopped"]:
        print(json.dumps(report, indent=1))
        return 1
    report["never_remembered"] = remember_never(doc.pop("never", []), a.never)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    print(json.dumps(dict(report, out=str(a.out)), indent=1))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
