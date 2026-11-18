#!/usr/bin/env python3
"""Daily scan of UK public contract notices for the career acquisition workflow.

Why this exists alongside ``scan_boards.py``: an employer's ATS board carries that employer's own
headcount, which in the UK is overwhelmingly permanent. Published contract *demand* — the work a
buyer has decided to pay an outside supplier to do — is announced instead through the public
procurement services. Those notices are the direct, outside-IR35 market that the board scan cannot
see, and unlike a job advert a notice states a buyer, a budget and a deadline.

Sources (both publish OCDS, and neither needs an API key or an account):
  contracts_finder  www.contractsfinder.service.gov.uk  — UK public contracts, incl. below-threshold
  find_a_tender     www.find-tender.service.gov.uk      — UK above-threshold notices

Writes, alongside but never into the job scan's files:
  <out>/pending/tenders/YYYY-MM-DD.tsv   notices first surfaced that day, retained across retries
  <out>/pending/tenders/seen.json        every OCID ever surfaced, so a notice is proposed once
  <out>/pending/tenders/last-run.json    per-source status, including partial reads, and totals

A notice is a buying signal, not a qualified opportunity: it says a public body intends to spend,
not that the owner can win it, that the timetable is open, or that a direct supply route exists. Nothing
here is contacted, applied to or promoted to a record automatically. No model calls. No sends.

Usage: scan_tenders.py [--portals PATH] [--out DIR] [--today YYYY-MM-DD]
                       [--offline-fixture DIR] [--probe SOURCE] [--days N]
"""
from __future__ import annotations

import argparse
import collection_schedule
import collection_budget
import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_rules import _word  # noqa: E402
from pending_store import ObservationStore, PendingStore  # noqa: E402
from web_read import USER_AGENT, failure, failure_kind, fetch_json, strip_html  # noqa: E402

COLUMNS = ["date_seen", "source", "source_key", "release_id", "ocid", "buyer", "title", "value_amount", "value_currency",
           "published", "deadline", "cpv", "cpv_label", "status", "url", "why",
           "source_url", "contact_name", "contact_email", "summary"]
SOURCES = ("contracts_finder", "find_a_tender")
MAX_PAGES = 10


# ---------------------------------------------------------------------------------------------
# Fetching

def source_url(source: str, params: dict, page: int, since: str, until: str) -> str:
    if source == "contracts_finder":
        args = {"stages": params.get("stages", "tender"),
                "publishedFrom": f"{since}T00:00:00", "publishedTo": f"{until}T23:59:59",
                "limit": str(params.get("page_size", 100))}
        return "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?" + urllib.parse.urlencode(args)
    if source == "find_a_tender":
        args = {"updatedFrom": f"{since}T00:00:00", "updatedTo": f"{until}T23:59:59",
                "limit": str(params.get("page_size", 100)), "stages": params.get("stages", "tender")}
        return "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?" + urllib.parse.urlencode(args)
    raise ValueError(f"unknown tender source: {source}")


def releases_of(payload) -> list[dict]:
    """Flatten a page to a list of OCDS releases.

    The two services wrap releases differently, and Contracts Finder has shipped both a bare-release
    and a release-package form of its search result. Accept either rather than depend on one.
    """
    out = []
    if isinstance(payload, dict):
        items = payload.get("releases") or payload.get("results") or []
    else:
        items = payload if isinstance(payload, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("releases"), list):   # a release package
            out.extend(r for r in item["releases"] if isinstance(r, dict))
        else:                                        # already a release
            out.append(item)
    return out


def next_link(payload) -> str:
    links = (payload or {}).get("links") or {}
    nxt = links.get("next")
    return nxt if isinstance(nxt, str) else ""


def fetch_source(source: str, params: dict, since: str, until: str) -> dict:
    """Page a source until exhausted or the page cap is hit.

    ``_complete`` is False when paging stopped with a next link still offered, so a truncated read
    is never reported as a complete view of what was published.
    """
    max_pages = int(params.get("max_pages", MAX_PAGES))
    releases, pages, url = [], 0, source_url(source, params, 1, since, until)
    nxt = ""
    while url and pages < max_pages:
        try:
            payload = fetch_json(url, {"User-Agent": USER_AGENT})
        except collection_budget.Deferred:
            if not pages: raise
            return {"releases": releases, "_pages": pages, "_complete": False, "_stop_reason": "budget"}
        pages += 1
        batch = releases_of(payload)
        releases.extend(batch)
        nxt = next_link(payload)
        if not batch or not nxt:
            break
        url = nxt
    return {"releases": releases, "_pages": pages, "_complete": not nxt}


