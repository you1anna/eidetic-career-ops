#!/usr/bin/env python3
"""Former colleagues from mail headers (10 November 2026).

LinkedIn's export has no work history for a connection, so someone the owner worked with but never
messaged on LinkedIn scored almost nothing on his sheet and never reached the site. This reads saved
Gmail header pages (the connector's metadata view: senders, recipients, dates and display names; no
subjects or bodies) for the earlier employers in `config/profile.yml` `employers`, and matches each
address at an employer's domain to one LinkedIn connection by name. Only a unique match counts.

  mail_colleagues.py evidence <pages dir>... --held <ids.json> --out <dir>/colleagues.json   # counts only
  mail_colleagues.py list <dir>/colleagues.json --db <pulled backup> --out <dir>/<date>-colleagues.json
  ops/wo-live import-list <dir>/<date>-colleagues.json --dry-run                   # then without
  mail_colleagues.py contacts <dir>/colleagues.json [--phones <file>] [--db <copy>] [--apply]

People reach the site only through the owner's list import (`network_list.import_list`); contact
details and direct emails go through the ordinary `edit` and `interaction` commands. An address at
an employer the person has left is kept as labelled context, never as their email route. Files that
name people stay in data/research/ and are never committed (#30). Commands print counts, not names.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workspace"))
sys.path.insert(0, str(ROOT / "tools"))
import network_list  # noqa: E402
import normalise  # noqa: E402
import profile_rules  # noqa: E402

TITLES = {"dr", "mr", "mrs", "ms", "miss", "prof", "sir"}
# Familiar forms of first names (an address may say Phil where LinkedIn says Philip). A short form
# matches each of its full forms, but two full forms never match each other (Rajiv is not Rajesh).
SHORT = {"phil": ("philip", "phillip"), "yiannis": ("ioannis",), "yannis": ("ioannis",), "perry": ("peregrine",),
         "raj": ("rajiv", "rajesh", "rajendra"), "jag": ("jagjit", "jagdeep", "jagdish"), "andy": ("andrew",),
         "drew": ("andrew",), "dan": ("daniel",), "danny": ("daniel",), "mike": ("michael",), "mick": ("michael",),
         "tom": ("thomas",), "rob": ("robert",), "robbie": ("robert",), "bob": ("robert",), "will": ("william",),
         "bill": ("william",), "jim": ("james",), "jamie": ("james",), "chris": ("christopher", "christian", "christine"),
         "matt": ("matthew",), "nick": ("nicholas",), "steve": ("stephen", "steven"), "sam": ("samuel", "samantha"),
         "ben": ("benjamin",), "alex": ("alexander", "alexandra"), "ed": ("edward", "edmund"), "eddie": ("edward",),
         "joe": ("joseph",), "tony": ("anthony",), "dave": ("david",), "pete": ("peter",),
         "kate": ("katherine", "catherine", "kathryn"), "katie": ("katherine", "catherine"), "liz": ("elizabeth",),
         "beth": ("elizabeth",), "jen": ("jennifer",), "jenny": ("jennifer",), "tim": ("timothy",), "greg": ("gregory",),
         "jon": ("jonathan",), "jonny": ("jonathan",), "vicky": ("victoria",), "becky": ("rebecca",),
         "mo": ("mohamed", "mohammed", "muhammad"), "abbie": ("abigail",), "abby": ("abigail",), "ollie": ("oliver",),
         "harry": ("henry", "harold"), "charlie": ("charles", "charlotte"), "fred": ("frederick",), "freddie": ("frederick",),
         "sue": ("susan",), "pat": ("patrick", "patricia"), "rich": ("richard",), "rick": ("richard",)}
FULL = {}
for _short, _fulls in SHORT.items():
    for _full in _fulls:
        FULL.setdefault(_full, set()).add(_short)
SYSTEM = re.compile(r"no-?reply|notif|notify|mailer-daemon|^support@|^hello@", re.I)
LIVE = ROOT / "ops/wo-live"


# --- names ---------------------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().casefold()
    words = [re.sub(r"[^a-z]", "", w) for w in re.split(r"[\s_.]+", plain)]
    return [w for w in words if w]


def person_key(first: str, last: str) -> str:
    """First name and surname, folded: "Dr Andie" + "Carver-Lowe, MBA" → andiecarverlowe."""
    given = [w for w in _tokens(first) if w not in TITLES]
    family = _tokens((last or "").split(",")[0])
    return given[0] + family[-1] if given and family else ""


def person_keys(first: str, last: str) -> set[str]:
    """Every key a connection answers to: the first name's familiar or full forms, and the whole surname
    or either part of a double-barrelled one (Andie Carver became Andie Carver-Lowe)."""
    given = [w for w in _tokens(first) if w not in TITLES]
    surname = (last or "").split(",")[0]
    family = _tokens(surname)
    if not given or not family:
        return set()
    names = {given[0], *SHORT.get(given[0], ()), *FULL.get(given[0], ())}
    surnames = {family[-1], *(t[-1] for t in (_tokens(part) for part in surname.split("-")) if t)}
    return {n + f for n in names for f in surnames}


def address_keys(address: str, display: str = "") -> set[str]:
    """The name keys an address can stand for: its display name, and a first.last local part.
    A one-word local part ("ian@") gives no key: it could be anyone called Ian."""
    keys = set()
    name = (display or "").strip().strip("'\"")
    if name and "@" not in name:
        if "," in name:
            last, _, first = name.partition(",")
            name = f"{first} {last}"
        words = name.split()
        if len(words) >= 2:
            keys.add(person_key(words[0], words[-1]) if words[0].casefold().strip(".") not in TITLES
                     else person_key(" ".join(words[1:-1]) or words[-1], words[-1]))
    local = re.sub(r"^\d+", "", address.partition("@")[0])
    parts = [p for p in re.split(r"[._-]+", local) if p]
    if len(parts) >= 2:
        keys.add(person_key(parts[0], parts[-1]))
    keys.discard("")
    return keys


# --- reading the saved pages ---------------------------------------------------------------------

def _when(m: dict) -> str:
    """The message time: the connector's `date`, or `internalDate` (milliseconds) in older saved pages."""
    if m.get("date"):
        return m["date"]
    try:
        return dt.datetime.fromtimestamp(int(m["internalDate"]) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, ValueError):
        return ""


