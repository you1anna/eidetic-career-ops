#!/usr/bin/env python3
"""Daily ATS board scan for the career acquisition workflow.

Reads the user-layer ``portals.yml`` (tracked_companies, title_filter, engagement_filter,
location_filter), calls the public job-board APIs for Greenhouse, Lever, Ashby and SmartRecruiters,
filters by rule, deduplicates against everything already seen, and writes:

  <out>/pending/YYYY-MM-DD.tsv   candidates first surfaced that day, retained across retries
  <out>/pending/seen.json        every job URL ever surfaced, so a job is proposed once
  <out>/pending/last-run.json    per-board status (including partial reads) and totals for this run

Each row carries structured pay (min, max, currency, period, and whether it came from the ATS or
the advert text), workplace (remote / hybrid / onsite), the ATS employment type, department and
IR35 status where stated.

No model calls. No sends. No canonical record writes. Approved 2026-09-14
(command-center/decisions/2026-09-14-expansion-answers.md). Triage remains attended: a row becomes
a report only through the existing career-ops allocator.

Usage: scan_boards.py [--portals PATH] [--out DIR] [--offline-fixture DIR] [--today YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'workspace'))
from normalise import board_identity as detect  # noqa: E402
from web_read import USER_AGENT, failure, failure_kind, fetch_json, fetch_text, single_attempt, strip_html as _strip_html  # noqa: E402
from job_rules import (  # noqa: E402
    day_rate, ir35_of, location_ok, observed_terms, qualification, text_pay, title_match)
from pending_store import PROTECTION, ObservationStore, PendingStore, Protection, atomic_write  # noqa: E402

COLUMNS = ["date_seen", "company", "title", "engagement", "rate", "location", "posted", "url", "provider", "why",
           "pay_min", "pay_max", "pay_currency", "pay_period", "pay_source", "workplace", "employment_type", "department", "ir35"]
SMARTRECRUITERS_PAGE = 100
SMARTRECRUITERS_MAX_PAGES = 10


# ---------------------------------------------------------------------------------------------
# Provider detection and fetching

def api_url(provider: str, slug: str) -> str:
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&pay_transparency=true",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
        "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={SMARTRECRUITERS_PAGE}",
        "rippling": f"https://ats.rippling.com/{slug}/jobs",
    }[provider]


def posting_api_url(provider: str, slug: str, posting_id: str) -> str:
    """One advert's own API address; Ashby publishes only the whole board, so its board is read."""
    slug = urllib.parse.quote(slug, safe="")
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{posting_id}",
        "lever": f"https://api.lever.co/v0/postings/{slug}/{posting_id}?mode=json",
    }.get(provider) or api_url(provider, slug)


def rippling_complete(payload):
    """Only a validated whole first page establishes absence; larger boards stay partial."""
    return payload['page'] == 0 and len(payload['items']) == payload['totalItems'] and payload['totalPages'] <= 1


def _validate_rippling(payload, slug=None):
    if (not isinstance(payload, dict) or not isinstance(payload.get('items'), list)
            or any(type(payload.get(k)) is not int or payload[k] < 0
                   for k in ('page', 'pageSize', 'totalItems', 'totalPages'))
            or payload['page'] != 0 or not 1 <= payload['pageSize'] <= 100
            or len(payload['items']) > min(payload['totalItems'], payload['pageSize'])):
        raise ValueError('Unsupported Rippling board pagination')
    expected_pages = (payload['totalItems'] + payload['pageSize'] - 1) // payload['pageSize']
    if payload['totalPages'] not in ({0, 1} if not payload['totalItems'] else {expected_pages}):
        raise ValueError('Contradictory Rippling board pagination')
    ids = set()
    for item in payload['items']:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', item['id'])
                or item['id'] in ids or not isinstance(item.get('name'), str) or not item['name'].strip()
                or not isinstance(item.get('url'), str)):
            raise ValueError('Unsupported Rippling job identity')
        provider, board = detect(item['url'])
        if (provider != 'rippling' or (slug and slug != board)
                or item['url'] != f'https://ats.rippling.com/{board}/jobs/{item["id"]}'):
            raise ValueError('Unexpected Rippling job URL')
        locations = item.get('locations')
        if not isinstance(locations, list) or any(not isinstance(loc, dict) or not isinstance(loc.get('name'), str)
                                                 for loc in locations):
            raise ValueError('Unsupported Rippling job locations')
        if not isinstance(item.get('department', {}), dict):
            raise ValueError('Unsupported Rippling department')
        ids.add(item['id'])
    return payload


def fetch_board(provider: str, slug: str):
    if provider == 'rippling':
        # Server-rendered public board, one bounded request; do not guess private API endpoints.
        html = fetch_text(api_url(provider, slug))
        match = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
        if not match:
            raise ValueError('Rippling board data missing')
        try:
            queries = json.loads(match[1])['props']['pageProps']['dehydratedState']['queries']
            matches = [q for q in queries if q.get('queryKey', [])[:3] == ['board', slug, 'job-posts']]
            pages = []
            for query in matches:
                key = query['queryKey']
                filters = key[4] if len(key) == 5 else None
                if (len(key) != 5 or key[3] is not False or not isinstance(filters, dict)
                        or filters.get('page') != 0
                        or any(filters.get(k) for k in ('searchQuery', 'departments', 'workplaceType', 'country', 'state', 'city'))
                        or set(filters) - {'searchQuery', 'departments', 'workplaceType', 'country', 'state', 'city', 'page', 'pageSize'}):
                    raise ValueError('Filtered Rippling page cannot establish board coverage')
                pages.append(query['state']['data'])
            if len(pages) != 1:
                raise ValueError('Rippling job page missing or ambiguous')
            return _validate_rippling(pages[0], slug)
        except (KeyError, TypeError) as exc:
            raise ValueError('Unsupported Rippling board data') from exc
    if provider != "smartrecruiters":
        return fetch_json(api_url(provider, slug))
    # SmartRecruiters answers 200 with totalFound 0 for unknown companies, and pages at 100.
    page = fetch_json(api_url(provider, slug))
    content = list(page.get("content", []))
    total = page.get("totalFound", len(content))
    offset = len(content)
    pages = 1
    while offset < total and pages < SMARTRECRUITERS_MAX_PAGES:
        try:
            more = fetch_json(f"{api_url(provider, slug)}&offset={offset}").get("content", [])
        except collection_budget.Deferred:
            page['_stop_reason']='budget'
            break
        if not more:
            break
        content.extend(more)
        offset += len(more)
        pages += 1
    page["content"] = content
    return page


