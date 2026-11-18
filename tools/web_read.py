#!/usr/bin/env python3
"""Reading public web APIs: one user agent, one timeout, one retry policy, one fetch log.

Shared by every scanner and `wo fetch` so that how this project identifies itself, how long it waits
and when it tries again change in one place.

A failure is classified (`failure_kind`). Only transient kinds are retried: timeouts, network errors,
server errors and a short `Retry-After`. A refusal, a bot challenge or a missing page is never
retried, and nothing here changes identity or network to get past one. Retries are bounded three
ways so one bad host cannot starve a scheduled run: a per-host breaker, a per-process retry cap and
a per-process retry deadline. A caller whose every request spends quota (the Adzuna and Reed feeds)
wraps its call in `single_attempt()`.

Each call appends one line to the fetch log named by WO_FETCH_LOG (`wo ingest` sets it to
<data>/logs/fetch.jsonl). The line holds scheme, host and path only: queries and headers can carry
credentials, so they are never written.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import html
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import http.client
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
import collection_budget

USER_AGENT = "work-ops-scan/1.0 (+https://github.com/you1anna/eidetic-career-ops; personal job search)"
TIMEOUT = 25
TRANSIENT = frozenset({"timeout", "network", "server", "rate-limited"})
BLOCKED = frozenset({"challenged", "refused", "disallowed"})
LOG_MAX_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_SECONDS = 60
PUBLIC_HEADERS = frozenset({'accept', 'user-agent'})


def _web_origin(url):
    """Validate without echoing URLs, which may contain provider credentials."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None:
            raise ValueError
        port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
        return parts.scheme, parts.hostname.lower(), port
    except ValueError:
        raise ValueError('Only http and https addresses without embedded credentials are read') from None


# One owner for "is this safe to fetch from the open internet". `wo fetch`, the scanners and the
# unattended research reader all go through this, so a request can never be turned into a probe of a
# loopback service (the live workspace on 127.0.0.1:8769), the tailnet, a private LAN range or the
# cloud metadata endpoint. research_sources re-exports these and adds its own stricter port rule.
PRIVATE_HOSTNAMES = frozenset({'localhost', 'ip6-localhost', 'ip6-loopback',
                               'metadata', 'metadata.google.internal'})


def _host_is_private(hostname: str) -> bool:
    """Loopback, private, link-local, CGNAT/tailnet, metadata or otherwise non-global, by name or IP."""
    name = hostname.strip('[]').lower()
    if name in PRIVATE_HOSTNAMES or name.endswith('.localhost'):
        return True
    try:
        return not ipaddress.ip_address(name).is_global
    except ValueError:
        return False  # a hostname; its resolved addresses are validated when we connect


def validate_public_url(url):
    """`_web_origin`, then refuse an address that is not on the public internet. Returns the origin."""
    scheme, host, port = _web_origin(url)
    if _host_is_private(host):
        raise ValueError('Refusing a private, loopback, link-local or metadata address')
    return scheme, host, port


def public_connection(address, timeout=TIMEOUT, source_address=None, **kwargs):
    """Resolve once, refuse any non-global answer, then connect to that numeric address, so a name
    that resolves to a private address (DNS rebinding) cannot reach an internal service."""
    answers = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
        raise ValueError('Refusing a destination that resolves to a private address')
    last = None
    for family, kind, proto, _, endpoint in answers:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            if source_address:
                sock.bind(source_address)
            sock.connect(endpoint)
            return sock
        except OSError as error:
            last = error
            sock.close()
    raise last or OSError('No address to connect to')


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


