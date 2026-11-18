#!/usr/bin/env python3
"""Channel coverage report — what the career system can see, and what it cannot.

Record counts do not answer "are we covering the opportunities that exist". A system can hold
hundreds of records and still be blind to a whole market: the 14–16 September runs read ~10,000 jobs
across 61 employer boards and returned 176 candidates, none of them contract, because employer
boards carry employer headcount and UK contract work is brokered elsewhere.

This reads the channel map (``channels.yml``), the scanner's own configuration and its run history,
and reports each channel's status next to its measured yield. Measured means counted from scan
output, not asserted: a channel with no runtime has no yield and is shown as such rather than as a
gap that might be fine.

Read-only. It writes a report when asked and never touches records, config or scan state.

Usage: coverage_report.py [--config DIR] [--data DIR] [--out FILE] [--json]
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import glob
import json
import sys
from pathlib import Path

ENGAGEMENTS = ("contract", "interim", "ftc", "unknown", "permanent")
CONTRACT_SHAPED = ("contract", "interim", "ftc")


def load_scan_rows(data_dir: Path) -> list[dict]:
    """Every job candidate ever surfaced, deduplicated by URL, newest file last."""
    rows: dict[str, dict] = {}
    for path in sorted(glob.glob(str(data_dir / "pending" / "*.tsv"))):
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                if r.get("url"):
                    rows[r["url"]] = r
    return list(rows.values())


def load_tender_rows(data_dir: Path) -> list[dict]:
    rows: dict[str, dict] = {}
    for path in sorted(glob.glob(str(data_dir / "pending" / "tenders" / "*.tsv"))):
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                # A procurement process (ocid) can publish multiple notices/releases. Count each
                # source-scoped release once; retain the legacy ocid fallback for old archives.
                source = r.get("source", "")
                identity = r.get("source_key") or (f"{source}:{r['release_id']}" if r.get("release_id")
                                                    else f"{source}:{r.get('ocid', '')}")
                if identity and identity != f"{source}:":
                    rows[identity] = r
    return list(rows.values())


def last_run(data_dir: Path, sub: str = "") -> dict:
    p = data_dir / "pending" / sub / "last-run.json" if sub else data_dir / "pending" / "last-run.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def load_observed_items(data_dir: Path) -> list[dict]:
    """Read the current observation inventory once for channel-specific measurements."""
    try:
        payload = json.loads((data_dir / "pending" / "observations.json").read_text())
    except (OSError, ValueError):
        return []
    return list((payload.get('items') or {}).values())


def load_observed_jobs(data_dir: Path, items=None) -> list[dict]:
    """Current source facts from lifecycle observations, excluding tender and award notices."""
    rows = []
    for item in load_observed_items(data_dir) if items is None else items:
        if item.get("kind") != "job":
            continue
        current = dict(item.get("current") or {})
        if item.get("provider") == "hn_algolia" and current.get("eligible") is not True:
            continue
        current.setdefault("provider", item.get("provider", ""))
        rows.append(current)
    return rows


def measure(channel: dict, scan_rows: list[dict], tender_rows: list[dict], award_rows=()) -> dict:
    """Count what this channel has actually produced. Absent runtime means absent yield, not zero."""
    cid = channel["id"]
    if cid == 'awarded-suppliers':
        if not award_rows:
            return {}
        return {'surfaced': len(award_rows), 'eligible_signals': sum(r.get('eligible') is True for r in award_rows)}
    if cid == "public-tenders":
        if not tender_rows:
            return {}
        live = [r for r in tender_rows if r.get("deadline", "") >= dt.date.today().isoformat() or not r.get("deadline")]
        return {"surfaced": len(tender_rows), "contract_shaped": len(tender_rows),
                "with_terms": sum(1 for r in tender_rows if r.get("value_amount")),
                "live": len(live)}
    providers = (channel.get("measure") or {}).get("provider_in")
    if not providers:
        return {}
    rows = [r for r in scan_rows if r.get("provider") in providers]
    if not rows:
        return {}
    eng = collections.Counter(r.get("engagement", "") for r in rows)
    return {"surfaced": len(rows),
            "contract_shaped": sum(eng[e] for e in CONTRACT_SHAPED),
            "with_terms": sum(1 for r in rows if (r.get("pay_period") == "day")),
            "by_engagement": {e: eng[e] for e in ENGAGEMENTS if eng[e]}}


STATUS_MARK = {"measured": "scanner output measured", "automated": "scanner output measured",
               "adapter-unprobed": "adapter built, unprobed",
               "manual-only": "only when someone looks", "uncovered": "not read at all",
               "blocked": "needs a decision", "deferred": "parked by decision"}


def build(config_dir: Path, data_dir: Path) -> dict:
    import yaml
    cfg = config_dir
    channels = (yaml.safe_load((cfg / "channels.yml").read_text()) or {}).get("channels", [])
    portals = yaml.safe_load((cfg / "portals.yml").read_text()) or {}
    data = data_dir
    scan_rows, tender_rows = load_scan_rows(data), load_tender_rows(data)
    observed = load_observed_items(data)
    current_jobs = [r for r in load_observed_jobs(data, observed) if r.get("provider") == "hn_algolia"]
    awards = [item.get('current') or {} for item in observed if item.get('kind') == 'commercial_signal']

    enabled_feeds = [f["name"] for f in portals.get("feeds", []) if f.get("enabled")]
    enabled_tenders = [s["name"] for s in (portals.get("tenders") or {}).get("sources", []) if s.get("enabled")]
    award_config = portals.get('commercial_awards') or {}
    enabled_awards = [s['name'] for s in award_config.get('sources', []) if s.get('enabled') and award_config.get('enabled', True)]
    boards = portals.get("tracked_companies", [])

    out = {
        "generated": dt.date.today().isoformat(),
        "config": {
            "tracked_boards_enabled": sum(1 for b in boards if b.get("enabled", True)),
            "tracked_boards_total": len(boards),
            "feeds_enabled": enabled_feeds,
            "feeds_total": len(portals.get("feeds", [])),
            "tender_sources_enabled": enabled_tenders,
            "tender_sources_total": len((portals.get("tenders") or {}).get("sources", [])),
            'award_sources_enabled': enabled_awards,
            'award_sources_total': len(award_config.get('sources', [])),
            "search_queries_never_read": len(portals.get("search_queries", [])),
        },
        "last_job_run": last_run(data), "last_tender_run": last_run(data, "tenders"),
        'last_award_run': last_run(data, 'awards'),
        "channels": [],
    }
    for c in channels:
        # The registry declares `runs`, `finds` and `gives`; older channel maps used status, market and notes.
        status = c.get("status") or c.get("runs")
        report_status = "measured" if status == "automated" else status
        out["channels"].append({**{k: c.get(k) for k in ("id", "name", "access")}, "market": c.get("market") or [c.get("finds")],
                                "status": report_status,
                                "yield": measure(c, scan_rows + current_jobs, tender_rows, awards),
                                "notes": (c.get("notes") or c.get("gives") or "").strip()})

    all_jobs = scan_rows + current_jobs
    totals = collections.Counter(r.get("engagement", "") for r in all_jobs)
    out["totals"] = {
        "job_candidates": len(all_jobs),
        "by_engagement": {e: totals[e] for e in ENGAGEMENTS if totals[e]},
        "contract_shaped": sum(totals[e] for e in CONTRACT_SHAPED),
        "with_a_day_rate": sum(1 for r in all_jobs if r.get("pay_period") == "day"),
        "tender_notices": len(tender_rows),
        'commercial_signals': len(awards),
        "channels_by_status": dict(collections.Counter(c["status"] for c in out["channels"])),
    }
    return out


def render(rep: dict) -> str:
    c, t = rep["config"], rep["totals"]
    L = [f"# Channel coverage — {rep['generated']}", "",
         "What the career system can see, measured from its own run output. A channel with no",
         "runtime produces nothing, however many records sit next to it.", "",
         "## Reach", "",
         "| | |", "| --- | --- |",
         f"| Job candidates ever surfaced | {t['job_candidates']} |",
         f"| …contract, interim or FTC | **{t['contract_shaped']}** |",
         f"| …carrying a day rate | **{t['with_a_day_rate']}** |",
         f"| Tender notices ever surfaced | {t['tender_notices']} |",
         f"| Supplier award signals observed | {t['commercial_signals']} |",
         f"| Employer boards enabled | {c['tracked_boards_enabled']} of {c['tracked_boards_total']} |",
         f"| Contract feeds enabled | {len(c['feeds_enabled'])} of {c['feeds_total']} |",
         f"| Tender sources enabled | {len(c['tender_sources_enabled'])} of {c['tender_sources_total']} |",
         f"| Award sources enabled | {len(c['award_sources_enabled'])} of {c['award_sources_total']} |",
         f"| Saved search queries no runtime reads | {c['search_queries_never_read']} |", ""]
    if t["by_engagement"]:
        L += ["Engagement mix of everything surfaced so far: "
              + ", ".join(f"{k} {v}" for k, v in t["by_engagement"].items()) + ".", ""]
    L += ["## Channels", "",
          "| Channel | Market | Status | Surfaced | Contract-shaped |", "| --- | --- | --- | --- | --- |"]
    for ch in rep["channels"]:
        y = ch["yield"]
        surfaced = str(y.get("surfaced", "")) if y else "—"
        shaped = str(y.get("contract_shaped", "")) if y else "—"
        L.append(f"| {ch['name']} | {', '.join(ch['market'] or [])} | {STATUS_MARK.get(ch['status'], ch['status'])} "
                 f"| {surfaced} | {shaped} |")
    L += ["", "A dash means there is no measured surfaced output; see the named access or coverage gap.", "", "## Where the gaps are", ""]
    for ch in rep["channels"]:
        if ch["status"] in ("automated", "measured"):
            continue
        L += [f"**{ch['name']}** — {STATUS_MARK.get(ch['status'], ch['status'])}.", "", ch["notes"], ""]
    return "\n".join(L).rstrip() + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--config", type=Path, default=root / "config",
                    help="scanner/channel configuration directory")
    ap.add_argument("--data", type=Path, default=root / "data",
                    help="scanner output directory (default: repository data)")
    ap.add_argument("--out", type=Path, default=None, help="write the Markdown report here")
    ap.add_argument("--json", action="store_true", help="print the measurements as JSON instead")
    a = ap.parse_args(argv)
    rep = build(a.config, a.data)
    if a.json:
        print(json.dumps(rep, indent=2))
        return 0
    text = render(rep)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text)
        print(f"wrote {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