# ---------------------------------------------------------------------------------------------
# Normalisation

def _party(release: dict, role: str) -> dict:
    for p in release.get("parties") or []:
        if role in [r.lower() for r in (p.get("roles") or [])]:
            return p
    return {}


def normalise(source: str, release: dict) -> dict:
    """One OCDS release -> one flat notice row. Absent facts stay empty, never guessed."""
    tender = release.get("tender") or {}
    value = tender.get("value") or tender.get("minValue") or {}
    cls = tender.get("classification") or {}
    if not cls:
        items = tender.get("items") or []
        cls = (items[0].get("classification") or {}) if items and isinstance(items[0], dict) else {}
    buyer = release.get("buyer") or _party(release, "buyer")
    contact = tender.get("contactPoint") or buyer.get("contactPoint") or _party(release, "buyer").get("contactPoint") or {}
    period = tender.get("tenderPeriod") or {}
    amount = value.get("amount")
    return {
        "source": source,
        "release_id": str(release.get("id", "") or ""),
        "ocid": release.get("ocid", "") or "",
        "buyer": buyer.get("name", "") or "",
        "title": tender.get("title", "") or "",
        "value_amount": f"{float(amount):.0f}" if isinstance(amount, (int, float)) else "",
        "value_currency": (value.get("currency") or "") if amount is not None else "",
        "published": (release.get("date") or "")[:10],
        "deadline": (period.get("endDate") or "")[:10],
        "cpv": str(cls.get("id", "") or ""),
        "cpv_label": cls.get("description", "") or "",
        "status": tender.get("status", "") or "",
        "url": _notice_url(source, release, tender),
        "source_url": _release_source_url(release),
        "contact_name": contact.get("name", "") or "",
        "contact_email": contact.get("email", "") or "",
        "summary": " ".join(strip_html(tender.get("description", "") or "").split())[:400],
    }


def _release_source_url(release: dict) -> str:
    links = release.get("links") or {}
    if isinstance(links, dict):
        return links.get("self") or links.get("canonical") or release.get("uri") or ""
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and link.get("href") and link.get("rel") in ("self", "canonical"):
                return link["href"]
    return release.get("uri") or ""


def _notice_url(source: str, release: dict, tender: dict) -> str:
    for doc in tender.get("documents") or []:
        if isinstance(doc, dict) and doc.get("url") and doc.get("documentType") in (None, "", "tenderNotice"):
            return doc["url"]
    rid = release.get("id", "") or release.get("ocid", "")
    if source == "contracts_finder" and rid:
        return f"https://www.contractsfinder.service.gov.uk/Notice/{rid}"
    if source == "find_a_tender" and rid:
        return f"https://www.find-tender.service.gov.uk/Notice/{rid}"
    return ""


# ---------------------------------------------------------------------------------------------
# Rules

def relevant(row: dict, f: dict) -> tuple[bool, str]:
    """Keep a notice only on a stated reason: a matching CPV class or a keyword in its own text.

    Rejects are checked first, then the value floor, then the deadline. An unstated value or
    deadline is not a rejection — it is an unknown, and the row carries it as one.
    """
    blob = f"{row['title']} {row['summary']}".lower()
    for neg in f.get("negative", []):
        if _word(neg, blob):
            return False, f"negative:{neg}"
    floor = float(f.get("minimum_value_gbp", 0) or 0)
    if floor and row["value_amount"] and row["value_currency"] in ("GBP", ""):
        if float(row["value_amount"]) < floor:
            return False, f"below-value-floor:{row['value_amount']}"
    reasons = []
    for prefix in f.get("cpv_prefixes", []):
        if row["cpv"].startswith(str(prefix)):
            reasons.append(f"cpv:{prefix}")
            break
    for kw in f.get("keywords", []):
        # A single word matches as a word, so "test" does not fire on "latest" or "contest";
        # a phrase matches as a substring, because its own words already make it specific.
        hit = _word(kw, blob) if " " not in kw.strip() else kw.lower() in blob
        if hit:
            reasons.append(f"keyword:{kw}")
            break
    if not reasons:
        return False, "no-match"
    return True, "; ".join(reasons)


