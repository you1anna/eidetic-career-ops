"""`wo fetch` and `wo fetch-log`: read one URL attended, with the scanners' identity, retries and log.

The chain stops at the first successful read:
  1. robots.txt, for pages (a JSON API is read as the scanners read documented APIs);
  2. a direct read from this host (web_read.request retries transient failures);
  3. headless Chromium (tools/browser_read.mjs), for a page that came back as a script-rendered shell,
     or first with --browser;
  4. the same command on the VPS, from the Mac only, when this host could not reach the source
     (timeout, network or server error). That is a different network, not a way round a refusal.
A bot challenge, a refusal or a robots.txt disallow ends the chain. It is reported with what to try
instead and never retried from elsewhere or with another identity.

Every read is logged to the fetch log: <database folder>/logs/fetch.jsonl, or WO_FETCH_LOG. On the VPS
that is /srv/work-ops/data/logs/fetch.jsonl, shared with the daily ingest; read it with
`ssh ops@100.64.0.10 wo fetch-log`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.robotparser

import web_read
import collection_policy
import collection_budget

ROOT = Path(__file__).resolve().parent.parent
BROWSER_SCRIPT = ROOT / "tools" / "browser_read.mjs"
BROWSER_TIMEOUT = 60
REMOTE_KINDS = frozenset({"timeout", "network", "server"})
NEXT_STEP = {
    "challenged": "The site challenges automated readers. Use an official API, a feed or an email alert instead.",
    "refused": "The site refused this reader. Look for a documented API or feed.",
    "disallowed": "robots.txt disallows this path for automated readers. Look for an API or feed.",
    "not-found": "The address was not found. Check the URL.",
    "rate-limited": "The site is rate-limiting. Try later.",
}


class BrowserUnavailable(RuntimeError):
    """No node or no Playwright browser on this host."""


def default_remote() -> str:
    """The VPS from the Mac; nowhere from elsewhere (a cloud session has no route, and the VPS is itself)."""
    if "WO_FETCH_REMOTE" in os.environ:
        return os.environ["WO_FETCH_REMOTE"].strip()
    return "ops@100.64.0.10" if sys.platform == "darwin" else ""


def robots_allows(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    robots = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        with web_read.single_attempt():
            r = web_read.request(robots, accept="text/plain", logged=False, opener=web_read.safe_opener())
    except Exception:
        return True  # no readable robots.txt: nothing is disallowed
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(r["body"].decode(r["charset"] or "utf-8", errors="replace").splitlines())
    return parser.can_fetch("work-ops-scan", url)


def browser_read(url: str) -> dict:
    if collection_policy.provider_for(url):
        raise BrowserUnavailable('Collection APIs use the budgeted HTTP reader, never a browser')
    node = shutil.which("node")
    if not node or not (ROOT / "tests" / "ui" / "node_modules" / "playwright-core").is_dir():
        raise BrowserUnavailable("node or playwright-core is not installed on this host")
    started = time.monotonic()
    try:
        proc = subprocess.run([node, str(BROWSER_SCRIPT), url, json.dumps(collection_policy.ENDPOINTS)], capture_output=True, text=True, timeout=BROWSER_TIMEOUT)
    except subprocess.TimeoutExpired:
        web_read.log({"url": web_read.safe_url(url), "via": "browser", "ok": False, "kind": "timeout", "attempts": 1,
                      "ms": round((time.monotonic() - started) * 1000)})
        raise TimeoutError(f"browser read timed out after {BROWSER_TIMEOUT}s") from None
    if proc.returncode:
        if "Executable doesn't exist" in proc.stderr or "Cannot find module" in proc.stderr:
            raise BrowserUnavailable("the Playwright browser is not installed on this host")
        raise RuntimeError(f"browser read failed: {proc.stderr.strip()[-300:]}")
    page = json.loads(proc.stdout)
    status = page.get("status") or 0
    kind = None
    if (page.get("headers") or {}).get("cf-mitigated", "").lower() == "challenge":
        kind = "challenged"
    elif status in (401, 403):
        kind = "refused"
    elif status >= 400:
        kind = "not-found" if status in (404, 410) else "http"
    page["kind"] = kind
    web_read.log({"url": web_read.safe_url(url), "via": "browser", "ok": kind is None, "kind": kind, "status": status,
                  "attempts": 1, "ms": round((time.monotonic() - started) * 1000)})
    return page


def remote_read(url: str, target: str, fmt: str, browser: bool) -> subprocess.CompletedProcess:
    remote = ["wo", "fetch", "--via", "local", "--as", fmt] + (["--browser"] if browser else []) + [url]
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, shlex.join(remote)],
                          capture_output=True, text=True, timeout=180)


def _render(body: bytes, charset: str | None, content_type: str, fmt: str) -> str:
    text = body.decode(charset or "utf-8", errors="replace")
    if fmt == "raw":
        return text
    if fmt == "json" or "json" in content_type:
        try:
            return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            if fmt == "json":
                raise
    return web_read.strip_html(text) if "html" in content_type or text.lstrip().startswith("<") else text


def run(url: str, fmt: str = "text", browser: bool = False, via: str = "auto", remote: str | None = None) -> tuple[int, str, list[str]]:
    """Returns (exit code, content, notes). Exit 0 read, 2 blocked by the site, 1 any other failure."""
    remote = default_remote() if remote is None else remote
    where = socket.gethostname().split(".")[0]
    notes: list[str] = []
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return 1, "", ["only http and https addresses are read; local files never are"]
    try:
        web_read.validate_public_url(url)
    except ValueError:
        return 1, "", ["refused: a private, loopback, link-local or metadata address is not a public web source"]
    if via == "vps":
        if not remote:
            return 1, "", ["no VPS route from this host (set WO_FETCH_REMOTE)"]
        try:
            proc = remote_read(url, remote, fmt, browser)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, "", [f"VPS route failed: {e}"]
        return proc.returncode, proc.stdout, [proc.stderr.strip() or f"exit {proc.returncode} from {remote}"]
    if collection_policy.provider_for(url):
        browser = False
    if fmt != "json" and not collection_policy.provider_for(url) and not robots_allows(url):
        web_read.log({"url": web_read.safe_url(url), "ok": False, "kind": "disallowed", "attempts": 0})
        return 2, "", ["disallowed by robots.txt", NEXT_STEP["disallowed"]]
    kind = fallback = None
    if not browser:
        try:
            r = web_read.request(url, accept="application/json" if fmt == "json" else "text/html,application/xhtml+xml,*/*;q=0.8",
                                 opener=web_read.safe_opener())
        except Exception as e:
            kind = web_read.failure_kind(e)
            notes.append(f"direct read on {where}: {kind} after {getattr(e, 'fetch_attempts', 1)} attempt(s) ({str(e)[:120]})")
        else:
            content_type = (r["headers"].get("Content-Type") or r["headers"].get("content-type") or "").lower()
            body_text = r["body"].decode(r["charset"] or "utf-8", errors="replace")
            if fmt != "json" and "html" in content_type and web_read.looks_script_rendered(body_text):
                notes.append(f"direct read on {where}: HTTP {r['status']}, but the page is a script-rendered shell")
                browser = True
                fallback = _render(r["body"], r["charset"], content_type, fmt)
            else:
                notes.append(f"read directly on {where}: HTTP {r['status']}")
                return 0, _render(r["body"], r["charset"], content_type, fmt), notes
    if browser and kind is None:
        try:
            page = browser_read(url)
        except Exception as e:
            notes.append(f"browser: {e}" if isinstance(e, BrowserUnavailable) else f"browser read on {where}: {str(e)[:160]}")
            if fallback is not None:
                notes.append("showing the direct read's visible text, which may be incomplete")
                return 0, fallback, notes
            if isinstance(e, BrowserUnavailable):
                return 1, "", notes
            kind = web_read.failure_kind(e)
        else:
            if page["kind"] is None:
                notes.append(f"read in headless Chromium on {where}: HTTP {page['status']}")
                return 0, page["html"] if fmt == "raw" else page["text"], notes
            kind = page["kind"]
            notes.append(f"browser read on {where}: {kind} (HTTP {page['status']})")
    if kind in REMOTE_KINDS and via == "auto" and remote:
        notes.append(f"this host could not reach the source; trying {remote}")
        try:
            proc = remote_read(url, remote, fmt, False)
        except (OSError, subprocess.TimeoutExpired) as e:
            notes.append(f"VPS route failed: {e}")
        else:
            notes.append(proc.stderr.strip() or f"exit {proc.returncode} from {remote}")
            if proc.returncode in (0, 2):  # read there, or blocked there: the VPS's answer stands
                return proc.returncode, proc.stdout, notes
    if kind in NEXT_STEP:
        notes.append(NEXT_STEP[kind])
    return (2 if kind in web_read.BLOCKED else 1), "", notes


def show_log(path: Path, limit: int = 30, failures: bool = False, as_json: bool = False) -> str:
    entries = [e for e in web_read.read_log(path) if not failures or not e.get("ok")][-limit:]
    if as_json:
        return json.dumps(entries, indent=1)
    if not entries:
        return f"{path}: no {'failed ' if failures else ''}reads logged"
    lines = []
    for e in entries:
        outcome = "ok  " if e.get("ok") else f"FAIL {e.get('kind') or 'error'}"
        extra = f" via {e['via']}" if e.get("via") else ""
        lines.append(f"{e.get('at', '')}  {e.get('run') or e.get('by', '')}  {outcome}  {e.get('url', '')}"
                     f"  (HTTP {e.get('status') or '-'}, {e.get('attempts', 1)} attempt(s){extra})")
    summary = web_read.summarise(entries)
    lines.append(f"{summary['requests']} reads shown, {summary['recovered']} recovered on retry"
                 + "".join(f", {n} {k}" for k, n in sorted(summary["failed"].items())))
    return "\n".join(lines)


def main(a) -> int:
    """Dispatch from tools/wo.py; neither command opens the database."""
    os.environ.setdefault("WO_FETCH_LOG", str(a.db.parent / "logs" / "fetch.jsonl"))
    if a.command == "fetch-log":
        print(show_log(Path(os.environ["WO_FETCH_LOG"]), a.limit, a.failures, a.json))
        return 0
    os.environ.setdefault("WO_FETCH_RUN", "fetch:" + dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    code, content, notes = run(a.url, a.format, a.browser, a.via)
    if content:
        sys.stdout.write(content[: a.max_chars] if a.max_chars else content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    for note in notes:
        print(f"fetch: {note}", file=sys.stderr)
    return code