# ---------------------------------------------------------------------------------------------
# Query feeds: whole-market sources, as opposed to one named employer's board.
#
# ATS boards carry an employer's own headcount, which in the UK is overwhelmingly permanent. UK
# contract work is brokered, and reaches the market through aggregators and agency desks instead.
# A feed is therefore queried by role and engagement, not by company, and the company name arrives
# in the payload. Credentials come from the environment or the service's credential directory
# (tools/credentials.py); none are stored in this repository.

import collection_policy
import collection_schedule
import collection_budget


def feed_limits():
    return {k: dict(max_calls=v['daily_limit'], min_interval_seconds=v['min_interval_seconds'])
            for k,v in collection_policy.load()['providers'].items() if k in ('adzuna','reed')}


FEED_PROVIDERS = ("adzuna", "reed")


class FeedConfigError(RuntimeError):
    """A feed is enabled but cannot be called — missing credential or unusable params."""


def _env(name: str) -> str:
    import credentials
    v = credentials.get(name)
    if not v:
        raise FeedConfigError(f"credential {name} is not set")
    return v


def feed_request(provider: str, params: dict, page: int, phrase: str | None = None) -> tuple[str, dict]:
    """Return (url, headers) for one page of a feed. Page numbering is the provider's own. ``phrase``
    is one exact role phrase from the feed's ``phrases`` list."""
    q = dict(params or {})
    if provider == "adzuna":
        country = q.get("country", "gb")
        args = {
            "app_id": _env(q.get("app_id_env", "ADZUNA_APP_ID")),
            "app_key": _env(q.get("app_key_env", "ADZUNA_APP_KEY")),
            "results_per_page": str(q.get("results_per_page", 50)),
            "content-type": "application/json",
        }
        for src, dest in (("what", "what"), ("what_or", "what_or"), ("what_exclude", "what_exclude"),
                          ("where", "where"), ("sort_by", "sort_by"),
                          ("max_days_old", "max_days_old"), ("distance", "distance"), ("title_only", "title_only"),
                          ("category", "category"), ("salary_min", "salary_min")):
            if q.get(src) not in (None, ""):
                args[dest] = str(q[src])
        # Adzuna filters engagement with flags; contract_type is a result field, not a filter.
        for flag in ("contract", "permanent", "full_time", "part_time"):
            if q.get(flag):
                args[flag] = "1"
        if phrase:
            args["what_phrase"] = phrase
        return (f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?" + urllib.parse.urlencode(args), {})
    if provider == "reed":
        take = int(q.get("results_per_page", 100))
        args = {"resultsToTake": str(take), "resultsToSkip": str((page - 1) * take)}
        for src, dest in (("keywords", "keywords"), ("where", "locationName"),
                          ("distance_miles", "distanceFromLocation"), ("salary_min", "minimumSalary")):
            if q.get(src) not in (None, ""):
                args[dest] = str(q[src])
        if phrase:
            args["keywords"] = phrase
        for flag in ("contract", "temp", "partTime", "fullTime", "permanent"):
            if flag in q:
                args[flag] = "true" if q[flag] else "false"
        key = _env(q.get("api_key_env", "REED_API_KEY"))
        auth = base64.b64encode(f"{key}:".encode()).decode()
        return ("https://www.reed.co.uk/api/1.0/search?" + urllib.parse.urlencode(args),
                {"Authorization": f"Basic {auth}"})
    raise FeedConfigError(f"unknown feed provider: {provider}")


class FeedStopped(RuntimeError):
    """Stop this provider for the rest of the run; never retry a denied request."""

    def __init__(self, state):
        self.state = state
        super().__init__(state)


class FeedRequests:
    """Per-run provider budgets and pacing shared by every search and detail request.

    These are local caps, not a claim about the account's remaining daily/monthly allowance.
    Count attempted requests, including failures; credentials never enter the receipt.
    """

    def __init__(self, limits):
        self.limits = limits
        self.calls = {}
        self.last = {}
        self.stopped = {}

    def read(self, provider, url, headers=None):
        if provider in self.stopped:
            raise FeedStopped(self.stopped[provider])
        limits = self.limits.get(provider)
        if not isinstance(limits, dict) or not {"max_calls", "min_interval_seconds"} <= limits.keys():
            raise FeedConfigError(f"explicit call cap and pacing required for {provider}")
        cap = int(limits["max_calls"])
        if self.calls.get(provider, 0) >= cap:
            self.stopped[provider] = "budget"
            raise FeedStopped("budget")
        interval = max(0, float(limits["min_interval_seconds"]))
        if provider in self.last:
            time.sleep(max(0, interval - (time.monotonic() - self.last[provider])))
        self.last[provider] = time.monotonic()
        self.calls[provider] = self.calls.get(provider, 0) + 1
        try:
            with single_attempt():  # every attempt spends quota, so the budget counts each one
                return fetch_json(url, headers)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 429):
                state = f"http-{error.code}"
                self.stopped[provider] = state
                raise FeedStopped(state) from None
            raise


