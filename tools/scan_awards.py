#!/usr/bin/env python3
"""Read public UK contract awards as supplier research signals, never open opportunities.

No account, key or contact discovery is involved. Award value belongs to the published award or
contract, not to an available subcontract budget. The seven-day publication/update window is
presence-only: a notice leaving it cannot close an item. Actual award dates remain separate.

Configuration: commercial_awards.sources, with commercial_awards.filter falling back to
tenders.filter. Each provider is capped at two pages of 100 notices. Write current observations
and pending/awards/last-run.json; there is no second discovery TSV or database writer.
"""
from __future__ import annotations

import argparse
import collection_schedule
import collection_budget
import datetime as dt
import json
import math
import re
import sys
import unicodedata
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pending_store import ObservationStore, atomic_write
from scan_tenders import SOURCES, next_link, releases_of, relevant, source_url
from web_read import USER_AGENT, failure, fetch_json, strip_html

WINDOW_DAYS = 7
MAX_PAGES = 2
MAX_PAGE_SIZE = 100
CLOSED = {"cancelled", "canceled", "withdrawn", "terminated", "complete", "completed", "unsuccessful"}
# Procurement labels are inconsistent, and "testing" frequently means electrical or clinical work.
# Require both a software setting and a delivery activity in the bounded award scope. A licence,
# appliance or generic consultancy purchase by itself is not a useful subcontracting hypothesis.
SOFTWARE_CONTEXT = re.compile(
    r"\b(?:software|digital|applications?|apis?|salesforce|unit4|erp|crm|saas|llms?|ai|devops|developers?)\b"
    r"|\bdata\s+(?:integration|warehouse|platform|pipeline|migration)\b|\bcloud[- ](?:based|hosted)\b", re.I)
DELIVERY_CONTEXT = re.compile(
    r"\b(?:integrat(?:e|es|ing|ion|ions)|implement(?:ation|ing|ed)?|migrat(?:ion|ing|e)|"
    r"moderni[sz]ation|deployment|configuration|handover|recovery|production)\b"
    r"|\b(?:test(?:ing)?\s+(?:strategy|automation|support)|digital\s+(?:delivery|test)|"
    r"(?:software|application)\s+development|"
    r"ai\s+assurance|model\s+evaluation|release\s+(?:evidence|readiness|reliability)|"
    r"knowledge\s+transfer|engineering\s+(?:quality|capability|leadership|productivity)|data\s+capture)\b", re.I)
PHYSICAL_SCOPE = re.compile(
    r"\b(?:building management|building fabric|fire protection|fire safety|portable appliance|"
    r"decontamination|sterilisers?|sterilizers?|laboratory equipment)\b", re.I)


def _objects(value) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _text(value, limit=1000) -> str:
    return " ".join(strip_html(value).split())[:limit] if isinstance(value, str) else ""


def _day(value) -> str:
    """Keep the source's calendar date, including UK midnight offsets; invalid dates stay unknown."""
    if not isinstance(value, str):
        return ""
    try:
        return dt.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return ""


def _release_order(release: dict) -> tuple[dt.datetime, str]:
    """Compare release updates in UTC, not lexicographically across different offsets."""
    try:
        stamp = dt.datetime.fromisoformat(str(release.get("date", "")).replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=dt.timezone.utc) if stamp.tzinfo is None else stamp
        stamp = stamp.astimezone(dt.timezone.utc)
    except ValueError:
        stamp = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return stamp, str(release.get("id") or "")


def delivery_context(title: str, description: str) -> bool:
    """Whether the notice states software delivery work, rather than only a class or purchase."""
    text = title[:500] + " " + description[:4000]
    # Incumbent renewals can mention migration/implementation only as risks they avoid.
    text = re.sub(r"\b(?:avoid(?:ing)?|without)\b[^.;]{0,500}", " ", text, flags=re.I)
    return bool(not PHYSICAL_SCOPE.search(title) and SOFTWARE_CONTEXT.search(text) and DELIVERY_CONTEXT.search(text))


