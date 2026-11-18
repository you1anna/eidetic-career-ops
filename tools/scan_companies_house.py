#!/usr/bin/env python3
"""Companies House: the official register's facts about each company to approach (decision #29).

`wo ingest` writes the targets (the companies to approach, each with its company number if one has
been recorded) to `<data>/pending/companies-house-targets.json`; this reader never opens the database.
It reads only companies whose number was recorded on evidence (`wo company-number`, from a page that
states it, such as the company's own terms or footer). A name is never matched automatically: on
30 September a third of exact-name matches were unrelated companies (a "Paddle" boat hire, a "Kraken"
waste firm). Targets without a number are reported, not read. For each number it reads, from the
Companies House Public Data API:

  - the company profile: registered name, number, status, incorporation date and SIC codes;
  - the current officers: name, Companies House role and appointment date only. Never a date of
    birth, address, nationality or residence, and never relabelled (a "director" is not a "CTO");
  - recent capital filings: share allotments (SH01) corroborate a funding story or reveal one.

The facts go to the observation store as one `registry` item per company; `wo import-scans` attaches
them to the record that number is recorded for. Presence-only: a company missing from one run means
nothing. Two aids for the person recording a number, neither of which stores anything:
`--numbers-on URL` lists the company numbers a page states (a company's own terms, privacy notice or
footer) with the register's name and status for each, and `--propose NAME` lists register candidates.

The API takes the key as the HTTP Basic username (developer guide, "API authentication") and allows
600 requests in five minutes ("Rate limiting"); a run stays well inside that and stops at a cap.
"""
from __future__ import annotations

import argparse
import collection_schedule
import collection_budget
import collection_policy
import base64
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import credentials  # noqa: E402
from pending_store import ObservationStore  # noqa: E402
from web_read import failure, fetch_json, request, strip_html  # noqa: E402

API = "https://api.company-information.service.gov.uk"
PUBLIC = "https://find-and-update.company-information.service.gov.uk/company/"
KEY = "COMPANIES_HOUSE_API_KEY"
SCOPE = "companies-house"
ALLOTMENT_DAYS = 400
# "company number 01234567", "registration number: SC123456", "registered in England (no. 01234567)".
STATED_NUMBER = re.compile(r"(?:company|registration|registered|reg\.?)[^0-9A-Za-z]{0,3}(?:in [A-Za-z ,&]{3,40}?)?"
                           r"[^0-9A-Za-z]{0,3}(?:under )?(?:company )?(?:number|no\.?|#)?[^0-9A-Za-z]{0,3}(?:is )?"
                           r"((?:\d{8})|(?:SC|NI|OC)\d{6})\b", re.I)
SUFFIXES = re.compile(r"\b(?:limited|ltd|plc|llp)\b\.?")  # legal forms only: "Acme Group" is not "Acme"


class Stop(RuntimeError):
    """The key was refused or the request cap was reached: end the run, keep what was read."""


def name_key(name: str) -> str:
    """The register's name and a record's name compared without legal suffixes, case or punctuation."""
    s = (name or "").casefold().replace("&", " and ")
    s = SUFFIXES.sub(" ", s)
    return re.sub(r"[^0-9a-z]+", "", s)


class Client:
    def __init__(self, key: str, get=None, pause: float | None = None, cap: int | None = None):
        self.synthetic = get is not None
        self.headers = {"Authorization": "Basic " + base64.b64encode((key + ":").encode()).decode()}
        self.get = get or (lambda url: fetch_json(url, self.headers))
        limits=collection_policy.load()['providers'][SCOPE]
        self.pause, self.cap, self.requests = (pause if get and pause is not None else limits['min_interval_seconds']), (cap if get and cap is not None else limits['daily_limit']), 0

    def __call__(self, path: str, **query):
        if self.requests >= self.cap:
            raise Stop(f"request cap of {self.cap} reached")
        if self.requests and self.pause:
            time.sleep(self.pause)
        self.requests += 1
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        try:
            return self.get(url)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code in (401, 403):
                raise Stop(f"Companies House refused the key (HTTP {error.code})") from error
            raise


