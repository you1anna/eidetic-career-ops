#!/usr/bin/env python3
"""Read funding news from public RSS feeds: companies that have just raised money.

A funding story is a trigger signal, not an opportunity. A company that has just raised may soon
need interim or fractional engineering and quality leadership, and can be approached directly before
any advert exists. Only a headline shaped "<Company> <raises|secures|…> <amount>" is kept, so the
company name is read from the headline (refined by the feed's own tags), never guessed from prose.
Fund raises are not company raises and are dropped. Presence-only: a story leaving a feed means
nothing, and nothing here contacts anyone.
"""
from __future__ import annotations

import argparse
import collection_schedule
import collection_budget
import datetime as dt
import email.utils
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pending_store import ObservationStore, PendingStore  # noqa: E402
from web_read import failure, fetch_text, strip_html  # noqa: E402

COLUMNS = ["date_seen", "feed", "source", "guid", "company", "amount", "currency", "amount_millions", "published",
           "region", "url", "headline", "summary"]
VERBS = ("raises", "secures", "lands", "bags", "closes", "nets", "scores", "grabs", "snags", "picks up")
HEADLINE = re.compile(
    r"^(?:exclusive:\s*)?(?P<lead>.+?)\s+(?P<verb>" + "|".join(VERBS) + r")\s+(?:an?\s+)?(?:further\s+)?"
    r"(?P<cur>[£$€])\s?(?P<num>\d+(?:[.,]\d+)?)\s?(?P<unit>bn|billion|m|mn|million|k)\b"
    r"(?:\s+(?P<code>[A-Z]{3})\b)?", re.I)
FUND_TERMS = re.compile(r"\b(?:fund|funds|first close|final close|fund of funds)\b", re.I)
CURRENCY = {"£": "GBP", "$": "USD", "€": "EUR"}
SCALE = {"bn": 1000.0, "billion": 1000.0, "m": 1.0, "mn": 1.0, "million": 1.0, "k": 0.001}


def company_from(lead: str, tags: list[str], descriptors: set[str]) -> str:
    """The company a funding headline names: the trailing run of capitalised words before the verb.

    "London traveltech Stasher" -> "Stasher"; "AI platform Reply Next" -> "Reply Next";
    "Scotland's HexSeed" -> "HexSeed". A leading descriptor ("Fintech Foo") is dropped. When the feed
    tags the story with a name the run ends with, the tag wins, because the publisher chose it."""
    words = lead.replace("’", "'").split()
    run: list[str] = []
    for w in reversed(words):
        if w.endswith("'s") or "-based" in w.lower() or not (w[0].isupper() or w[0].isdigit()):
            break
        run.insert(0, w)
    while len(run) > 1 and run[0].strip(",").casefold() in descriptors:
        run.pop(0)
    name = " ".join(run).strip(" ,:")
    for tag in sorted(tags, key=len, reverse=True):
        if tag and name and name.casefold().endswith(tag.casefold()) and len(tag) < len(name):
            return tag
    return name


def parse_item(item: dict, feed: dict, descriptors: set[str], exclude: tuple[str, ...] = ()) -> dict | None:
    """One funding story, or None when the headline does not state a company raise with an amount, or
    names something that is not a growth raise (a configured exclusion such as debt or job cuts)."""
    title = " ".join(html.unescape(item.get("title") or "").split())
    m = HEADLINE.match(title)
    if not m or FUND_TERMS.search(title) or any(x in title.casefold() for x in exclude):
        return None
    company = company_from(m["lead"], item.get("tags") or [], descriptors)
    if not company:
        return None
    millions = float(m["num"].replace(",", ".")) * SCALE[m["unit"].lower()]
    currency = (m["code"] or CURRENCY[m["cur"]]).upper()
    published = item.get("published") or ""
    return {"feed": feed["id"], "source": feed.get("name", feed["id"]), "guid": item.get("guid") or item.get("link") or "", "company": company,
            "amount": f"{m['cur']}{m['num']}{' ' if len(m['unit']) > 2 else ''}{m['unit']}" + (f" {m['code']}" if m["code"] else ""),
            "currency": currency, "amount_millions": f"{millions:g}", "published": published,
            "region": feed.get("region", ""), "url": item.get("link") or "", "headline": title,
            "summary": " ".join(strip_html(html.unescape(item.get("description") or "")).split())[:400],
            "eligible": millions >= float(feed.get("min_amount_millions", 0) or 0)}


