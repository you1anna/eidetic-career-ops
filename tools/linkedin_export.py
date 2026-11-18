#!/usr/bin/env python3
"""LinkedIn full-export reader and importer (archive/plans/2026-09-22-linkedin-export.md, use U1).

`snapshot` turns one export folder into a deterministic JSON document of relationship evidence:
connections, thread metadata, endorsements, recommendations and invitations. Message bodies and
subjects are dropped as each row is read, and files outside `READS` are never opened. `import_snapshot`
queues review cards through the same `records.queue_linkedin_person` as the Connections-CSV upload, so
a repeated or later export folds into the same records.

  linkedin_export.py snapshot data/raw-imports/linkedin/2026-09-21            # counts only
  wo import-linkedin-export data/imports/linkedin/2026-09-21.json [--tranche first|all] [--company NAME]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workspace"))
import normalise  # noqa: E402
import profile_rules  # noqa: E402
import records  # noqa: E402

READS = ("Connections.csv", "messages.csv", "Endorsement_Received_Info.csv", "Endorsement_Given_Info.csv",
         "Recommendations_Received.csv", "Recommendations_Given.csv", "Invitations.csv")
MESSAGE_FIELDS = ("CONVERSATION ID", "FROM", "SENDER PROFILE URL", "TO", "RECIPIENT PROFILE URLS", "DATE", "FOLDER",
                  "IS MESSAGE DRAFT")
TWO_WAY_SINCE = "2024-01-01"


def _profile(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    try:
        return records.canonical_profile(url)
    except ValueError:
        return ""


def _rows(path: Path, header_starts: str | None = None):
    text = path.read_text(encoding="utf-8-sig")
    if header_starts:
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith(header_starts)), None)
        if start is None:
            raise ValueError(f"{path.name}: no header row starting {header_starts!r}")
        text = "\n".join(lines[start:])
    return csv.DictReader(io.StringIO(text))


def _date(value: str, *formats: str) -> str:
    value = (value or "").strip()
    for f in formats:
        try:
            return dt.datetime.strptime(value, f).date().isoformat()
        except ValueError:
            continue
    return ""


def snapshot(folder: Path, owner_profile: str) -> dict:
    folder = Path(folder)
    owner = _profile(owner_profile)
    if not owner:
        raise ValueError("The owner's LinkedIn profile URL is needed to tell sent from received messages")
    counts = {}
    rules = profile_rules.current()
    people, blank = {}, []
    for line, row in enumerate(_rows(folder / "Connections.csv", "First Name,"), 2):
        name = " ".join(filter(None, [row.get("First Name", "").strip(), row.get("Last Name", "").strip()]))
        url = _profile(row.get("URL", ""))
        if not name or not url:
            blank.append(line)
            continue
        company = row.get("Company", "").strip()
        people[url] = {
            "name": name, "first_name": row.get("First Name", "").strip(), "last_name": row.get("Last Name", "").strip(),
            "url": url, "email": row.get("Email Address", "").strip().lower(),
            "company": company, "position": row.get("Position", "").strip(),
            "connected_on": row.get("Connected On", "").strip(),
            "connected_date": _date(row.get("Connected On", ""), "%d %b %Y"),
            "flags": [rules.flag] if rules.flag and rules.at_employer(company) else [],
        }
    counts["connections"] = len(people) + len(blank)
    counts["connections_with_name_and_url"] = len(people)
    counts["connections_blank"] = len(blank)
    counts["emails"] = sum(1 for p in people.values() if p["email"])

    conversations, names, message_total, skipped = {}, {}, 0, {"draft": 0, "spam": 0}
    for row in _rows(folder / "messages.csv"):
        meta = {k: row.get(k, "") for k in MESSAGE_FIELDS}
        has_subject = bool((row.get("SUBJECT") or "").strip())
        row.clear()  # bodies, subjects and attachments go no further than this line
        message_total += 1
        cid = meta["CONVERSATION ID"]
        sender = _profile(meta["SENDER PROFILE URL"])
        recipients = [_profile(u) for u in re.split(r"[,\s]+", meta["RECIPIENT PROFILE URLS"]) if u]
        conv = conversations.setdefault(cid, {"participants": set(), "sent": 0, "received": 0, "dates": [], "sent_dates": [],
                                              "subject": False, "evidence": True})
        conv["participants"].update(u for u in [sender, *recipients] if u and u != owner)
        conv["subject"] |= has_subject
        date = _date(meta["DATE"], "%Y-%m-%d %H:%M:%S UTC")
        if meta["IS MESSAGE DRAFT"].strip().lower() == "yes":
            skipped["draft"] += 1
            continue
        if meta["FOLDER"].strip().upper() == "SPAM":
            skipped["spam"] += 1
            conv["evidence"] = False
        if sender == owner:
            conv["sent"] += 1
            conv["sent_dates"].append(date)
        else:
            conv["received"] += 1
            if sender:
                names.setdefault(sender, meta["FROM"].strip())
        conv["dates"].append(date)
    counts["messages"] = message_total
    counts["conversations"] = len(conversations)
    counts["two_way"] = sum(1 for c in conversations.values() if c["sent"] and c["received"])
    counts["received_only"] = sum(1 for c in conversations.values() if c["received"] and not c["sent"])
    counts["sent_only"] = sum(1 for c in conversations.values() if c["sent"] and not c["received"])
    counts["messages_sent"] = sum(c["sent"] for c in conversations.values())
    counts["messages_received"] = sum(c["received"] for c in conversations.values())
    counts["skipped_messages"] = skipped

    threads = {}
    for cid, c in conversations.items():
        if not c["evidence"] or len(c["participants"]) != 1 or not (c["sent"] or c["received"]):
            continue  # spam, group and draft-only threads are not evidence about one person
        (who,) = c["participants"]
        t = threads.setdefault(who, {"count": 0, "two_way": 0, "sent": 0, "received": 0, "first": "", "last": "",
                                     "last_sent": "", "last_two_way": "", "subject": False, "conversation_ids": []})
        dates = [d for d in c["dates"] if d]
        t["count"] += 1
        t["sent"] += c["sent"]
        t["received"] += c["received"]
        t["subject"] |= c["subject"]
        t["conversation_ids"].append(cid)
        if dates:
            t["first"] = min(filter(None, [t["first"], min(dates)]))
            t["last"] = max(t["last"], max(dates))
        if c["sent"] and c["received"]:
            t["two_way"] += 1
            t["last_two_way"] = max(t["last_two_way"], max(dates, default=""))
        if c["sent"]:
            t["last_sent"] = max(t["last_sent"], max(dates, default=""))
    for t in threads.values():
        t["conversation_ids"].sort()

    endorsed_you, you_endorsed = {}, {}
    for row in _rows(folder / "Endorsement_Received_Info.csv"):
        u = _profile(row.get("Endorser Public Url", ""))
        if u and row.get("Endorsement Status", "").upper() != "REJECTED":
            endorsed_you[u] = endorsed_you.get(u, 0) + 1
    for row in _rows(folder / "Endorsement_Given_Info.csv"):
        u = _profile(row.get("Endorsee Public Url", ""))
        if u:
            you_endorsed[u] = you_endorsed.get(u, 0) + 1
    by_name = {}
    for p in people.values():
        by_name.setdefault(p["name"].casefold(), []).append(p["url"])
    recommendations = {}
    for direction, file in (("received", "Recommendations_Received.csv"), ("given", "Recommendations_Given.csv")):
        for row in _rows(folder / file):
            name = " ".join(filter(None, [row.get("First Name", "").strip(), row.get("Last Name", "").strip()])).casefold()
            if len(by_name.get(name, [])) == 1:  # the file has no profile URL, so only an unambiguous name counts
                recommendations.setdefault(by_name[name][0], []).append(direction)
    invitations = {}
    for row in _rows(folder / "Invitations.csv"):
        outgoing = row.get("Direction", "").upper() == "OUTGOING"
        u = _profile(row.get("inviteeProfileUrl" if outgoing else "inviterProfileUrl", ""))
        if u:
            invitations[u] = {"direction": "outgoing" if outgoing else "incoming",
                              "date": _date(row.get("Sent At", ""), "%m/%d/%y, %I:%M %p")}

    for url, p in people.items():
        p["evidence"] = {
            "threads": {k: v for k, v in threads.get(url, {}).items()},
            "endorsed_you": endorsed_you.get(url, 0), "you_endorsed": you_endorsed.get(url, 0),
            "recommendations": sorted(set(recommendations.get(url, []))), "invitation": invitations.get(url),
        }
    counterparts = {u: {"url": u, "name": names.get(u, ""), "threads": t} for u, t in threads.items() if u not in people}
    excluded = sorted(p.name for p in folder.rglob("*") if p.is_file() and p.name not in READS)
    counts["former_employer"] = sum(1 for p in people.values() if p["flags"])
    counts["counterparts_not_connected"] = len(counterparts)
    return {
        "export": {"folder": folder.name, "reads": list(READS), "excluded_unread": excluded, "counts": counts,
                   "blank_connection_lines": blank},
        "people": dict(sorted(people.items())),
        "counterparts": dict(sorted(counterparts.items())),
    }


def write(snap: dict, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return out


# --- import -------------------------------------------------------------------------------------

def criteria(conn, p: dict) -> list[str]:
    """Why a connection belongs in the first tranche. Observable evidence only."""
    t = p["evidence"]["threads"]
    why = []
    if t.get("two_way") and t.get("last_two_way", "") >= TWO_WAY_SINCE:
        why.append("two_way_since_2024")
    if records.match_organisation(conn, p["company"]):
        why.append("existing_organisation")
    if p["evidence"]["endorsed_you"] or p["evidence"]["you_endorsed"] or p["evidence"]["recommendations"]:
        why.append("endorsement_or_recommendation")
    return why


def _strength(p: dict, why: list[str]) -> tuple:
    t = p["evidence"]["threads"]
    latest = int((t.get("last_two_way") or t.get("last") or "0").replace("-", "") or 0)
    return (-("two_way_since_2024" in why), -len(why), -latest, -(t.get("two_way") or 0), p["name"].casefold())


def card(conn, p: dict, why: list[str], export: str) -> tuple[dict, dict]:
    """One snapshot person as a review-card row and its evidence block: relationship metadata only,
    never message text. The owner's list (tools/network_list.py) queues through this too."""
    org = None if normalise.placeholder_employer(p["company"]) else records.match_organisation(conn, p["company"])
    evidence = {k: v for k, v in p["evidence"]["threads"].items() if k != "conversation_ids"}
    extra = {"evidence": {**{k: v for k, v in p["evidence"].items() if k != "threads"}, "threads": evidence,
                          "last_sent": evidence.get("last_sent", ""), "export": export, "criteria": why},
             "flags": p["flags"], "connected_date": p["connected_date"]}
    if org:
        extra["org_match"] = {"id": org["id"], "name": org["name"], "type": org["type"],
                              "relation": "consultant_at" if org["type"] == "agency" else "works_at",
                              "note": f"LinkedIn export {export}; unverified"}
    if p["flags"]:
        extra["warning"] = profile_rules.current().warning
    row = {"First Name": p["first_name"], "Last Name": p["last_name"], "URL": p["url"], "Email Address": p["email"],
           "Company": p["company"], "Position": p["position"], "Connected On": p["connected_on"]}
    return row, extra