def read_pages(*folders: Path) -> list[dict]:
    """Every message in the saved pages, once, with its thread link and time. Copies of one message
    from different pages are merged: a page may hold only some of its recipients."""
    by_id = {}
    for path in sorted(p for folder in folders for p in folder.glob("*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        for thread in page.get("threads") or []:
            for m in thread.get("messages") or []:
                m = dict(m, date=_when(m), thread_url=thread.get("viewUrl") or "")
                kept = by_id.setdefault(m.get("id"), m)
                if kept is m:
                    continue
                for field in ("toRecipients", "ccRecipients"):
                    have = {a.casefold() for a in kept.get(field) or []}
                    kept[field] = list(kept.get(field) or []) + [a for a in m.get(field) or [] if a.casefold() not in have]
                named = {(x.get("emailAddress") or "").casefold() for x in kept.get("addressDisplayNames") or []}
                kept["addressDisplayNames"] = list(kept.get("addressDisplayNames") or []) + [
                    x for x in m.get("addressDisplayNames") or [] if (x.get("emailAddress") or "").casefold() not in named]
                kept["thread_url"] = kept["thread_url"] or m["thread_url"]
    return list(by_id.values())


def message_link(m: dict) -> str:
    """The Gmail link to one message: the thread link with the message's decimal id."""
    url = m.get("thread_url") or ""
    try:
        return f"{url}|msg-f:{int(m['id'], 16)}" if url and "|" not in url else url
    except (KeyError, ValueError):
        return url


def addresses(messages: list[dict], rules) -> dict:
    """Facts for every address at a declared employer: who it names, when it was seen, in how many
    threads, whether any message fell in the owner's months there, and direct exchanges with him."""
    own = rules.own_name_terms[-1] if rules.own_name_terms else None
    is_own = (lambda a: own in a.casefold()) if own else (lambda a: False)
    facts = {}
    for m in messages:
        names = {(x.get("emailAddress") or "").casefold(): x.get("displayName") or ""
                 for x in m.get("addressDisplayNames") or []}
        sender = (m.get("sender") or "").casefold()
        recipients = [a.casefold() for a in (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])]
        when = m.get("date") or ""
        for address in {sender, *recipients}:
            employer = next((e for e in rules.employers if e.owns(address)), None)
            if not employer or not address or is_own(address):
                continue
            f = facts.setdefault(address, dict(address=address, employer=employer.name, names=set(), first="", last="",
                                               threads=set(), during=False, direct=[]))
            if names.get(address):
                f["names"].add(names[address])
            f["first"] = min(filter(None, [f["first"], when]), default="")
            f["last"] = max(f["last"], when)
            f["threads"].add(m.get("threadId"))
            f["during"] = f["during"] or employer.during(when)
            others = [a for a in recipients if a != address]
            if sender == address and recipients and all(is_own(a) for a in recipients):
                f["direct"].append(dict(direction="in", at=when, link=message_link(m)))
            elif is_own(sender) and recipients and all(a == address or is_own(a) for a in recipients) \
                    and address in recipients and not [a for a in others if not is_own(a)]:
                f["direct"].append(dict(direction="out", at=when, link=message_link(m)))
    return facts


# --- matching -------------------------------------------------------------------------------------

def match(facts: dict, snapshot: dict) -> tuple[dict, dict]:
    """Connections matched to employer addresses by a unique name key. Returns (evidence by profile URL,
    report). An address whose keys point at two connections, or at two different people, is left out."""
    index = {}
    for p in snapshot["people"].values():
        for key in person_keys(p.get("first_name") or "", p.get("last_name") or ""):
            index.setdefault(key, set()).add(network_list.canonical(p["url"]))
    people = {network_list.canonical(p["url"]): p for p in snapshot["people"].values()}
    found, report = {}, dict(addresses=len(facts), matched_addresses=0, ambiguous=0, unmatched=0, outside_months=0)
    for address, f in sorted(facts.items()):
        keys = set().union(*(address_keys(address, n) for n in f["names"] or {""}))
        urls = set().union(*(index.get(k, set()) for k in keys)) if keys else set()
        if len(urls) != 1:
            report["ambiguous" if urls else "unmatched"] += 1
            continue
        if not f["during"]:
            report["outside_months"] += 1
            continue
        url = urls.pop()
        report["matched_addresses"] += 1
        p = people[url]
        entry = found.setdefault(url, dict(url=p["url"], name=p["name"], employers={}))
        e = entry["employers"].setdefault(f["employer"], dict(addresses=[], first="", last="", threads=0, direct=[]))
        e["addresses"].append(address)
        e["first"] = min(filter(None, [e["first"], f["first"]]), default="")
        e["last"] = max(e["last"], f["last"])
        e["threads"] += len(f["threads"])
        e["direct"] += f["direct"]
    for entry in found.values():
        for e in entry["employers"].values():
            e["addresses"].sort()
            e["direct"].sort(key=lambda d: (d["at"], d["direction"]))
    report["people"] = len(found)
    return found, report


def personal_addresses(messages: list[dict], found: dict, snapshot: dict, rules) -> None:
    """Addresses outside every declared employer that a matched colleague sent mail from, newest first.
    Only the sender line counts, and only when its display name is that colleague's; notification
    senders are ignored. `direct` means the message went to the owner alone."""
    own = rules.own_name_terms[-1] if rules.own_name_terms else None
    people = {network_list.canonical(p["url"]): p for p in snapshot["people"].values()}
    keys = {}
    for url in found:
        p = people[url]
        keys.setdefault(person_key(p.get("first_name") or "", p.get("last_name") or ""), []).append(url)
    for entry in found.values():
        entry["personal"] = []
    seen = {}
    for m in messages:
        sender = (m.get("sender") or "").casefold()
        name = next((x.get("displayName") or "" for x in m.get("addressDisplayNames") or []
                     if (x.get("emailAddress") or "").casefold() == sender), "")
        urls = keys.get(next(iter(address_keys("", name)), ""), [])
        if (len(urls) != 1 or not sender or SYSTEM.search(sender) or (own and own in sender)
                or any(e.owns(sender) for e in rules.employers)):
            continue
        recipients = [a.casefold() for a in (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])]
        direct = bool(recipients) and bool(own) and all(own in a for a in recipients)
        x = seen.setdefault((urls[0], sender), dict(address=sender, last="", direct=False))
        if x["last"] == "":
            found[urls[0]]["personal"].append(x)
        x["last"] = max(x["last"], m.get("date") or "")
        x["direct"] = x["direct"] or direct
    for entry in found.values():
        entry["personal"].sort(key=lambda x: x["last"], reverse=True)