def expired(row: dict, today: str) -> bool:
    return bool(row["deadline"]) and row["deadline"] < today


# ---------------------------------------------------------------------------------------------
# Run

def run(portals_path: Path, out_dir: Path, today: str, days: int = 7,
        fixture_dir: Path | None = None) -> dict:
    import yaml
    cfg = yaml.safe_load(portals_path.read_text())
    tcfg = cfg.get("tenders") or {}
    f = tcfg.get("filter", {})

    store = PendingStore(out_dir / "pending" / "tenders", COLUMNS, "source_key")
    observations = ObservationStore(out_dir / "pending" / "observations.json")
    rows, seen = store.resume(today)

    until = today
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    status = {"date": today, "window": [since, until], "sources": {},
              "totals": {"sources": 0, "notices_read": 0, "matched": 0, "new": 0, "expired_skipped": 0}}
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    scheduler = collection_schedule.Session(out_dir, offline=fixture_dir is not None)
    for src in tcfg.get("sources", []):
        if not src.get("enabled", False):
            continue
        name = src.get("name", src.get("source", "?"))
        source = src.get("source", "")
        status["totals"]["sources"] += 1
        if source not in SOURCES:
            status["sources"][name] = {"state": "unsupported-source", "source": source}
            continue
        task_key = 'tender:'+source
        if not scheduler.choose(source,[task_key],background=True):
            status['sources'][name] = scheduler.receipt(source)
            continue
        try:
            if fixture_dir:
                fx = fixture_dir / f"tender-{source}.json"
                if not fx.exists():
                    raise FileNotFoundError(f"fixture missing: {fx}")
                payload = json.loads(fx.read_text())
                payload = {"releases": releases_of(payload), "_pages": 1, "_complete": True}
            else:
                payload = scheduler.call(source,task_key,lambda: fetch_source(source, src.get("params", {}), since, until))
        except collection_budget.Deferred as error:
            status['sources'][name] = scheduler.receipt(source,error.reason)
            continue
        except urllib.error.HTTPError as e:
            status["sources"][name] = {"state": f"http-{e.code}", "source": source}
            observations.observe(scope=f"tender:{source}", source_type="tender", provider=source,
                                 observed_at=observed_at, state=f"http-{e.code}",
                                 complete=False, items=[], detail={"name": name, "kind": failure_kind(e), "status": e.code})
            continue
        except Exception as e:
            status["sources"][name] = {"state": "error", "detail": str(e)[:200], "source": source}
            observations.observe(scope=f"tender:{source}", source_type="tender", provider=source,
                                 observed_at=observed_at, state="error", complete=False,
                                 items=[], detail={"name": name, **failure(e)})
            continue

        releases = payload["releases"]
        status["totals"]["notices_read"] += len(releases)
        matched = new = skipped = 0
        for release in releases:
            row = normalise(source, release)
            ok, why = relevant(row, f)
            if not ok:
                continue
            matched += 1
            if expired(row, today):
                skipped += 1
                continue
            release_id = row["release_id"] or row["ocid"]
            row["release_id"] = release_id
            discovery_id = f"{source}:{release_id}" if release_id else ""
            row["source_key"] = discovery_id
            if not discovery_id or any(k in seen for k in (discovery_id, release_id, row["ocid"])):
                continue
            seen.add(discovery_id)
            new += 1
            rows.append({**{c: "" for c in COLUMNS}, **row, "date_seen": today, "why": why})
        status["totals"]["matched"] += matched
        status["totals"]["new"] += new
        status["totals"]["expired_skipped"] += skipped
        complete = bool(payload.get("_complete", True))
        status["sources"][name] = {
            "state": ("ok" if releases else "empty") if complete else "partial", "complete": complete,
            "notices": len(releases), "matched": matched, "new": new, "expired_skipped": skipped,
            "source": source, "pages": payload.get("_pages"),
        }
        lifecycle_items = []
        for release in releases:
            row = normalise(source, release)
            sid = row["release_id"] or row["ocid"]
            eligible, eligibility_reason = relevant(row, f)
            eligible = bool(eligible and not expired(row, today))
            row["eligible"] = eligible
            row["eligibility_reason"] = eligibility_reason
            explicit = None
            if (row.get("status") or "").lower() in ("cancelled", "canceled", "withdrawn", "complete"):
                explicit = {"reason": f"source-status:{row['status'].lower()}"}
            lifecycle_items.append({"source_id": sid, "current": row, "explicit_closure": explicit})
        observations.observe(scope=f"tender:{source}", source_type="tender", provider=source,
                             observed_at=observed_at, state=status["sources"][name]["state"],
                             complete=complete, items=lifecycle_items,
                             detail={"name": name, "window": [since, until]},
                             absence_authoritative=False)

    # Soonest real deadline first: a notice with a date you can still meet is the actionable one,
    # and notices with no stated deadline sort after them rather than being dropped.
    rows.sort(key=lambda r: (not r["deadline"], r["deadline"], r["buyer"], r["title"]))
    store.save(today, rows, seen, status)
    return status


