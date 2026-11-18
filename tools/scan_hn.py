#!/usr/bin/env python3
"""Read the current Hacker News Who is hiring thread through the public Algolia API.

The adapter is intentionally manual: it creates a discovery archive and source observations but
does not contact posters, create applications, or imply that every comment is a suitable role.
"""
from __future__ import annotations

import argparse
import collection_schedule
import collection_budget
import datetime as dt
import json
import html
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pending_store import ObservationStore, PendingStore  # noqa: E402
from job_rules import engagement_of, location_ok, title_match  # noqa: E402
from web_read import failure, fetch_json, strip_html  # noqa: E402

COLUMNS = ["date_seen", "thread_id", "comment_id", "company", "title", "location",
           "engagement", "posted", "url", "summary"]


def _search_url(query: str, tags: str, page: int = 0, hits: int = 100) -> str:
    return "https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode(
        {"query": query, "tags": tags, "page": page, "hitsPerPage": hits})


class ThreadNotFound(RuntimeError):
    """No Who is hiring thread for that month yet: HN posts it on the first weekday, about 15:00 UTC."""


def find_thread(month: str) -> dict:
    payload = fetch_json(_search_url(f"Ask HN: Who is hiring? ({month})", "story", hits=20))
    hits = payload.get("hits") or []
    exact = [h for h in hits if f"who is hiring? ({month.lower()})" in (h.get("title") or "").lower()]
    if not exact:
        raise ThreadNotFound(f"Who is hiring thread not found for {month}")
    return exact[0]


def current_thread(today: str, pinned: str = "") -> tuple[dict, str]:
    """This month's thread; in the first week, last month's until the new one is posted, so the daily
    run does not report a failure at the start of every month."""
    if pinned:
        return find_thread(pinned), pinned
    day = dt.date.fromisoformat(today)
    month = day.strftime("%B %Y")
    try:
        return find_thread(month), month
    except ThreadNotFound:
        if day.day > 7:
            raise
        previous = (day.replace(day=1) - dt.timedelta(days=1)).strftime("%B %Y")
        return find_thread(previous), previous


def fetch_comments(thread_id: str, max_pages: int = 10) -> dict:
    rows, complete = [], False
    for page in range(max_pages):
        try:
            payload = fetch_json(_search_url("", f"comment,story_{thread_id}", page=page))
        except collection_budget.Deferred:
            if not rows: raise
            return {"hits": rows, "_complete": False, "_pages": page, "_stop_reason": "budget"}
        batch = payload.get("hits") or []
        rows.extend(batch)
        if page + 1 >= int(payload.get("nbPages", 0)):
            complete = True
            break
    return {"hits": rows, "_complete": complete, "_pages": page + 1}


def normalise(thread_id: str, hit: dict) -> dict:
    raw=hit.get('comment_text') or ''
    text=' '.join(html.unescape(strip_html(raw)).split())
    header=next((html.unescape(strip_html(x)).strip() for x in re.split(r'<p>|<br\s*/?>|\n',raw,flags=re.I) if strip_html(x).strip()),text)
    parts=[p.strip() for p in header.split('|') if p.strip()]
    first=(parts[0] if parts else header)[:120]
    # Apply links and URLs often contain role words ("…/jobs/123-ai-engineer"); they are never the title.
    roles=[p for p in parts[1:] if re.search(r'\b(?:engineer\w*|developer|manager|lead|director|cto|founding|quality|reliability)\b',p,re.I)
           and not re.search(r'https?://|www\.|\bapply\b|\.(?:com|io|dev|ai|co)/',p,re.I)]
    cid=str(hit.get('objectID') or '')
    return dict(thread_id=str(thread_id),comment_id=cid,company=first,
        title=(' | '.join(roles) if roles else ' | '.join(parts[1:]) or first)[:160],
        location=' | '.join(parts[1:])[:400],posted=(hit.get('created_at') or '')[:10],
        parent_id=str(hit.get('parent_id') or ''),url=f'https://news.ycombinator.com/item?id={cid}' if cid else '',summary=text[:2000])