def evidence(folders, snapshot: dict, rules, held: set = frozenset()) -> dict:
    """`held` are thread ids the legal-term screen held unread: they are not evidence of anything."""
    messages = read_pages(*folders)
    kept = [m for m in messages if m.get("threadId") not in held]
    found, report = match(addresses(kept, rules), snapshot)
    report["held_messages"] = len(messages) - len(kept)
    messages = kept
    personal_addresses(messages, found, snapshot, rules)
    report["personal_addresses"] = sum(len(e["personal"]) for e in found.values())
    return dict(purpose="Former colleagues among LinkedIn connections, from mail headers. Never committed (#30).",
                made=dt.date.today().isoformat(), export=snapshot["export"].get("folder", ""), report=report,
                people=sorted(found.values(), key=lambda x: x["name"].casefold()))


# --- the list import ------------------------------------------------------------------------------

def _site(conn: sqlite3.Connection) -> tuple[dict, set]:
    people = {network_list.canonical(r["url"]): dict(r) for r in conn.execute(
        "SELECT id,url,kind,relationship FROM records WHERE type='person' AND archived_at IS NULL AND url<>''")}
    rejected = set()
    for payload, resolution in conn.execute("SELECT payload_json,resolution FROM review_queue WHERE kind='linkedin_person'"):
        if (resolution or "").startswith("reject"):
            rejected.add(network_list.canonical(json.loads(payload).get("url")))
    return people, rejected