def propose(client: Client, name: str) -> list[dict]:
    """Register candidates for a name, for a person to verify against the company's own pages."""
    found = client("/search/companies", q=name, items_per_page=20) or {}
    return [{"company_number": x.get("company_number", ""), "title": x.get("title", ""),
             "status": x.get("company_status", ""), "incorporated_on": x.get("date_of_creation", ""),
             "same_name": name_key(x.get("title", "")) == name_key(name)}
            for x in found.get("items") or []]


def numbers_on(client: Client, url: str, read=None) -> list[dict]:
    """The company numbers a page states, each with the register's name and status, for a person to
    record with `wo company-number`. A page that states no number returns nothing; nothing is guessed."""
    page = (read or (lambda u: request(u, logged=False)))(url)
    text = strip_html(page["body"].decode(page.get("charset") or "utf-8", "replace"))
    out, seen = [], set()
    for m in STATED_NUMBER.finditer(text):
        number = m.group(1).upper()
        if number in seen:
            continue
        seen.add(number)
        profile = client(f"/company/{number}") or {}
        out.append({"company_number": number, "company_name": profile.get("company_name", ""),
                    "status": profile.get("company_status", "not on the register"),
                    "stated_as": " ".join(text[max(0, m.start() - 60):m.end() + 20].split()),
                    "url": page.get("url") or url})
    return out


def officers(client: Client, number: str) -> list[dict]:
    listed = client(f"/company/{number}/officers", items_per_page=100, order_by="appointed_on") or {}
    return [{"name": x.get("name", ""), "role": x.get("officer_role", ""), "appointed_on": x.get("appointed_on", "")}
            for x in listed.get("items") or [] if not x.get("resigned_on")]


def allotments(client: Client, number: str, today: dt.date) -> list[dict]:
    listed = client(f"/company/{number}/filing-history", category="capital", items_per_page=25) or {}
    out = []
    for x in listed.get("items") or []:
        if x.get("description") != "capital-allotment-shares":
            continue
        try:
            filed = dt.date.fromisoformat(x.get("date", ""))
        except ValueError:
            continue
        if (today - filed).days > ALLOTMENT_DAYS:
            continue
        values = x.get("description_values") or {}
        out.append({"filed_on": filed.isoformat(), "allotted_on": values.get("date", ""), "form": x.get("type", ""),
                    "capital": [{"figure": c.get("figure", ""), "currency": c.get("currency", "")}
                                for c in values.get("capital") or []],
                    "url": PUBLIC + number + "/filing-history"})
    return out


def read_company(client: Client, target: dict, today: dt.date) -> dict:
    """One recorded company's facts, or the reason it has none. Raises Stop to end the run."""
    number = target["company_number"]
    profile = client(f"/company/{number}")
    if profile is None:
        return {"record_id": target["id"], "name": target["name"], "unread": f"company {number} not found"}
    return {
        "record_id": target["id"], "name": target["name"],
        "company_name": profile.get("company_name", ""), "company_number": number,
        "company_status": profile.get("company_status", ""), "incorporated_on": profile.get("date_of_creation", ""),
        "sic_codes": list(profile.get("sic_codes") or []),
        "officers": officers(client, number), "allotments": allotments(client, number, today),
        "url": PUBLIC + number,
    }