def fetch_feed(provider: str, params: dict, cache: dict | None = None, today: str = "",
               wants_detail=None, budget: dict | None = None, request=None) -> dict:
    """Read each search's first page before later pages, within the shared request cap.

    Returns the provider payload with all pages concatenated, plus ``_pages``, ``_complete`` and
    ``_calls`` (requests made, for the provider quota). ``_complete`` is False when paging stopped at
    the cap with more results outstanding, so a truncated read is never reported as a whole view of
    the market. ``cache`` holds Reed detail reads by job ID; a detail is read again only when the
    search summary changes (application counts alone do not invalidate it). ``wants_detail`` skips details for adverts the local rules would
    reject, and ``budget`` caps detail reads per run; the rest are read on a later run.
    """
    max_pages = int((params or {}).get("max_pages", 4))
    request = request or fetch_json
    raw_request = request
    def request(url, headers):
        key=(params or {}).get('_query_keys',{}).get(query['phrase'])
        if key:
            with collection_budget.task(key): return raw_request(url,headers)
        return raw_request(url,headers)
    cache = {} if cache is None else cache
    merged: dict = {}
    pages = calls = 0
    completed_queries = set()
    queries = [dict(phrase=p, count=0, total=None, done=False) for p in (params or {}).get('phrases') or [None]]

    def finish(stop_reason=None):
        complete = not stop_reason and all(q['total'] is not None and q['count'] >= q['total'] for q in queries)
        return dict(results=list(merged.values()), _pages=pages, _total=sum(q['total'] or 0 for q in queries),
                    _complete=complete, _calls=calls, _queries=sorted(completed_queries), **({'_stop_reason': stop_reason} if stop_reason else {}))

    for page in range(1, max_pages + 1):
        for query in queries:
            if query['done']:
                continue
            url, headers = feed_request(provider, params, page, phrase=query['phrase'])
            try:
                payload = request(url, headers)
            except collection_budget.Deferred as error:
                if not pages: raise
                return finish('budget')
            except FeedStopped as error:
                # Local allowance exhaustion cannot discard successful earlier pages.
                # Access/rate refusals retain their failure semantics and stop the provider.
                if error.state != 'budget' or not pages:
                    raise
                return finish('budget')
            pages += 1
            calls += 1
            stopped = False
            if provider == "adzuna":
                batch = payload.get("results") or []
                query['total'] = payload.get("count", query['total'])
            else:
                batch = payload.get("results") or []
                query['total'] = payload.get("totalResults", query['total'])
                # Search results are summaries. Fetch documented job details so description, salary
                # period, contract type, expiry and canonical URL are source facts rather than guesses.
                enriched = []
                for index, summary in enumerate(batch):
                    jid = summary.get("jobId") if isinstance(summary, dict) else None
                    if not jid:
                        enriched.append(summary)
                        continue
                    cached = cache.get(str(jid))
                    fingerprint = {k: v for k, v in summary.items() if k != "applications"}
                    if cached and cached.get("summary") == fingerprint:
                        cached["seen"] = today or cached.get("seen", "")
                        enriched.append({**summary, **cached["detail"]})
                        continue
                    if (wants_detail and not wants_detail(summary)) or (budget is not None and budget.get("left", 0) <= 0):
                        enriched.append(summary)
                        continue
                    try:
                        if budget is not None:
                            budget["left"] -= 1
                        detail = request(f"https://www.reed.co.uk/api/1.0/jobs/{jid}", headers)
                        if not isinstance(detail, dict):
                            raise ValueError('Malformed Reed detail')
                        cache[str(jid)] = {"summary": fingerprint, "detail": detail, "seen": today}
                        enriched.append({**summary, **detail})
                    except collection_budget.Deferred:
                        enriched.extend(batch[index:]); stopped = True; break
                    except FeedStopped as error:
                        if error.state != 'budget':
                            raise
                        # These summaries were already read. Keep them without inventing details.
                        enriched.extend(batch[index:])
                        stopped = True
                        break
                    except Exception:
                        enriched.append(summary)
                    calls += 1
                batch = enriched
            query_key=params.get('_query_keys',{}).get(query['phrase'])
            if query_key:completed_queries.add(query_key)
            query['count'] += len(batch)
            query['done'] = not batch or (query['total'] is not None and query['count'] >= query['total'])
            for item in batch:
                if params.get('_query_keys'):
                    item = dict(item, _query_keys=[params['_query_keys'].get(query['phrase'])])
                key = str(item.get("id") or item.get("jobId") or item.get("redirect_url") or item.get("jobUrl") or len(merged))
                if key in merged and item.get('_query_keys'):
                    merged[key]['_query_keys']=sorted(set(merged[key].get('_query_keys',[])+item['_query_keys']))
                else:merged.setdefault(key, item)
            if stopped:
                return finish('budget')
    return finish()


# ---------------------------------------------------------------------------------------------
# Normalisation: every provider becomes {title, location, url, posted, text, rate, pay, workplace,
# employment_type, department}

def _period(label: str) -> str:
    s = (label or "").lower()
    for key, period in (("hour", "hour"), ("day", "day"), ("daily", "day"), ("week", "week"), ("month", "month"), ("year", "year"), ("annual", "year"), ("salary", "year")):
        if key in s:
            return period
    return ""