def list_rows(doc: dict, snapshot: dict, conn: sqlite3.Connection, never: set = frozenset()) -> tuple[dict, dict]:
    """The owner's-list rows that put each matched colleague on the site as a former colleague. A kind he
    already recorded is kept; people at the former employer, turned down or marked never are left out."""
    people = {network_list.canonical(p["url"]): p for p in snapshot["people"].values()}
    site, rejected = _site(conn)
    rows, report = [], dict(colleagues=len(doc["people"]), new=0, on_site=0, kind_set=0, former_employer=0,
                            turned_down=0, never=0, placeholder_employer=0)
    for entry in doc["people"]:
        url = network_list.canonical(entry["url"])
        p, record = people.get(url), site.get(url)
        if not p:
            continue
        if p.get("flags"):
            report["former_employer"] += 1
            continue
        if url in never or (record and record.get("relationship") == "do_not_contact"):
            report["never"] += 1
            continue
        if url in rejected and not record:
            report["turned_down"] += 1
            continue
        row = dict(n=None, line=None, name=p["name"], url=p["url"], record_id=(record or {}).get("id"), note="",
                   former_employer=False, linkedin=None if record else network_list._linkedin(p))
        if row["linkedin"] and normalise.placeholder_employer(row["linkedin"]["company"]):
            row["linkedin"]["company"] = ""  # "Stealth" or "Freelance" names no company to create or link
            report["placeholder_employer"] += 1
        if not (record or {}).get("kind"):
            row["kind"] = "former_colleague"
            report["kind_set"] += 1
        report["on_site" if record else "new"] += 1
        rows.append(row)
    employers = sorted({name for e in doc["people"] for name in e["employers"]})
    return dict(source=f"Former colleagues from your mail ({', '.join(employers)}), {doc['made']}",
                sheet="", export=doc.get("export") or snapshot["export"].get("folder", ""),
                made=dt.date.today().isoformat(), rows=rows), report


# --- contact details and direct emails --------------------------------------------------------------

def _month(when: str) -> str:
    try:
        return f"{dt.date.fromisoformat(when[:10]):%b %Y}"
    except ValueError:
        return when[:7]