def read_rss(text: str) -> list[dict]:
    items = []
    for it in ET.fromstring(text.encode("utf-8")).iter("item"):
        when = it.findtext("pubDate") or ""
        try:
            published = email.utils.parsedate_to_datetime(when).date().isoformat() if when else ""
        except (TypeError, ValueError):
            published = ""
        items.append({"title": it.findtext("title") or "", "link": (it.findtext("link") or "").strip(),
                      "guid": (it.findtext("guid") or "").strip(), "description": it.findtext("description") or "",
                      "published": published, "tags": [(c.text or "").strip() for c in it.findall("category")]})
    return items


def run(portals_path: Path, out_dir: Path, today: str, fixtures: dict[str, Path] | None = None) -> dict:
    import yaml
    cfg = (yaml.safe_load(portals_path.read_text()) or {}).get("signals") or {}
    descriptors = {d.casefold() for d in cfg.get("descriptors", [])}
    exclude = tuple(x.casefold() for x in cfg.get("exclude_terms", []))
    store = PendingStore(out_dir / "pending" / "news", COLUMNS, "guid")
    observations = ObservationStore(out_dir / "pending" / "observations.json")
    rows, seen = store.resume(today)
    status = {"date": today, "feeds": {}, "new": 0}
    scheduler = collection_schedule.Session(out_dir, offline=fixtures is not None)
    for feed in cfg.get("funding_news", []):
        if not feed.get("enabled"):
            status["feeds"][feed["id"]] = {"state": "disabled"}
            continue
        scope = f"news:{feed['id']}"
        observed_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        task_key = 'news:'+feed['id']
        if not scheduler.choose(feed['id'],[task_key],background=True):
            status['feeds'][feed['id']] = scheduler.receipt(feed['id'])
            continue
        try:
            text = fixtures[feed["id"]].read_text() if fixtures else scheduler.call(feed["id"],task_key,lambda:fetch_text(feed["url"]))
            items = read_rss(text)
        except collection_budget.Deferred as error:
            status['feeds'][feed['id']] = scheduler.receipt(feed['id'],error.reason)
            continue
        except Exception as error:  # one feed failing never stops the others
            status["feeds"][feed["id"]] = {"state": "error", "error": f"{type(error).__name__}: {str(error)[:160]}",
                                           "kind": failure(error)["kind"]}
            observations.observe(scope=scope, source_type="signal", provider=feed["id"], observed_at=observed_at,
                                 state="error", complete=False, items=[], absence_authoritative=False,
                                 detail={**failure(error), "error": status["feeds"][feed["id"]]["error"]})
            continue
        stories = [s for s in (parse_item(i, feed, descriptors, exclude) for i in items) if s and s["guid"]]
        kept = [s for s in stories if s["eligible"]]
        for s in kept:
            if s["guid"] not in seen:
                seen.add(s["guid"])
                rows.append({"date_seen": today, **s})
                status["new"] += 1
        state = "ok" if kept else "empty"
        status["feeds"][feed["id"]] = {"state": state, "items": len(items), "funding": len(stories), "kept": len(kept)}
        observations.observe(scope=scope, source_type="signal", provider=feed["id"], observed_at=observed_at,
                             state=state, complete=True, absence_authoritative=False,
                             items=[{"source_id": s["guid"], "current": s} for s in kept],
                             detail={"name": feed.get("name", feed["id"]), "items": len(items), "funding": len(stories)})
    store.save(today, rows, seen, status)
    return status


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--portals", type=Path, default=root / "config/portals.yml")
    ap.add_argument("--out", type=Path, default=root / "data")
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--offline-fixture", action="append", default=[], metavar="FEED=PATH",
                    help="read a saved RSS file instead of the network, for example uktn=tools/fixtures/news/uktn.xml")
    a = ap.parse_args(argv)
    fixtures = {k: Path(v) for k, v in (x.split("=", 1) for x in a.offline_fixture)} or None
    status = run(a.portals, a.out, a.today, fixtures)
    print(json.dumps(status, indent=2))
    return 0 if all(f["state"] in ("ok", "empty", "disabled", "deferred") for f in status["feeds"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