def public_opener():
    """A direct-egress opener that validates every resolved address and redirect. For attended
    `wo fetch` and unattended research, where no forward proxy stands between us and the internet."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), PublicHTTPHandler(), PublicHTTPSHandler(), SafeRedirectHandler()).open


def safe_opener():
    """`public_opener` when egress is direct; when a forward proxy is configured (a cloud session),
    keep urllib's proxy handling and validate only the URL and redirects, since the proxy connects."""
    if urllib.request.getproxies():
        return urllib.request.build_opener(SafeRedirectHandler()).open
    return public_opener()


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # urllib drains redirect bodies with an unlimited read before following Location.
        # We need only the headers: close the transport and let its normal loop detection
        # and URL resolution operate on an empty body instead.
        collection_budget.finish(getattr(req,'_collection_reservations',[None])[-1],code)
        fp.close()
        location = headers.get('Location') or headers.get('URI')
        if location:
            validate_public_url(urllib.parse.urljoin(req.full_url, location))
        with io.BytesIO() as empty:
            return super().http_error_302(req, empty, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            before, after = validate_public_url(req.full_url), validate_public_url(newurl)
            private = (getattr(req, '_workops_private', False)
                       or bool(urllib.parse.urlsplit(req.full_url).query)
                       or bool({key.lower() for key in req.headers} - PUBLIC_HEADERS))
            if before[0] == 'https' and after[0] != 'https':
                raise ValueError('HTTPS redirects must not downgrade transport security')
            if before != after and private:
                raise ValueError('Requests with query parameters or private headers must not redirect to another origin')
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
            if redirected is not None:
                redirected._workops_private = private
                # urllib follows redirects internally; each hop spends another attempt.
                reservations=getattr(req,'_collection_reservations',[])
                reservations.append(collection_budget.begin(newurl,inherited=reservations[-1] if reservations else None))
                redirected._collection_reservations=reservations
            return redirected
        except ValueError:
            fp.close()
            raise


def _read_body(response):
    """Bound bytes even when Content-Length is absent or false; check time between reads."""
    deadline = time.monotonic() + MAX_RESPONSE_SECONDS
    body = bytearray()
    # HTTPResponse.read1 returns available bytes instead of waiting to fill a whole chunk.
    read = getattr(response, 'read1', response.read)
    while True:
        chunk = read(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - len(body)))
        if time.monotonic() > deadline:
            raise ValueError('Response exceeded the read time budget')
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError('Response exceeded the size limit')


class RetryPolicy:
    """Bounds for retrying transient failures within one process."""

    def __init__(self, retries: int = 2, backoff: tuple[float, ...] = (2.0, 6.0), host_breaker: int = 3,
                 retry_cap: int = 30, deadline: float = 300.0, max_retry_after: float = 30.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.retries, self.backoff, self.host_breaker = retries, backoff, host_breaker
        self.retry_cap, self.deadline, self.max_retry_after = retry_cap, deadline, max_retry_after
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.retries_used = 0
        self.host_failures: dict[str, int] = {}
        self.disabled = 0

    def may_retry(self, host: str) -> bool:
        return (not self.disabled and self.retries_used < self.retry_cap
                and self.host_failures.get(host, 0) < self.host_breaker
                and self.clock() - self.started < self.deadline)

    def wait(self, attempt: int, retry_after: float | None) -> None:
        self.retries_used += 1
        self.sleep(retry_after if retry_after is not None else self.backoff[min(attempt - 1, len(self.backoff) - 1)])


POLICY = RetryPolicy()


@contextmanager
def single_attempt():
    """One attempt per call inside this block, for requests counted against a provider quota."""
    POLICY.disabled += 1
    try:
        yield
    finally:
        POLICY.disabled -= 1


def _is_timeout(error: BaseException) -> bool:
    reason = getattr(error, "reason", None)
    return isinstance(error, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout))


def failure_kind(error: BaseException) -> str:
    """What went wrong, in terms of what to do next."""
    if isinstance(error, collection_budget.Deferred): return 'collection-deferred'
    kind = getattr(error, "fetch_kind", None)
    if kind:
        return kind
    if isinstance(error, urllib.error.HTTPError):
        headers = error.headers or {}
        if (headers.get("cf-mitigated") or "").lower() == "challenge":
            return "challenged"
        if error.code in (401, 403, 407):
            return "refused"
        if error.code in (404, 410):
            return "not-found"
        if error.code == 429:
            return "rate-limited"
        return "server" if error.code >= 500 else "http"
    if _is_timeout(error):
        return "timeout"
    if isinstance(error, (urllib.error.URLError, ConnectionError)):
        return "network"
    if isinstance(error, (ValueError, UnicodeDecodeError)):
        return "bad-response"
    return "error"


def failure(error: BaseException) -> dict:
    """A source receipt's detail for a failed read: the message, its kind and the attempts made."""
    out = {"error": str(error)[:200], "kind": failure_kind(error), "attempts": getattr(error, "fetch_attempts", 1)}
    if isinstance(error, urllib.error.HTTPError):
        out["status"] = error.code
    return out