def context_lines(entry: dict) -> list[str]:
    """Labelled lines for the person's context: where the mail shows they worked with the owner, each old
    work address and each personal address. An address at an employer they have left is never their
    email route."""
    lines = []
    for name, e in sorted(entry["employers"].items()):
        span = _month(e["first"]) if _month(e["first"]) == _month(e["last"]) else f"{_month(e['first'])}–{_month(e['last'])}"
        direct = len(e["direct"])
        lines.append(f"Former colleague at {name} (your mail): {e['threads']} thread{'s' if e['threads'] != 1 else ''}, {span}"
                     + (f"; {direct} direct email{'s' if direct != 1 else ''}." if direct else "; group mail only."))
        for address in e["addresses"]:
            lines.append(f"Old work address ({name}, former employer, likely inactive; not used as a route): {address}")
    who = (entry["name"].split() or [""])[0]
    for x in entry.get("phones") or []:
        lines.append(f"Phone from {who}'s email signature ({_month(x['at'])}, {x['kind']}; current reachability "
                     f"unconfirmed): {x['phone']}")
    for x in entry.get("personal") or []:
        lines.append(f"Other address from your mail ({'direct email' if x['direct'] else 'group mail'}, "
                     f"{_month(x['last'])}): {x['address']}")
    return lines


def attach_phones(doc: dict, phones: list[dict]) -> None:
    """Numbers read from a colleague's own signature in a direct email (attended; the legal-term screen
    runs first), as {url, phone, kind: mobile|office|unspecified, at, link}. Office lines are at an
    employer they have left, so they are not kept."""
    by = {network_list.canonical(e["url"]): e for e in doc["people"]}
    for x in phones:
        entry = by.get(network_list.canonical(x.get("url")))
        if not entry:
            raise ValueError("a phone names someone who is not a matched colleague")
        if x.get("kind") in ("mobile", "unspecified") and x.get("phone"):
            entry.setdefault("phones", []).append(dict(phone=x["phone"], kind=x["kind"], at=x["at"], link=x.get("link", "")))


def _line_key(line: str) -> str:
    """What a line written by `context_lines` is about: an employer's summary, or one address or number.
    Lines it did not write have no key and are never replaced."""
    if line.startswith("Former colleague at ") and "(your mail):" in line:
        return line.split(":", 1)[0]
    for prefix in ("Old work address (", "Other address from your mail (", "Phone from "):
        if line.startswith(prefix) and ": " in line:
            return prefix + line.rsplit(": ", 1)[1]
    return ""


def contact_steps(entry: dict, record: dict) -> dict:
    """What still needs writing to one person's record: context lines not yet present and direct emails
    not already recorded (by message link, or the same direction on the same day). Running it twice
    changes nothing."""
    context = record.get("known_context") or ""
    lines = context_lines(entry)
    # A rerun may count or date things differently (a held thread, a new page): replace this tool's own
    # line about the same employer, address or number rather than adding a second one.
    ours = {_line_key(line) for line in lines} - {""}
    kept = [line for line in context.splitlines() if not (_line_key(line) in ours and line not in lines)]
    context = "\n".join(kept)
    new = [line for line in lines if line not in context]
    changed = bool(new) or context != (record.get("known_context") or "")
    recorded = {i.get("evidence_ref") for i in record.get("interactions") or []}
    # An earlier pass may have recorded the same email under a thread link: same direction, same day.
    days = {(i.get("direction"), (i.get("at") or "")[:10]) for i in record.get("interactions") or []
            if i.get("channel") == "email"}
    who = (entry["name"].split() or [""])[0]
    interactions = []
    for name, e in sorted(entry["employers"].items()):
        for d in e["direct"]:
            if d["link"] in recorded or (d["direction"], d["at"][:10]) in days:
                continue
            recorded.add(d["link"])
            days.add((d["direction"], d["at"][:10]))
            summary = (f"Direct email from {who} ({name} address); header read only" if d["direction"] == "in"
                       else f"You emailed {who} directly ({name} address); header read only")
            interactions.append(dict(direction=d["direction"], at=d["at"], summary=summary, evidence=d["link"]))
    edit = dict(known_context="\n".join(filter(None, [context, *new]))) if changed else {}
    # The route is the newest personal address used in direct mail; an email already saved stays.
    route = next((x["address"] for x in entry.get("personal") or [] if x["direct"]), "")
    if route and not record.get("email"):
        edit["email"] = route
    mobile = next((x["phone"] for x in entry.get("phones") or [] if x["kind"] == "mobile"), "")
    if mobile and not record.get("phone"):
        edit["phone"] = mobile
    return dict(edit=edit, interactions=interactions)