def fetch_source(source: str, params: dict, since: str, until: str) -> dict:
    """Read bounded pages, retaining successful earlier pages when a later page fails."""
    cap = max(1, min(MAX_PAGES, int(params.get("max_pages", MAX_PAGES))))
    size = max(1, min(MAX_PAGE_SIZE, int(params.get("page_size", MAX_PAGE_SIZE))))
    url = source_url(source, {"stages": "award", "page_size": size}, 1, since, until)
    origin = urllib.parse.urlsplit(url)
    releases, visited = [], set()
    pages = 0
    while url and pages < cap:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != origin.netloc or parsed.path != origin.path
                or url in visited):
            return {"releases": releases, "_pages": pages, "_complete": False,
                    "_error": {"kind": "bad-response", "error": "unsafe or repeated pagination cursor"}}
        visited.add(url)
        try:
            payload = fetch_json(url, {"User-Agent": USER_AGENT})
            if not isinstance(payload, dict) or not any(isinstance(payload.get(k), list) for k in ("releases", "results")):
                raise ValueError("award response has no release collection")
            batch = releases_of(payload)
            nxt = next_link(payload)
        except collection_budget.Deferred:
            if not pages:raise
            return {'releases':releases,'_pages':pages,'_complete':False,'_stop_reason':'budget'}
        except Exception as error:
            if not pages:
                raise
            return {"releases": releases, "_pages": pages, "_complete": False, "_error": failure(error)}
        pages += 1
        releases.extend(batch)
        url = nxt
        if not batch:
            break
    return {"releases": releases, "_pages": pages, "_complete": not bool(url)}


def _notice_url(source: str, release: dict, award: dict, contracts: list[dict]) -> str:
    host = "www.contractsfinder.service.gov.uk" if source == "contracts_finder" else "www.find-tender.service.gov.uk"
    for obj in [award, *contracts, release.get("tender") or {}, release]:
        for doc in _objects(obj.get("documents")):
            url = doc.get("url") or ""
            parts = urllib.parse.urlsplit(url)
            if parts.scheme == "https" and parts.netloc == host and parts.path.startswith("/Notice/") and "/Attachment/" not in parts.path:
                return url
    rid = str(release.get("id") or "")
    pattern = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}" if source == "contracts_finder" else r"\d{6,}-\d{4}"
    return f"https://{host}/Notice/{rid}" if re.fullmatch(pattern, rid) else ""


def _period(award: dict, contracts: list[dict]) -> tuple[str, str]:
    period = award.get("contractPeriod") or {}
    start, end = _day(period.get("startDate")), _day(period.get("endDate"))
    if contracts:
        starts = [_day((c.get("period") or {}).get("startDate")) for c in contracts]
        ends = [_day((c.get("period") or {}).get("endDate")) for c in contracts]
        start = start or (min(starts) if all(starts) else "")
        end = end or (max(ends) if all(ends) else "")
    return start, end


def _scope(tender: dict, award: dict, contracts: list[dict]) -> tuple[str, str]:
    """Use the awarded lot/contract scope before any whole-procurement description."""
    related = {str(x) for x in award.get("relatedLots") or []}
    lots = [lot for lot in _objects(tender.get("lots")) if str(lot.get("id")) in related]
    contract_title = contracts[0].get("title") if len(contracts) == 1 else ""
    contract_text = " ".join(_text(c.get("description"), 4000) for c in contracts).strip()
    if related:
        # An unresolved lot reference must not borrow an unrelated lot's software demand.
        fallback_title = " / ".join(_text(lot.get("title"), 500) for lot in lots).strip(" / ")
        single_lot = len(lots) == len(_objects(tender.get("lots"))) == len(related) == 1
        if single_lot:
            fallback_title = fallback_title or tender.get("title")
        fallback_title = fallback_title or "Contract lot " + ", ".join(sorted(related))
        fallback_text = " ".join(_text(lot.get("description"), 4000) for lot in lots)
        if single_lot:
            # One lot is the whole procurement; retaining its headline and overview is safe.
            fallback_text += " " + _text(tender.get("description"), 4000)
    else:
        fallback_title, fallback_text = tender.get("title"), tender.get("description")
    return (_text(award.get("title") or contract_title or fallback_title, 500),
            _text(award.get("description") or contract_text or fallback_text, 4000))