def uk_route(job):
    """Preserve geographic restrictions before considering broad Remote labels."""
    header=job.get('location','').lower()
    restrictions=job.get('summary','')[:1000].lower()
    if re.search(r'(?:us|usa|united states)[ -]*(?:based|only)|(?:based in|resident in|reside in|located in) (?:the )?(?:us|usa|united states)\b|us citizens',restrictions):
        return False,'UK eligibility rejected: US restriction'
    uk=bool(re.search(r'\b(?:uk|united kingdom|london|england|scotland|wales)\b',header))
    if uk:return True,'UK location stated'
    if re.search(r'\b(?:onsite|on-site|hybrid)\b',header):return False,'UK eligibility rejected: overseas attendance'
    if re.search(r'\b(?:us|usa|united states|nyc|new york|san francisco|bay area|california|boston|canada|india)\b',header) and not re.search(r'worldwide|global|emea|europe',header):
        return False,'UK eligibility rejected: restricted foreign location'
    if re.search(r'\bEU[- ]only|EU residents',header,re.I):return False,'UK eligibility not established: EU residence required'
    if re.search(r'remote|worldwide|global',header):return True,'Remote stated; UK eligibility and hours require review'
    return False,'UK or remote route not established'


def run(portals_path: Path, out_dir: Path, today: str, fixture: Path | None = None) -> dict:
    import yaml
    cfg = (yaml.safe_load(portals_path.read_text()) or {}).get("hn_who_is_hiring") or {}
    whole_cfg = yaml.safe_load(portals_path.read_text()) or {}
    month = cfg.get("month") or dt.date.fromisoformat(today).strftime("%B %Y")
    scheduler = collection_schedule.Session(out_dir, offline=fixture is not None)
    if not scheduler.choose('hn_algolia',['hn:current'],background=True):
        return scheduler.receipt('hn_algolia')
    if fixture:
        payload = json.loads(fixture.read_text())
        thread, comments = payload["thread"], payload["comments"]
    else:
        def read():
            thread, month = current_thread(today, cfg.get('month') or '')
            comments = fetch_comments(str(thread['objectID']), int(cfg.get('max_pages',10)))
            return thread,month,comments
        try:thread,month,comments = scheduler.call('hn_algolia','hn:current',read)
        except collection_budget.Deferred as error:return scheduler.receipt('hn_algolia',error.reason)
    tid = str(thread["objectID"])
    jobs = [normalise(tid, h) for h in comments.get("hits", [])]
    for job in jobs:
        title_ok, title_reason = title_match(job['title'], whole_cfg.get("title_filter", {}))
        location_okay, location_reason = location_ok(job["location"], job["summary"], whole_cfg.get("location_filter", {}))
        job["engagement"] = engagement_of({"title": job["title"], "location": job["location"],
                                            "employment_type": "", "text": job["summary"]},
                                           whole_cfg.get("engagement_filter", {}))
        route_ok,route_reason=uk_route(job)
        top_level = job["parent_id"] == tid
        job["eligible"] = bool(top_level and title_ok and location_okay and route_ok)
        job["eligibility_reason"] = f"{'top-level' if top_level else 'reply-or-unknown-parent'}; {title_reason}; {location_reason}; {route_reason}"
    store = PendingStore(out_dir / "pending" / "hn", COLUMNS, "comment_id")
    rows, seen = store.resume(today)
    new = 0
    for row in jobs:
        if not row["eligible"]:
            continue
        if row["comment_id"] and row["comment_id"] not in seen:
            seen.add(row["comment_id"])
            rows.append({"date_seen": today, **row})
            new += 1
    complete = bool(comments.get("_complete"))
    status = {"date": today, "thread_id": tid, "state": "ok" if complete else "partial",
              "complete": complete, "comments": len(jobs), "eligible": sum(bool(j["eligible"]) for j in jobs),
              "new": new}
    store.save(today, rows, seen, status)
    ObservationStore(out_dir / "pending" / "observations.json").observe(
        scope=f"hn:who-is-hiring:{tid}", source_type="job", provider="hn_algolia",
        observed_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        state=status["state"], complete=complete,
        items=[{"source_id": r["comment_id"], "current": r} for r in jobs],
        detail={"thread_id": tid, "month": month, "pages": comments.get("_pages")})
    return status


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--portals", type=Path, default=root / "config/portals.yml")
    ap.add_argument("--out", type=Path, default=root / "data")
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--offline-fixture", type=Path)
    a = ap.parse_args(argv)
    try:
        result=run(a.portals,a.out,a.today,a.offline_fixture)
    except Exception as error:
        ObservationStore(a.out/'pending/observations.json').observe(scope='hn:who-is-hiring:discovery',source_type='job',provider='hn_algolia',
            observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),state='error',complete=False,items=[],detail=failure(error))
        raise
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