def import_snapshot(conn, snap: dict, tranche: str = "first", company: str = "") -> dict:
    """Queue review cards for the chosen people, strongest evidence first; refresh evidence on people
    already accepted. Nobody outside the selection is written."""
    export = snap["export"]["folder"]
    source = f"linkedin-export:{export}"
    report = {"export": export, "tranche": tranche, "queued": 0, "unchanged": 0, "refreshed": 0,
              "last_touch_advanced": 0, "outside_selection": 0, "former_employer_flagged": 0, "criteria": {}, "invalid": []}
    chosen = []
    for p in snap["people"].values():
        why = criteria(conn, p)
        person = conn.execute("SELECT id FROM records WHERE type='person' AND archived_at IS NULL AND rtrim(url,'/')=?",
                              (p["url"],)).fetchone()
        selected = (tranche == "all" or (tranche == "first" and why) or
                    (company and normalise.org_key(p["company"]) == normalise.org_key(company)))
        if not selected and not person:
            report["outside_selection"] += 1
            continue
        chosen.append((p, why, person))
    chosen.sort(key=lambda x: _strength(x[0], x[1]))
    with records.transaction(conn):
        for p, why, person in chosen:
            for w in why:
                report["criteria"][w] = report["criteria"].get(w, 0) + 1
            row, extra = card(conn, p, why, export)
            evidence = extra["evidence"]["threads"]
            if p["flags"]:
                report["former_employer_flagged"] += 1
            try:
                queued = records.queue_linkedin_person(conn, row, source="LinkedIn export " + export, extra=extra)
            except ValueError as error:
                report["invalid"].append({"reason": str(error)})  # no profile URL: import_runs is committed
                continue
            report["queued" if queued else "unchanged"] += 1
            if person:
                payload = json.dumps({"url": p["url"], "company": p["company"], "position": p["position"],
                                      "evidence": extra["evidence"], "flags": p["flags"]}, sort_keys=True, ensure_ascii=False)
                cur = records.add_observation(conn, person["id"], source, export, payload)
                report["refreshed"] += cur.rowcount
                if records.advance_last_touch(conn, person["id"], evidence.get("last_sent", ""),
                                              f"Last LinkedIn conversation in which the owner sent a message ({export})"):
                    report["last_touch_advanced"] += 1
    conn.execute("INSERT INTO import_runs (at, command, source, report_json) VALUES (?,?,?,?)",
                 (records.db.now(), "import-linkedin-export", export, json.dumps(report, ensure_ascii=False)))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="write the evidence snapshot for one export folder; prints counts only")
    snap.add_argument("folder", type=Path)
    snap.add_argument("--out", type=Path)
    snap.add_argument("--profile", type=Path, default=ROOT / "config/profile.yml")
    a = ap.parse_args(argv)
    import yaml
    owner = (yaml.safe_load(a.profile.read_text()) or {}).get("candidate", {}).get("linkedin", "")
    result = snapshot(a.folder, owner)
    out = write(result, a.out or ROOT / "data/imports/linkedin" / f"{a.folder.name}.json")
    print(json.dumps({"snapshot": str(out), **result["export"]["counts"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