def normalise(source: str, release: dict, today: str, filters: dict) -> list[dict]:
    """One source award/supplier pair per item, with no inferred missing dates or supplier budget."""
    ocid = str(release.get("ocid") or "")
    if not ocid:
        return []
    tender = release.get("tender") or {}
    parties = {str(p.get("id")): p for p in _objects(release.get("parties")) if p.get("id")}
    buyer_ref = release.get("buyer") or next((p for p in parties.values() if "buyer" in (p.get("roles") or [])), {})
    buyer = _text(buyer_ref.get("name") or parties.get(str(buyer_ref.get("id")), {}).get("name"))
    classification = tender.get("classification") or next(
        (i.get("classification") for i in _objects(tender.get("items")) if i.get("classification")), {})
    # A moving search-window URL is request metadata, not a changed source fact. Keep it in the
    # receipt; a stable record API or provider endpoint preserves unchanged-evidence deduplication.
    api_url = ("https://www.contractsfinder.service.gov.uk/Published/OCDS/Record/" + urllib.parse.quote(ocid, safe="")
               if source == "contracts_finder" else "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages")
    items = []
    for award in _objects(release.get("awards")):
        award_id = str(award.get("id") or "")
        if not award_id:
            continue
        contracts = [c for c in _objects(release.get("contracts")) if str(c.get("awardID") or "") == award_id]
        active_contracts = [c for c in contracts if str(c.get("status") or "").lower() not in CLOSED]
        all_contracts_closed = bool(contracts and not active_contracts)
        start, end = _period(award, active_contracts or contracts)
        value = award.get("value") or ((active_contracts[0].get("value") or {}) if len(active_contracts) == 1 else {})
        amount = value.get("amount", "")
        amount = amount if isinstance(amount, (int, float)) and not isinstance(amount, bool) and math.isfinite(amount) and amount >= 0 else ""
        currency = _text(value.get("currency"), 8) if amount != "" else ""
        title, description = _scope(tender, award, contracts)
        # The tender filter includes broad management/R&D classes. Those alone do not establish
        # a software workstream for an awarded supplier; require a configured text cue instead.
        cpv = str(classification.get("id") or "")
        software_cpv = cpv if cpv.startswith(("722", "725", "726")) else ""
        match, why = relevant({"title": title, "summary": description,
                               "cpv": software_cpv,
                               "value_amount": str(amount), "value_currency": currency}, filters)
        if match and not delivery_context(title, description):
            match, why = False, "no-software-delivery-context"
        award_date = _day(award.get("date"))
        closure = ""
        if str(award.get("status") or "").lower() in CLOSED:
            closure = "source-status:" + str(award["status"]).lower()
        elif all_contracts_closed:
            closure = "source-status:contracts-closed"
        elif end and end < today:
            closure = "contract-expired:" + end
        suppliers = _objects(award.get("suppliers"))
        for supplier in suppliers:
            supplier_id = str(supplier.get("id") or "")
            company = _text(supplier.get("name") or parties.get(supplier_id, {}).get("name"), 300)
            if not company:
                continue
            identity = supplier_id or "name:" + " ".join(unicodedata.normalize("NFKC", company).casefold().split())
            source_id = "|".join(urllib.parse.quote(v, safe="") for v in (source, ocid, award_id, identity))
            unknown = " Award date not stated." if not award_date else f" Award dated {award_date}."
            if not end:
                unknown += " Contract end date not stated."
            summary = (description[:800] + unknown + " Published award evidence only; subcontract demand and budget are unconfirmed.").strip()
            row = {"signal_type": "contract_award", "source": source, "ocid": ocid, "award_id": award_id,
                   "company": company, "supplier_id": supplier_id, "buyer": buyer, "title": title,
                   "summary": summary, "award_date": award_date, "contract_start": start,
                   "contract_end": end, "expires": end, "amount": amount, "currency": currency,
                   "amount_scope": "award" if award.get("value") else "contract" if amount != "" else "",
                   "supplier_count": len(suppliers), "url": _notice_url(source, release, award, contracts),
                   "source_url": api_url, "published": _day(release.get("date")),
                   "headline": f"{company} — contract award: {title}",
                   "eligible": bool(match and not closure), "eligibility_reason": closure or why}
            items.append({"source_id": source_id, "current": row,
                          "explicit_closure": {"reason": closure} if closure else None})
    return items