def run(targets: list[dict], out_dir: Path, client: Client, today: dt.date, observed_at: str | None = None) -> dict:
    observed_at = observed_at or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    found, unread, errors, stopped = [], [], [], ""
    unrecorded = [t["name"] for t in targets if not (t.get("company_number") or "").strip()]
    scheduler = collection_schedule.Session(out_dir,offline=getattr(client,'synthetic',True))
    eligible = [t for t in targets if (t.get('company_number') or '').strip()]
    if not scheduler.offline:
        if scheduler.reason:return scheduler.receipt(SCOPE)
        history=scheduler.b.history(SCOPE,['registry:'+t['company_number'] for t in eligible]);now=scheduler.b.now()
        policy=scheduler.policy['providers'][SCOPE]
        def due(t):
            row=history.get('registry:'+t['company_number'],{})
            last=row.get('last_success')
            chosen=scheduler.context.get('companies',{}).get(t['id'])
            return not last or now-collection_budget.instant(last)>=dt.timedelta(days=policy['refresh_days']) or bool(chosen and chosen>last)
        eligible=[t for t in eligible if due(t)]
        eligible.sort(key=lambda t:(t['id'] not in scheduler.context.get('companies',{}),history.get('registry:'+t['company_number'],{}).get('last_attempt') or ''))
        eligible=eligible[:policy['company_limit']]
        if not eligible:return scheduler.receipt(SCOPE,'not-due')
    for target in eligible:
        try:
            facts = scheduler.call(SCOPE,'registry:'+target['company_number'],lambda:read_company(client, target, today))
        except collection_budget.Deferred as error:
            if not found:return scheduler.receipt(SCOPE,error.reason)
            stopped=error.reason;break
        except Stop as error:
            stopped = str(error)
            break
        except Exception as error:  # one company failing never stops the others
            errors.append({"name": target["name"], **failure(error)})
            continue
        (unread if "unread" in facts else found).append(facts)
    if stopped and not found:
        state = "error"
    elif stopped:
        state = "partial"
    else:
        state = "ok" if found else "empty"
    detail = {"name": "Companies House", "targets": len(targets), "read": len(found), "requests": client.requests,
              "no_number": unrecorded, "unread": [{"name": u["name"], "reason": u["unread"]} for u in unread],
              "errors": len(errors)}
    if stopped:
        detail["error"] = stopped
    if errors:
        detail["first_error"] = {k: errors[0].get(k) for k in ("name", "kind", "error") if errors[0].get(k)}
    ObservationStore(out_dir / "pending" / "observations.json").observe(
        scope=SCOPE, source_type="registry", provider=SCOPE, observed_at=observed_at, state=state,
        complete=not stopped and not errors, absence_authoritative=False, detail=detail,
        items=[{"source_id": f["company_number"], "current": f} for f in found])
    return {"state": state, **detail}


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=root / "data")
    ap.add_argument("--targets", type=Path, help="default: <out>/pending/companies-house-targets.json")
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--propose", metavar="NAME", help="list register candidates for NAME to verify; stores nothing")
    ap.add_argument("--numbers-on", metavar="URL", nargs="+", help="list the company numbers each page states, with "
                    "the register's name and status; stores nothing")
    a = ap.parse_args(argv)
    key = credentials.get(KEY)
    if not key:
        print(f"credential {KEY} is not set", file=sys.stderr)
        return 2
    if a.numbers_on:
        client = Client(key)
        for url in a.numbers_on:
            try:
                found = numbers_on(client, url)
            except Exception as error:
                print(f"{url}: {failure(error)['kind']}")
                continue
            if not found:
                print(f"{url}: no company number stated")
            for c in found:
                print(f"{c['company_number']}  {c['status']:<10} {c['company_name']}  <- {c['url']}\n    “{c['stated_as']}”")
        return 0
    if a.propose:
        for c in propose(Client(key), a.propose):
            print(f"{c['company_number']}  {c['status']:<10} {c['incorporated_on'] or '?':<10}  {c['title']}"
                  + ("  (same name)" if c["same_name"] else ""))
        return 0
    path = a.targets or a.out / "pending" / "companies-house-targets.json"
    targets = json.loads(path.read_text()) if path.exists() else []
    status = run(targets, a.out, Client(key), dt.date.fromisoformat(a.today))
    print(json.dumps(status, indent=2))
    return 0 if status["state"] in ("ok", "empty", "deferred") else 1


if __name__ == "__main__":
    sys.exit(main())