EXPECTED = ["ocid", "date", "tender.title", "tender.description", "tender.status",
            "tender.value.amount", "tender.tenderPeriod.endDate", "buyer.name"]


def probe(source: str, params: dict, since: str, until: str) -> dict:
    """Fetch one page and report which OCDS fields this service actually populates."""
    payload = fetch_json(source_url(source, params, 1, since, until), {"User-Agent": USER_AGENT})
    releases = releases_of(payload)
    if not releases:
        return {"source": source, "ok": False, "sampled": 0,
                "problem": "no releases found in the response",
                "top_level_keys": sorted(payload)[:20] if isinstance(payload, dict) else []}
    present = {}
    for path in EXPECTED:
        hits = 0
        for r in releases:
            cur, found = r, True
            for part in path.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    found = False
                    break
                cur = cur[part]
            hits += int(found and cur not in (None, "", {}, []))
        present[path] = hits
    required = ("ocid", "date", "tender.title", "tender.status")
    missing = [p for p in required if present[p] == 0]
    optional_missing = [p for p, n in present.items() if n == 0 and p not in required]
    return {"source": source, "ok": not missing, "sampled": len(releases), "present": present,
            "missing": missing, "optional_missing": optional_missing,
            "shape": "release-package" if isinstance((payload.get("results") or [{}])[0], dict)
                     and "releases" in (payload.get("results") or [{}])[0] else "bare-release"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--portals", type=Path, default=root / "config/portals.yml")
    ap.add_argument("--out", type=Path, default=root / "data")
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--days", type=int, default=7, help="how far back to ask for notices (default 7)")
    ap.add_argument("--offline-fixture", type=Path, default=None)
    ap.add_argument("--probe", metavar="SOURCE", default=None, help=f"{'|'.join(SOURCES)}|all")
    a = ap.parse_args(argv)

    if a.probe:
        import yaml
        cfg = yaml.safe_load(a.portals.read_text())
        until = a.today
        since = (dt.date.fromisoformat(a.today) - dt.timedelta(days=a.days)).isoformat()
        rc = 0
        for source in (SOURCES if a.probe == "all" else (a.probe,)):
            src = next((s for s in (cfg.get("tenders") or {}).get("sources", []) if s.get("source") == source), {})
            try:
                r = probe(source, src.get("params", {}), since, until)
            except Exception as e:
                print(f"{source}: request failed — {type(e).__name__}: {str(e)[:200]}")
                rc = 1
                continue
            print(f"{source}: {'SCHEMA OK' if r['ok'] else 'SCHEMA MISMATCH'} "
                  f"({r['sampled']} releases sampled, shape={r.get('shape', '?')})")
            for path, n in r.get("present", {}).items():
                print(f"    {'ok ' if n else 'MISSING'} {path:32} {n} populated")
            if r.get("problem"):
                print(f"    {r['problem']}; top-level keys: {r.get('top_level_keys')}")
            rc = rc or (0 if r["ok"] else 1)
        return rc

    status = run(a.portals, a.out, a.today, a.days, a.offline_fixture)
    t = status["totals"]
    bad = [n for n, s in status["sources"].items() if s.get("state") not in ("ok", "empty", "partial", "deferred")]
    print(f"sources={t['sources']} window={status['window'][0]}..{status['window'][1]} "
          f"read={t['notices_read']} matched={t['matched']} new={t['new']} "
          f"expired_skipped={t['expired_skipped']} problem_sources={len(bad)}")
    if bad:
        print("problem sources: " + ", ".join(f"{n}({status['sources'][n]['state']})" for n in bad))
    print('collection-receipt: '+json.dumps(collection_schedule.dispatch_summary(status)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