def run(portals_path: Path, out_dir: Path, today: str, fixture_dir: Path | None = None) -> dict:
    import yaml
    cfg = yaml.safe_load(portals_path.read_text()) or {}
    acfg = cfg.get("commercial_awards") or {}
    filters = acfg.get("filter", (cfg.get("tenders") or {}).get("filter", {}))
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    observations = ObservationStore(out_dir / "pending" / "observations.json")
    previous = observations.load().get("items", {})
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    status = {"date": today, "window": [since, today], "sources": {},
              "totals": {"sources": 0, "notices_read": 0, "signals": 0, "new": 0}}
    processed = set()
    scheduler = collection_schedule.Session(out_dir, offline=fixture_dir is not None)
    for src in acfg.get("sources", []) if acfg.get("enabled", True) else []:
        source = src.get("source", "")
        name = src.get("name", source or "unknown")
        if not src.get("enabled", False):
            status["sources"][name] = {"state": "disabled", "source": source}
            continue
        if source not in SOURCES or source in processed:
            status["sources"][name] = {"state": "unsupported-source" if source not in SOURCES else "duplicate-source", "source": source}
            continue
        processed.add(source)
        status["totals"]["sources"] += 1
        scope = f"award:{source}"
        task_key = 'award:'+source
        if not scheduler.choose(source,[task_key],background=True):
            status['sources'][name] = scheduler.receipt(source)
            continue
        try:
            if fixture_dir:
                payload = json.loads((fixture_dir / f"award-{source}.json").read_text())
                payload = {"releases": releases_of(payload), "_pages": 1, "_complete": not bool(next_link(payload))}
            else:
                payload = scheduler.call(source,task_key,lambda: fetch_source(source, src.get("params") or {}, since, today))
            latest = {}
            for release in sorted(payload["releases"], key=_release_order):
                for item in normalise(source, release, today, filters):
                    latest[item["source_id"]] = item
            items = [item for sid, item in latest.items() if item["current"]["eligible"] or ObservationStore.key(scope, sid) in previous]
            complete = bool(payload.get("_complete", False))
            state = ("ok" if payload["releases"] else "empty") if complete else "partial"
            new = sum(ObservationStore.key(scope, item["source_id"]) not in previous for item in items)
            receipt = {"state": state, "source": source, "complete": complete,
                       "notices": len(payload["releases"]), "signals": len(items), "new": new,
                       "pages": payload.get("_pages", 0), **({"stop_reason":payload["_stop_reason"]} if payload.get("_stop_reason") else {}), **(payload.get("_error") or {})}
            status["totals"]["notices_read"] += receipt["notices"]
            status["totals"]["signals"] += len(items)
            status["totals"]["new"] += new
        except collection_budget.Deferred as error:
            status['sources'][name] = scheduler.receipt(source,error.reason)
            continue
        except Exception as error:
            receipt = {"state": "error", "source": source, "complete": False, **failure(error)}
            items, complete, state = [], False, "error"
        status["sources"][name] = receipt
        observations.observe(scope=scope, source_type="commercial_signal", provider=source,
                             observed_at=observed_at, state=state, complete=complete, items=items,
                             absence_authoritative=False, expired_on=today,
                             detail={"name": name, "window": [since, today], **receipt})
    atomic_write(out_dir / "pending" / "awards" / "last-run.json", json.dumps(status, indent=2))
    return status


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--portals", type=Path, default=root / "config" / "portals.yml")
    ap.add_argument("--out", type=Path, default=root / "data")
    ap.add_argument("--today", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    ap.add_argument("--offline-fixture", type=Path)
    args = ap.parse_args(argv)
    status = run(args.portals, args.out, args.today, args.offline_fixture)
    print(json.dumps(status, indent=2))
    return int(any(s.get("error") or s["state"] not in ("ok", "empty", "disabled", "partial", "deferred")
                   for s in status["sources"].values()))


if __name__ == "__main__":
    sys.exit(main())