WO = [str(LIVE)]  # the live gateway; `contacts --db` points it at a local copy for rehearsal


def _wo(*args: str, stdin: str | None = None) -> str:
    return subprocess.run([*WO, *args], input=stdin, capture_output=True, text=True, check=True).stdout


def _live(*args: str, stdin: str | None = None) -> dict:
    return json.loads(_wo(*args, stdin=stdin))


def _live_id(entry: dict) -> str | None:
    """The record for this connection: found by name, confirmed by its LinkedIn URL."""
    for line in _wo("find", entry["name"], "--type", "person").splitlines():
        rid = line.split()[0] if line.strip() and not line.startswith("No records") else ""
        if rid and network_list.canonical(_live("show", rid).get("url")) == network_list.canonical(entry["url"]):
            return rid
    return None


def apply_contacts(doc: dict, apply: bool) -> dict:
    report = dict(people=len(doc["people"]), not_on_site=0, context_added=0, interactions_added=0, unchanged=0)
    for entry in doc["people"]:
        rid = _live_id(entry)
        if not rid:
            report["not_on_site"] += 1
            continue
        record = _live("show", rid)
        steps = contact_steps(entry, record)
        if not steps["edit"] and not steps["interactions"]:
            report["unchanged"] += 1
            continue
        if steps["edit"]:
            report["context_added"] += 1
            if apply:
                _live("edit", rid, "--json", "-", stdin=json.dumps(dict(steps["edit"], revision=record["revision"])))
        for i in steps["interactions"]:
            report["interactions_added"] += 1
            if apply:
                _live("interaction", rid, "--channel", "email", "--direction", i["direction"], "--at", i["at"],
                      "--summary", i["summary"], "--evidence", i["evidence"])
    report["applied"] = apply
    return report


# --- command line --------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    e = sub.add_parser("evidence", help="match saved header pages to connections; prints counts")
    e.add_argument("pages", type=Path, nargs="+", help="folders of saved header pages")
    e.add_argument("--snapshot", type=Path, default=network_list.SNAPSHOT)
    e.add_argument("--out", type=Path, required=True)
    e.add_argument("--held", type=Path, help="JSON list of thread ids held by the legal-term screen")
    li = sub.add_parser("list", help="write a list-import file for the matched colleagues; prints counts")
    li.add_argument("evidence", type=Path)
    li.add_argument("--db", type=Path, required=True, help="a pulled backup (ops/pull-backup.sh)")
    li.add_argument("--snapshot", type=Path, default=network_list.SNAPSHOT)
    li.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("contacts", help="context lines and direct emails through ops/wo-live; dry run unless --apply")
    c.add_argument("evidence", type=Path)
    c.add_argument("--apply", action="store_true")
    c.add_argument("--db", type=Path, help="rehearse on a local copy instead of the live site")
    c.add_argument("--phones", type=Path, help="numbers read from colleagues' own signatures (JSON list)")
    a = ap.parse_args(argv)
    if a.command == "evidence":
        snapshot = json.loads(a.snapshot.read_text(encoding="utf-8"))
        held = set(json.loads(a.held.read_text(encoding="utf-8"))) if a.held else set()
        doc = evidence(a.pages, snapshot, profile_rules.current(), held)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
        print(json.dumps(dict(doc["report"], out=str(a.out)), indent=1))
        return 0
    doc = json.loads(a.evidence.read_text(encoding="utf-8"))
    if a.command == "list":
        snapshot = json.loads(a.snapshot.read_text(encoding="utf-8"))
        conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        never = set(json.loads(network_list.NEVER.read_text(encoding="utf-8"))["people"]) if network_list.NEVER.exists() else set()
        out, report = list_rows(doc, snapshot, conn, never)
        a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False))
        print(json.dumps(dict(report, rows=len(out["rows"]), out=str(a.out)), indent=1))
        return 0
    if a.phones:
        attach_phones(doc, json.loads(a.phones.read_text(encoding="utf-8")))
    if a.db:
        WO[:] = [sys.executable, str(ROOT / "tools/wo.py"), "--db", str(a.db), "--actor", "agent"]
    print(json.dumps(apply_contacts(doc, a.apply), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