def _workplace(value) -> str:
    s = (value or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    if "remote" in s:
        return "remote"
    if "hybrid" in s:
        return "hybrid"
    if "onsite" in s or "office" in s:
        return "onsite"
    return ""


def _pay(lo, hi, currency, period):
    if lo is None and hi is None:
        return None
    lo = hi if lo is None else lo
    hi = lo if hi is None else hi
    return (float(lo), float(hi), (currency or "").upper(), period or "year")


def normalise(provider: str, payload) -> list[dict]:
    jobs = []
    if provider == 'rippling':
        for j in _validate_rippling(payload)['items']:
            locations = j['locations']
            workplaces = {_workplace(loc.get('workplaceType', '').replace('_', ' ')) for loc in locations}
            jobs.append(dict(source_id=j['id'], title=j['name'].strip(),
                             location=' / '.join(loc['name'] for loc in locations), url=j['url'],
                             text='', posted='', employment_type='',
                             department=j.get('department', {}).get('name', ''),
                             workplace=next(iter(workplaces)) if len(workplaces) == 1 else ''))
    elif provider == "greenhouse":
        for j in payload.get("jobs", []):
            pay = None
            for r in j.get("pay_input_ranges") or []:
                if r.get("min_cents") is not None or r.get("max_cents") is not None:
                    lo, hi = r.get("min_cents"), r.get("max_cents")
                    pay = _pay(lo / 100 if lo is not None else None, hi / 100 if hi is not None else None, r.get("currency_type"), _period(r.get("title", "")))
                    break
            meta = {(m.get("name") or "").lower(): m.get("value") for m in j.get("metadata") or []}
            workplace = next((_workplace(v) for k, v in meta.items() if isinstance(v, str) and ("location type" in k or "workplace" in k or "remote" in k)), "")
            employment = next((v for k, v in meta.items() if isinstance(v, str) and "employment" in k), "")
            jobs.append({
                "source_id": str(j.get("id", "")),
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "url": j.get("absolute_url", ""),
                "posted": (j.get("updated_at") or j.get("first_published") or "")[:10],
                "text": _strip_html(j.get("content", "")),
                "rate": "",
                "pay": pay,
                "workplace": workplace,
                "employment_type": employment,
                "department": ((j.get("departments") or [{}])[0] or {}).get("name", ""),
            })
    elif provider == "lever":
        for j in payload if isinstance(payload, list) else []:
            cats = j.get("categories") or {}
            created = j.get("createdAt")
            posted = dt.datetime.fromtimestamp(created / 1000, dt.timezone.utc).date().isoformat() if created else ""
            sr = j.get("salaryRange") or {}
            jobs.append({
                "source_id": str(j.get("id", "")),
                "title": j.get("text", ""),
                "location": " / ".join(filter(None, [cats.get("location", ""), cats.get("commitment", "")])),
                "url": j.get("hostedUrl", ""),
                "posted": posted,
                "text": _strip_html(j.get("descriptionPlain") or j.get("description", "")),
                "rate": "",
                "pay": _pay(sr.get("min"), sr.get("max"), sr.get("currency"), _period(sr.get("interval", ""))) if sr else None,
                "workplace": _workplace(j.get("workplaceType")),
                "employment_type": cats.get("commitment", ""),
                "department": cats.get("department") or cats.get("team") or "",
            })
    elif provider == "ashby":
        for j in payload.get("jobs", []):
            comp = j.get("compensation") or {}
            rate = comp.get("compensationTierSummary") or ""
            components = comp.get("summaryComponents") or [c for t in comp.get("compensationTiers") or [] for c in t.get("components") or []]
            salary = next((c for c in components if c.get("compensationType") == "Salary" and (c.get("minValue") is not None or c.get("maxValue") is not None)), None)
            loc = j.get("location", "")
            workplace = _workplace(j.get("workplaceType")) or ("remote" if j.get("isRemote") else "")
            if workplace == "remote":
                loc = f"{loc} / Remote".strip(" /")
            jobs.append({
                "source_id": str(j.get("id", "")),
                "title": j.get("title", ""),
                "location": loc,
                "url": j.get("jobUrl", ""),
                "posted": (j.get("publishedAt") or "")[:10],
                "text": j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", "")),
                "rate": rate,
                "pay": _pay(salary.get("minValue"), salary.get("maxValue"), salary.get("currencyCode"), _period(salary.get("interval", ""))) if salary else None,
                "workplace": workplace,
                "employment_type": j.get("employmentType", ""),
                "department": j.get("department") or j.get("team") or "",
            })
    elif provider == "smartrecruiters":
        for j in payload.get("content", []):
            loc = j.get("location") or {}
            full = loc.get("fullLocation") or ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
            if loc.get("remote"):
                full = f"{full} / Remote".strip(" /")
            ident = (j.get("company") or {}).get("identifier", "")
            jobs.append({
                "source_id": str(j.get("id", "")),
                "title": j.get("name", ""),
                "location": full,
                "url": f"https://jobs.smartrecruiters.com/{ident}/{j.get('id')}" if ident and j.get("id") else "",
                "posted": (j.get("releasedDate") or "")[:10],
                "text": "",
                "rate": "",
                "pay": None,
                "workplace": "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else "",
                "employment_type": (j.get("typeOfEmployment") or {}).get("label", ""),
                "department": (j.get("department") or {}).get("label", ""),
            })
    return jobs


def _reed_date(v: str) -> str:
    try:
        return dt.datetime.strptime((v or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return ""


def normalise_feed(provider: str, payload) -> list[dict]:
    """Feed payload -> the same job dict the board providers produce, plus ``company``.

    Neither aggregator states the period its salary figures belong to: Adzuna annualises, and Reed's
    minimum/maximumSalary carry no unit. Those numbers are therefore carried as ``feed_pay`` (amount
    and currency, no period) and never as a day rate. A day rate is only ever taken from the advert
    text by the existing ``text_pay``/``day_rate`` path, so the recorded floor keeps comparing
    like with like.
    """
    jobs = []
    if provider == "adzuna":
        for j in payload.get("results", []):
            lo, hi = j.get("salary_min"), j.get("salary_max")
            jobs.append({
                "source_id": str(j.get("id", "")),
                "company": (j.get("company") or {}).get("display_name", ""),
                "title": j.get("title", "") or "",
                "location": (j.get("location") or {}).get("display_name", ""),
                "url": j.get("redirect_url", ""),
                "posted": (j.get("created") or "")[:10],
                "text": _strip_html(j.get("description", "")),
                "rate": "",
                "pay": None,
                "feed_pay": (float(lo if lo is not None else hi), float(hi if hi is not None else lo), "GBP") if (lo is not None or hi is not None) else None,
                "workplace": "",
                "employment_type": j.get("contract_type", "") or "",
                "department": (j.get("category") or {}).get("label", ""),
            })
    elif provider == "reed":
        for j in payload.get("results", []):
            lo, hi = j.get("minimumSalary"), j.get("maximumSalary")
            jobs.append({
                "source_id": str(j.get("jobId", "")),
                "company": j.get("employerName", "") or "",
                "title": j.get("jobTitle", "") or "",
                "location": j.get("locationName", "") or "",
                "url": j.get("jobUrl") or (f"https://www.reed.co.uk/jobs/{j['jobId']}" if j.get("jobId") else ""),
                "posted": _reed_date(j.get("date", "")),
                "text": _strip_html(j.get("jobDescription", "")),
                "rate": "",
                "pay": None,
                "feed_pay": (float(lo if lo is not None else hi), float(hi if hi is not None else lo), (j.get("currency") or "GBP")) if (lo is not None or hi is not None) else None,
                "workplace": "",
                "employment_type": (j.get("contractType") or "").strip().lower(),
                "expires": _reed_date(j.get("expirationDate", "")),
                "department": "",
            })
    return jobs


# ---------------------------------------------------------------------------------------------

def consider(jobs: list[dict], source: str, provider: str, today: str, rules: dict,
             seen: set, rows: list, status: dict) -> tuple[int, int]:
    """Apply the title, remit, location, engagement and floor rules to one source's jobs.

    Appends surviving candidates to ``rows``, updates ``seen`` and the run totals, and returns
    (title_matched, new). Boards and feeds share this so a candidate means the same thing whichever
    channel found it, and so a job seen on both is recorded once.
    """
    tf, ef, lf = rules["title"], rules["engagement"], rules["location"]
    min_rate, prefer = rules["min_rate"], rules["prefer"]
    status["totals"]["jobs_read"] += len(jobs)
    matched = new = 0
    for job in jobs:
        ok, why = title_match(job["title"], tf)
        if not ok:
            continue
        matched += 1
        qualified = qualification(job)
        if not qualified["eligible"]:
            continue
        lok, lwhy = location_ok(job["location"], job["text"], lf)
        if not lok:
            continue
        if not job["url"] or job["url"] in seen:
            continue
        terms = observed_terms(job, ef)
        eng = terms["engagement"]
        _, rate_hi = day_rate(job)
        if terms["pay_currency"] == "GBP" and terms["pay_period"] == "day":
            structured_hi = float(terms["pay_max"]) if terms["pay_max"] else None
            rate_hi = structured_hi if rate_hi is None else rate_hi
        if rate_hi is not None and min_rate and rate_hi < min_rate:
            continue  # below the recorded floor; do not surface
        seen.add(job["url"])
        new += 1
        status["totals"]["preferred_new"] += int(eng in prefer)
        status["totals"]["by_engagement"][eng] = status["totals"]["by_engagement"].get(eng, 0) + 1
        rows.append({
            "date_seen": today, "company": job.get("company") or source, "title": job["title"], "engagement": eng,
            "rate": terms["rate"], "location": job["location"], "posted": job["posted"],
            "url": job["url"], "provider": provider,
            "why": "; ".join([why, lwhy, *qualified["reasons"], *qualified["conflicts"]]),
            "pay_min": terms["pay_min"], "pay_max": terms["pay_max"],
            "pay_currency": terms["pay_currency"], "pay_period": terms["pay_period"], "pay_source": terms["pay_source"],
            "workplace": terms["workplace"], "employment_type": job.get("employment_type", ""),
            "department": job.get("department", ""), "ir35": terms["ir35"],
        })
    return matched, new


def run(portals_path: Path, out_dir: Path, today: str, fixture_dir: Path | None = None, feeds: bool = True) -> dict:
    try:
        import yaml
    except ImportError:  # pragma: no cover
        print("PyYAML is required: pip install pyyaml", file=sys.stderr)
        raise
    cfg = yaml.safe_load(portals_path.read_text())
    tf = cfg.get("title_filter", {})
    ef = cfg.get("engagement_filter", {})
    lf = cfg.get("location_filter", {})
    prefer = set(ef.get("prefer", ["contract", "interim", "fractional", "ftc"]))
    min_rate = int(ef.get("minimum_day_rate_gbp", 0) or 0)

    store = PendingStore(out_dir / "pending", COLUMNS, "url")
    observations = ObservationStore(out_dir / "pending" / "observations.json")
    rows, seen = store.resume(today)
    detail_cache_path = out_dir / "pending" / "feeds" / "reed-details.json"
    try:
        detail_cache = json.loads(detail_cache_path.read_text())
    except (OSError, ValueError):
        detail_cache = {}

    cfg_rules = {"title": tf, "engagement": ef, "location": lf, "min_rate": min_rate, "prefer": prefer}
    status = {"date": today, "boards": {}, "feeds": {},
              "totals": {"boards": 0, "feeds": 0, "jobs_read": 0, "title_matched": 0, "new": 0,
                         "preferred_new": 0, "by_engagement": {}}}
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    # Most adverts on a board are for unrelated roles. Their full text is most of the store, so keep
    # only identity, lifecycle and short facts for them, unless ingest names a record that needs them.
    protection, manifest = Protection.load(out_dir / "pending" / PROTECTION)
    status["retention"] = {"manifest": manifest, "text_dropped": 0, "held": 0}

    def observed(job: dict, scope: str, company: str | None = None) -> dict:
        sid = job.get("source_id") or job.get("url")
        current = {**job, **observed_terms(job, ef)}
        if company is not None:
            current["company"] = company
        if protection is not None and not title_match(job["title"], tf)[0]:
            if not protection.holds(scope=scope, source_id=str(sid), url=job.get("url") or "",
                                    company=current.get("company") or "", title=job["title"]):
                status["retention"]["text_dropped"] += 1
                return {"source_id": sid, "current": current, "compaction_reason": "title-unmatched"}
            status["retention"]["held"] += 1
        current["qualification"] = qualification(job)
        return {"source_id": sid, "current": current}

    scheduler = collection_schedule.Session(out_dir, offline=fixture_dir is not None)
    board_tasks = {}
    for company in cfg.get('tracked_companies', []):
        if company.get('enabled', True):
            provider, slug = detect(company.get('careers_url', ''))
            if provider: board_tasks.setdefault(provider, []).append(f'board:{provider}:{slug}')
    board_selected = [key for provider,keys in board_tasks.items() for key in scheduler.choose(provider,keys)]
    board_order = {key:i for i,key in enumerate(board_selected)}
    def board_position(company):
        provider,slug=detect(company.get('careers_url',''))
        return board_order.get(f'board:{provider}:{slug}',len(board_order))
    for company in sorted(cfg.get("tracked_companies", []),key=board_position):
        if not company.get("enabled", True):
            continue
        name = company.get("name", "?")
        provider, slug = detect(company.get("careers_url", ""))
        status["totals"]["boards"] += 1
        if not provider:
            status["boards"][name] = {"state": "unsupported-url", "url": company.get("careers_url")}
            continue
        board_key = f'board:{provider}:{slug}'
        if board_key not in board_selected:
            status['boards'][name] = scheduler.receipt(provider)
            continue
        try:
            if fixture_dir:
                fx = fixture_dir / f"{provider}-{slug}.json"
                if not fx.exists():
                    raise urllib.error.HTTPError(api_url(provider, slug), 404, "fixture missing", None, None)
                payload = json.loads(fx.read_text())
            else:
                payload = scheduler.call(provider, board_key, lambda: fetch_board(provider, slug))
            jobs = normalise(provider, payload)
        except collection_budget.Deferred as e:
            status['boards'][name] = scheduler.receipt(provider,e.reason)
            continue
        except urllib.error.HTTPError as e:
            status["boards"][name] = {"state": "bad-slug" if e.code == 404 else f"http-{e.code}", "provider": provider, "slug": slug}
            observations.observe(scope=f"board:{provider}:{slug}", source_type="job", provider=provider,
                                 observed_at=observed_at, state=status["boards"][name]["state"],
                                 complete=False, items=[], detail={"name": name, "slug": slug, "kind": failure_kind(e), "status": e.code})
            continue
        except Exception as e:  # network, timeout, JSON
            status["boards"][name] = {"state": "error", "detail": str(e)[:200], "provider": provider, "slug": slug}
            observations.observe(scope=f"board:{provider}:{slug}", source_type="job", provider=provider,
                                 observed_at=observed_at, state="error", complete=False,
                                 items=[], detail={"name": name, "slug": slug, **failure(e)})
            continue

        matched, new = consider(jobs, name, provider, today, cfg_rules, seen, rows, status)
        complete = True
        if provider == 'rippling':
            complete = rippling_complete(payload)
        if provider == "smartrecruiters":
            ids = [j.get("id") for j in payload.get("content", [])]
            complete = all(ids) and len(set(ids)) == len(ids) == payload.get("totalFound")
        state = ("ok" if jobs else "empty") if complete else "partial"
        status["boards"][name] = {"state": state, "complete": complete, "jobs": len(jobs), "title_matched": matched, "new": new, "provider": provider, "slug": slug}
        scope = f"board:{provider}:{slug}"
        observations.observe(scope=scope, source_type="job", provider=provider,
                             observed_at=observed_at, state=state, complete=complete,
                             items=[observed(j, scope, j.get("company") or name) for j in jobs],
                             detail={"name": name, "slug": slug})
        status["totals"]["title_matched"] += matched
        status["totals"]["new"] += new

    # A feed is a moving search window, so absence from it is never evidence that an advert closed;
    # only a provider-stated expiry or status closes one.
    feed_lifecycle = {"absence_authoritative": False, "expired_on": today}
    # Reed details cost a call each: read them only for adverts the rules keep, within a daily budget.
    try:
        detail_budget = {"left": collection_policy.load()['providers']['reed']['detail_limit']}
        requests = FeedRequests(feed_limits())
        if not scheduler.offline and scheduler.b:
            used=scheduler.b.summary()['providers']['reed']['detail_used']
            detail_budget['left']=max(0,detail_budget['left']-(used if used is not None else detail_budget['left']))
    except (ValueError,OSError):
        scheduler.reason='invalid-policy'
        detail_budget = {'left':0}
        requests = FeedRequests({})

    def wants_detail(summary: dict) -> bool:
        return (title_match(summary.get("jobTitle", "") or "", tf)[0]
                and location_ok(summary.get("locationName", "") or "", "", lf)[0])

    feed_tasks = {}
    for feed in cfg.get('feeds', []) if feeds else []:
        if feed.get('enabled'):
            feed_tasks.setdefault(feed['provider'], []).extend(collection_schedule.query_key(feed,p) for p in feed.get('params',{}).get('phrases') or [None])
    feed_selected = {}
    for provider,keys in feed_tasks.items():
        selected = scheduler.choose(provider,keys)
        feed_selected[provider] = selected
    # Without feeds (the free-sources run) no keyed provider is called and earlier feed sightings stand.
    def feed_position(feed):
        selected=feed_selected.get(feed.get('provider'),[])
        return min((selected.index(collection_schedule.query_key(feed,p)) for p in feed.get('params',{}).get('phrases') or [None] if collection_schedule.query_key(feed,p) in selected),default=len(selected))
    for feed in sorted(cfg.get("feeds", []),key=feed_position) if feeds else []:
        if not feed.get("enabled", False):
            continue
        name = feed.get("name", "?")
        provider = feed.get("provider", "")
        status["totals"]["feeds"] += 1
        if provider not in FEED_PROVIDERS:
            status["feeds"][name] = {"state": "unsupported-provider", "provider": provider}
            continue
        params = dict(feed.get('params', {}))
        phrases = params.get('phrases') or [None]
        positions={key:i for i,key in enumerate(feed_selected.get(provider,[]))}
        chosen = sorted([p for p in phrases if collection_schedule.query_key(feed,p) in positions],key=lambda p:positions[collection_schedule.query_key(feed,p)])
        if not fixture_dir and not chosen:
            status['feeds'][name] = scheduler.receipt(provider)
            continue
        if not fixture_dir:
            params.update(phrases=chosen, _query_keys={p:collection_schedule.query_key(feed,p) for p in chosen})
        calls_before = requests.calls.get(provider, 0)
        try:
            if fixture_dir:
                fx = fixture_dir / f"feed-{provider}-{feed.get('fixture', name)}.json"
                if not fx.exists():
                    raise FileNotFoundError(f"fixture missing: {fx}")
                payload = json.loads(fx.read_text())
                payload.setdefault("_complete", True)
            else:
                payload = fetch_feed(provider, params, cache=detail_cache, today=today,
                                     wants_detail=wants_detail, budget=detail_budget,
                                     request=lambda url, headers: requests.read(provider, url, headers))
        except collection_budget.Deferred as error:
            status['feeds'][name] = scheduler.receipt(provider,error.reason)
            continue
        except FeedStopped as error:
            status["feeds"][name] = {"state": error.state, "provider": provider,
                                      "calls": requests.calls.get(provider, 0) - calls_before}
            observations.observe(scope=f"feed:{provider}:{name}", source_type="job", provider=provider, **feed_lifecycle,
                                 observed_at=observed_at, state=error.state, complete=False,
                                 items=[], detail={"name": name})
            continue
        except FeedConfigError as e:
            status["feeds"][name] = {"state": "config", "detail": str(e)[:200], "provider": provider}
            observations.observe(scope=f"feed:{provider}:{name}", source_type="job", provider=provider, **feed_lifecycle,
                                 observed_at=observed_at, state="config", complete=False,
                                 items=[], detail={"name": name, "error": str(e)[:200]})
            continue
        except urllib.error.HTTPError as e:
            status["feeds"][name] = {"state": f"http-{e.code}", "provider": provider,
                                      "calls": requests.calls.get(provider, 0) - calls_before}
            observations.observe(scope=f"feed:{provider}:{name}", source_type="job", provider=provider, **feed_lifecycle,
                                 observed_at=observed_at, state=f"http-{e.code}", complete=False,
                                 items=[], detail={"name": name, "kind": failure_kind(e), "status": e.code})
            continue
        except Exception as e:  # network, timeout, JSON, missing fixture
            status["feeds"][name] = {"state": "error", "detail": str(e)[:200], "provider": provider,
                                      "calls": requests.calls.get(provider, 0) - calls_before}
            observations.observe(scope=f"feed:{provider}:{name}", source_type="job", provider=provider, **feed_lifecycle,
                                 observed_at=observed_at, state="error", complete=False,
                                 items=[], detail={"name": name, **failure(e)})
            continue

        jobs = normalise_feed(provider, payload)
        if not scheduler.offline and scheduler.b:
            for key in payload.get('_queries',[]):scheduler.b.succeeded(provider,key,observed_at)
        query_keys = {str(item.get('id') or item.get('jobId')):item.get('_query_keys',[]) for item in payload.get('results',[])}
        for job in jobs:
            if query_keys.get(str(job.get('source_id'))): job['query_keys'] = query_keys[str(job['source_id'])]
        # A rotation observes a subset of configured searches, never a complete feed.
        if len(chosen) < len(phrases): payload['_complete'] = False
        matched, new = consider(jobs, name, provider, today, cfg_rules, seen, rows, status)
        status["totals"]["title_matched"] += matched
        status["totals"]["new"] += new
        complete = bool(payload.get("_complete", True))
        status["feeds"][name] = {
            "state": ("ok" if jobs else "empty") if complete else "partial", "complete": complete,
            "jobs": len(jobs), "title_matched": matched, "new": new, "provider": provider,
            "pages": payload.get("_pages"), "total_available": payload.get("_total"),
            "calls": payload.get("_calls", 0),
        }
        detail = {'name': name}
        if payload.get('_stop_reason'):
            detail['stop_reason'] = status['feeds'][name]['stop_reason'] = payload['_stop_reason']
        scope = f"feed:{provider}:{name}"
        observations.observe(scope=scope, source_type="job", provider=provider, **feed_lifecycle,
                             observed_at=observed_at, state=status["feeds"][name]["state"],
                             complete=complete, items=[observed(j, scope) for j in jobs], detail=detail)

    if detail_cache:
        # Keep details seen in the last 30 days; anything older has left every feed.
        cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=30)).isoformat()
        kept = {k: v for k, v in detail_cache.items() if (v.get("seen") or "") >= cutoff}
        detail_cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(detail_cache_path, json.dumps(kept, sort_keys=True))

    # Contract-first ordering: preferred engagement types first, then unknown, then permanent.
    order = {e: 0 for e in prefer}
    order.update({"unknown": 1, "permanent": 2})
    rows.sort(key=lambda r: (order.get(r["engagement"], 1), r["company"], r["title"]))
    store.save(today, rows, seen, status)
    return status


# ---------------------------------------------------------------------------------------------
# Schema probe: confirm a feed's field names against the live API before trusting its rows.
#
# These adapters were written against each provider's published schema but have not been run
# against the live services from this repository. --probe answers "does the adapter still read what
# the API sends" with a field-by-field report, so a feed is enabled on evidence rather than hope.

FEED_SCHEMA = {
    "adzuna": {"root": "results", "required": ["title", "redirect_url"],
               "fields": ["title", "description", "created", "redirect_url", "company.display_name",
                          "location.display_name", "contract_type", "salary_min", "salary_max",
                          "category.label"]},
    "reed": {"root": "results", "required": ["jobId", "jobTitle"],
             "fields": ["jobId", "jobTitle", "employerName", "locationName", "date", "jobUrl",
                        "jobDescription", "minimumSalary", "maximumSalary", "currency", "contractType"]},
}


def _dig(item, path: str):
    cur = item
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def probe_feed(provider: str, params: dict, request=None) -> dict:
    """Fetch one page and report which expected fields are present, and what else came back."""
    spec = FEED_SCHEMA[provider]
    url, headers = feed_request(provider, params, 1)
    with single_attempt():
        payload = (request or fetch_json)(url, headers)
    items = payload.get(spec["root"])
    if not isinstance(items, list):
        return {"provider": provider, "ok": False,
                "problem": f"expected a list at '{spec['root']}'; got {type(items).__name__}",
                "top_level_keys": sorted(payload)[:20]}
    present = {}
    for f in spec["fields"]:
        hits = sum(1 for it in items if _dig(it, f)[1] and _dig(it, f)[0] not in (None, ""))
        present[f] = hits
    known = {f.split(".")[0] for f in spec["fields"]}
    extra = sorted({k for it in items if isinstance(it, dict) for k in it} - known)
    missing = [f for f in spec["required"] if present[f] == 0]
    optional_missing = [f for f, n in present.items() if n == 0 and f not in spec["required"]]
    return {"provider": provider, "ok": not missing, "sampled": len(items), "present": present,
            "missing": missing, "optional_missing": optional_missing, "unexpected_item_keys": extra,
            "top_level_keys": sorted(k for k in payload if k != spec["root"])[:20]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--portals", type=Path, default=root / "config/portals.yml")
    ap.add_argument("--out", type=Path, default=root / "data")
    ap.add_argument("--offline-fixture", type=Path, default=None, help="directory of <provider>-<slug>.json fixtures instead of the network")
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--no-feeds", action="store_true", help="read the employer boards only; call no keyed feed")
    ap.add_argument("--probe", metavar="PROVIDER", default=None,
                    help=f"check one feed provider's live schema and exit ({'|'.join(FEED_PROVIDERS)}|all)")
    a = ap.parse_args(argv)

    if a.probe:
        import yaml
        cfg = yaml.safe_load(a.portals.read_text())
        requests = FeedRequests(feed_limits())
        wanted = FEED_PROVIDERS if a.probe == "all" else (a.probe,)
        rc = 0
        for provider in wanted:
            feed = next((f for f in cfg.get("feeds", []) if f.get("provider") == provider), None)
            if feed is None:
                print(f"{provider}: no feed configured in {a.portals}")
                rc = 1
                continue
            try:
                r = probe_feed(provider, feed.get("params", {}),
                               request=lambda url, headers: requests.read(provider, url, headers))
            except FeedStopped as e:
                print(f"{provider}: not callable — {e.state}")
                rc = 1
                continue
            except FeedConfigError as e:
                print(f"{provider}: not callable — {e}")
                rc = 1
                continue
            except Exception as e:
                print(f"{provider}: request failed — {type(e).__name__}: {str(e)[:200]}")
                rc = 1
                continue
            print(f"{provider}: {'SCHEMA OK' if r['ok'] else 'SCHEMA MISMATCH'} ({r.get('sampled', 0)} items sampled)")
            for f, n in sorted(r.get("present", {}).items()):
                print(f"    {'ok ' if n else 'MISSING'} {f:34} {n} non-empty")
            if r.get("unexpected_item_keys"):
                print("    fields the adapter ignores: " + ", ".join(r["unexpected_item_keys"][:15]))
            if r.get("problem"):
                print(f"    {r['problem']}; top-level keys: {r.get('top_level_keys')}")
            rc = rc or (0 if r["ok"] else 1)
        return rc

    status = run(a.portals, a.out, a.today, a.offline_fixture, feeds=not a.no_feeds)
    t = status["totals"]
    bad = [n for n, b in status["boards"].items() if b.get("state") not in ("ok", "empty", "partial", "deferred")]
    bad_feeds = [n for n, f in status["feeds"].items() if f.get("state") not in ("ok", "empty", "partial", "deferred")]
    print(f"boards={t['boards']} feeds={t['feeds']} read={t['jobs_read']} title_matched={t['title_matched']} "
          f"new={t['new']} preferred_new={t['preferred_new']} problem_boards={len(bad)} problem_feeds={len(bad_feeds)}")
    eng = t["by_engagement"]
    print("yield by engagement: " + (", ".join(f"{k}={v}" for k, v in sorted(eng.items())) or "none"))
    # A run that surfaces jobs but no contract-shaped ones is a channel problem, not a quiet market:
    # employer ATS boards carry that employer's own headcount, which is overwhelmingly permanent.
    if t["new"] and not t["preferred_new"]:
        print("note: no contract/interim/FTC rows this run — check the feeds section, not just the boards")
    if bad:
        print("problem boards: " + ", ".join(f"{n}({status['boards'][n]['state']})" for n in bad))
    if bad_feeds:
        print("problem feeds: " + ", ".join(f"{n}({status['feeds'][n]['state']})" for n in bad_feeds))
    print('collection-receipt: '+json.dumps(collection_schedule.dispatch_summary(status)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