def _retry_after(error: BaseException) -> float | None:
    if not isinstance(error, urllib.error.HTTPError):
        return None
    value = (error.headers or {}).get("Retry-After") or ""
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def safe_url(url: str) -> str:
    """Scheme, host and path: never the query, which can hold an API key."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}{parts.path[:200]}"


def log_path() -> Path | None:
    value = os.environ.get("WO_FETCH_LOG", "").strip()
    return Path(value) if value else None


def log(entry: dict, path: Path | None = None) -> None:
    """Append one line to the fetch log. A logging failure never fails the read."""
    path = path or log_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        line = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "run": os.environ.get("WO_FETCH_RUN", ""), "by": Path(sys.argv[0]).stem, **entry}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError:
        pass


def read_log(path: Path, run: str | None = None) -> list[dict]:
    """Lines of the fetch log and its one rotated predecessor, oldest first."""
    lines = []
    for p in (path.with_name(path.name + ".1"), path):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if run is None or entry.get("run") == run:
                lines.append(entry)
    return lines


def summarise(entries: list[dict]) -> dict:
    """Counts for a run's receipt: requests, reads that recovered on retry, failures by kind."""
    failed: dict[str, int] = {}
    for e in entries:
        if not e.get("ok"):
            failed[e.get("kind") or "error"] = failed.get(e.get("kind") or "error", 0) + 1
    return {"requests": len(entries), "recovered": sum(1 for e in entries if e.get("ok") and e.get("attempts", 1) > 1),
            "failed": failed}


def request(url: str, headers: dict | None = None, accept: str = "*/*", opener=None, logged: bool = True) -> dict:
    """GET with the project identity, retrying transient failures within POLICY's bounds.

    Returns status, final URL, headers, body bytes and charset. After the last attempt the original
    exception is raised, carrying `fetch_kind` and `fetch_attempts`, so callers that branch on
    HTTPError codes keep working."""
    _, host, _ = validate_public_url(url)
    h = {"User-Agent": USER_AGENT, "Accept": accept}
    h.update(headers or {})
    # Keep urllib's configured proxies and TLS verification; replace only redirect policy.
    opener = opener or urllib.request.build_opener(SafeRedirectHandler()).open
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        reservations = [None]
        try:
            reservations[0] = collection_budget.begin(url)
            req=urllib.request.Request(url,headers=h)
            req._collection_reservations=reservations
            with opener(req, timeout=TIMEOUT) as resp:
                collection_budget.finish(reservations[-1], getattr(resp, 'status', 200))
                result = {"status": getattr(resp, "status", 200), "url": resp.geturl() if hasattr(resp, "geturl") else url,
                          "headers": dict(resp.headers.items()) if resp.headers else {}, "body": _read_body(resp),
                          "charset": resp.headers.get_content_charset() if resp.headers else None}
        except Exception as error:
            kind = failure_kind(error)
            if not isinstance(error,collection_budget.Deferred):
                collection_budget.finish(reservations[-1], getattr(error, 'code', kind))
            wait = _retry_after(error)
            retryable = kind in TRANSIENT and not (kind == "rate-limited" and (wait is None or wait > POLICY.max_retry_after))
            if retryable and attempt <= POLICY.retries and POLICY.may_retry(host):
                POLICY.wait(attempt, wait)
                continue
            if kind in TRANSIENT:
                POLICY.host_failures[host] = POLICY.host_failures.get(host, 0) + 1
            try:
                error.fetch_kind, error.fetch_attempts = kind, attempt
            except AttributeError:
                pass
            if logged:
                log({"url": safe_url(url), "ok": False, "kind": kind, "attempts": attempt,
                     "status": getattr(error, "code", None), "ms": round((time.monotonic() - started) * 1000)})
            raise
        POLICY.host_failures[host] = 0
        if logged:
            log({"url": safe_url(url), "ok": True, "status": result["status"], "attempts": attempt,
                 "ms": round((time.monotonic() - started) * 1000)})
        return result


def fetch_json(url: str, headers: dict | None = None):
    r = request(url, headers, accept="application/json")
    return json.loads(r["body"].decode("utf-8"))


def fetch_text(url: str, headers: dict | None = None) -> str:
    """A text document such as an RSS feed, with the same identity and timeout as fetch_json."""
    r = request(url, headers, accept="application/rss+xml, application/xml;q=0.9, */*;q=0.5")
    return r["body"].decode(r["charset"] or "utf-8", errors="replace")


class _PlainTextParser(HTMLParser):
    """Collect visible text while discarding executable and styling element contents."""

    BLOCKS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td",
        "tfoot", "th", "thead", "tr", "ul",
    }
    HIDDEN = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self.HIDDEN:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.HIDDEN:
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif not self.hidden_depth and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def strip_html(s: str) -> str:
    """Return visible plain text from literal, encoded, or double-encoded provider HTML."""
    decoded = s or ""
    # Some APIs JSON-encode an HTML document whose markup was already entity-escaped.
    for _ in range(2):
        expanded = html.unescape(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    parser = _PlainTextParser()
    parser.feed(decoded)
    parser.close()
    return " ".join("".join(parser.parts).split())


def looks_script_rendered(body: str) -> bool:
    """An HTML shell whose content arrives by script: little visible text, and scripts present."""
    return bool(re.search(r"<script\b", body, re.I)) and len(strip_html(body)) < 200
