#!/usr/bin/env python3
"""Read job-alert emails (LinkedIn first) from an explicit local spool or read-only IMAP.

The scanner never holds access to the owner's main Gmail. The IMAP fallback uses a dedicated mailbox
opened read-only with BODY.PEEK. The explicit spool transport reads retained raw mail without
changing it; processing state and quarantine references live beneath --out. Neither route marks,
moves or deletes source mail. Only parsed advert facts enter source staging.

Alerts report presence only. An advert missing from later alerts has not closed, so this source
never adds absence (`absence_authoritative=False`). Email content is untrusted data: fields are
extracted, never followed or acted on.

  scan_alerts.py [--portals PATH] [--out DIR] [--today YYYY-MM-DD] [--offline-fixture DIR] [--probe]
"""
from __future__ import annotations

import argparse
import datetime as dt
import email
import hashlib
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "workspace")]
import credentials  # noqa: E402
from normalise import job_url  # noqa: E402
from job_rules import location_ok, title_match, observed_terms, text_pay  # noqa: E402
from pending_store import ObservationStore, PendingStore, atomic_write, atomic_write_json  # noqa: E402
from source_images import linkedin_company_logo, logo_counts  # noqa: E402
import email_sources  # noqa: E402
import alert_jackandjill  # noqa: E402

COLUMNS = ["date_seen", "source_id", "company", "title", "location", "engagement", "posted", "url", "alert_date"]
JOB_LINK = re.compile(r"https://www\.linkedin\.com/jobs/view/(\d+)/")
NOT_A_TITLE = {"view job", "apply", "easy apply", "see all jobs", "see more jobs", "view all"}
ENV_KEYS = ("WO_ALERTS_IMAP_HOST", "WO_ALERTS_IMAP_USER", "WO_ALERTS_IMAP_PASSWORD")
MAX_MESSAGES = 200
IMAP_TIMEOUT = 20
FETCH_SECONDS = 600  # leave time to save before ingest's 900-second subprocess timeout
SPOOL_PATH_KEY = "WO_ALERTS_SPOOL"


class AlertError(ValueError):
    """A safe diagnostic containing no server replies or message content."""


class AlertConfigError(AlertError):
    """Missing credential names, never their values."""


def _job_id(value: str) -> str | None:
    # Use the same identity as manual capture and imports. Restrict ports too: alert links
    # must identify the public LinkedIn site, not a different service on its hostname.
    try:
        parsed = urlsplit(value)
        if parsed.port not in (None, 80 if parsed.scheme == "http" else 443):
            return None
        match = JOB_LINK.fullmatch(job_url(value))
        return match[1] if match else None
    except ValueError:
        return None


class _Cards(HTMLParser):
    """Collect cards in one pass, keeping repeated cards separate from repeated job anchors.

    The explicit leading-regular / pt-3 boundary is present in all three retained alert layouts.
    Legacy unwrapped layouts retain their job-link grouping; arbitrary nested tables are not cards.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards: dict[tuple[int, str], dict] = {}
        self.current: tuple[int, str] | None = None
        self.link: tuple[int, str] | None = None
        self.link_text: list[str] = []
        self.line: list[str] = []
        self.ancestors: list[tuple[str, bool, int]] = []
        self.scope = self.scopes = 0

    def flush_line(self):
        if self.current and self.line:
            self.cards[self.current]["text"].append(" ".join(self.line))
        self.line = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "div", "tr", "td", "li", "br"}:
            self.flush_line()
        attributes = dict(attrs)
        classes = set((attributes.get('class') or '').split())
        previous_scope = self.scope
        if (tag == 'td' and 'pt-3' in classes and len(self.ancestors) >= 3
                and self.ancestors[-3][0:2] == ('table', True)
                and [item[0] for item in self.ancestors[-2:]] == ['tbody', 'tr']):
            self.scopes += 1
            self.scope = self.scopes
            self.current = None
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.ancestors.append((tag, tag == 'table' and 'leading-regular' in classes, previous_scope))
        if tag == "a":
            jid = _job_id(attributes.get("href") or "")
            if jid:
                self.flush_line()
            self.link = (self.scope, jid) if jid else None
            self.link_text = []
            if jid:
                self.current = self.link
                self.cards.setdefault(self.current, {"title": "", "text": []})
        elif tag == 'img' and self.link:
            # The current card persists after its link closes; it is not proof that a later
            # image belongs to that job. Only the exact containing job anchor establishes it.
            logo = linkedin_company_logo(attributes.get('src'))
            if logo:
                self.cards[self.link].setdefault('company_logo_url', logo)

    def handle_endtag(self, tag):
        if tag in {"p", "div", "tr", "td", "li"}:
            self.flush_line()
        if tag == "a":
            title = " ".join(self.link_text)
            if self.link and title and title.lower() not in NOT_A_TITLE:
                card = self.cards[self.link]
                if not card["title"]:
                    card["title"] = title
            self.link = None
            self.link_text = []
        for i in range(len(self.ancestors) - 1, -1, -1):
            if self.ancestors[i][0] == tag:
                previous_scope = self.ancestors[i][2]
                del self.ancestors[i:]
                if self.scope != previous_scope:
                    self.current = self.link = None
                self.scope = previous_scope
                break

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text or self.current is None:
            return
        if self.link == self.current:
            self.link_text.append(text)
        elif text.lower() not in NOT_A_TITLE:
            self.line.append(text)


def parse_linkedin(html: str) -> list[dict]:
    parser = _Cards()
    parser.feed(html)
    parser.close()
    parser.flush_line()
    jobs = []
    for (_, jid), card in parser.cards.items():
        if not card["title"]:
            raise AlertError("unsupported LinkedIn alert layout: job title missing")
        company = location = ""
        line = next((t for t in card["text"] if "·" in t), None)
        if line:
            company, _, location = (part.strip() for part in line.partition("·"))
        elif len(card["text"]) >= 2:
            # The other supported card layout uses separate company and location rows.
            company, location = card["text"][:2]
        job = {"source_id": jid, "title": card["title"], "company": company, "location": location,
               "url": f"https://www.linkedin.com/jobs/view/{jid}/"}
        # Keep only explicit pay text from this card, not personalized badges or footer content.
        pay_lines = [line for line in card['text'] if text_pay(line)]
        if pay_lines:
            job['pay_text'] = '\n'.join(dict.fromkeys(pay_lines))
        if card.get('company_logo_url'):
            job['company_logo_url'] = card['company_logo_url']
        jobs.append(job)
    return jobs


PARSERS = {"linkedin": parse_linkedin, "jackandjill": alert_jackandjill.parse}


def _sender_kind(message, senders: dict) -> str | None:
    headers = message.get_all('From', [])
    if len(headers) != 1 or headers[0].defects or len(headers[0].addresses) != 1:
        return None
    address = headers[0].addresses[0].addr_spec.lower()
    return next((kind for kind, addrs in senders.items() if kind in PARSERS and address in {a.lower() for a in addrs}), None)


def read_message(raw: bytes, senders: dict) -> tuple[str | None, list[dict], str]:
    """(sender kind, jobs, alert date); the email never establishes the advert's posted date."""
    message = email.message_from_bytes(raw, policy=policy.default)
    kind = _sender_kind(message, senders)
    if not kind:
        return None, [], ""
    part = message.get_body(preferencelist=("html", "plain") if kind == 'jackandjill' else ("html",))
    html = part.get_content() if part else ""
    if part and part.get_content_type() == 'text/plain':
        html = alert_jackandjill.plain_html(html)
    try:
        alert_date = parsedate_to_datetime(message.get("Date")).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        alert_date = ""
    found = PARSERS[kind](html)
    if not found:
        raise AlertError("unsupported job alert layout: no readable job cards")
    return kind, found, alert_date


def fetch_imap(cfg: dict, env: dict, state: dict, today: str) -> tuple[list[tuple[int, bytes]], dict]:
    """Oldest pending known-sender messages, read-only. The returned last_uid is the baseline,
    not an acknowledgement: run() advances it only after parsing and saving the prefix."""
    import imaplib
    deadline = time.monotonic() + FETCH_SECONDS
    host, user, password = (env[k] for k in ENV_KEYS)
    conn = imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT)

    def checked(result, operation):
        status, data = result
        if status != "OK" or not isinstance(data, list):
            raise RuntimeError(f"IMAP {operation} failed")
        return data

    try:
        checked(conn.login(user, password), "login")
        checked(conn.select(cfg.get("mailbox", "INBOX"), readonly=True), "select")
        validity = (conn.response("UIDVALIDITY")[1] or [b""])[0]
        validity = validity.decode() if isinstance(validity, bytes) else str(validity or "")
        if not validity.isdigit() or int(validity) < 1:
            raise RuntimeError("IMAP UIDVALIDITY missing or invalid")
        resume = state.get("uidvalidity") == validity
        last = int(state.get("last_uid", 0)) if resume else 0
        # Older checkpoints have no fixed window. Keep all outstanding UIDs rather than
        # applying a new lookback which could discard their existing backlog.
        first_since = state.get("first_since") or ("1970-01-01" if state else
            (dt.date.fromisoformat(today) - dt.timedelta(days=int(cfg.get("first_run_days", 14)))).isoformat())
        since = dt.date.fromisoformat(first_since).strftime("%d-%b-%Y")
        uids = set()
        for addrs in cfg.get("senders", {}).values():
            for addr in addrs:
                criteria = ["UID", f"{last + 1}:*"] if resume else []
                # Keep the original window across empty reads, first-message failures and UID
                # resets. Advancing today's lookback would eventually abandon failed mail.
                criteria += ["SINCE", since]
                data = checked(conn.uid("SEARCH", *criteria, "FROM", f'"{addr}"'), "search")
                if not data or not isinstance(data[0], bytes):
                    raise RuntimeError("IMAP search returned invalid data")
                uids |= {int(u) for u in (data[0] or b"").split() if int(u) > last}
        wanted = sorted(uids)[:_limit(cfg)]
        receipt = {"uidvalidity": validity, "last_uid": last, "first_since": first_since, "capped": len(uids) > len(wanted),
                   "pending_messages": len(uids)}
        messages = []
        for uid in wanted:
            if time.monotonic() >= deadline:
                receipt.update(capped=True, detail="mailbox read budget reached; pending messages retained")
                break
            try:
                data = checked(conn.uid("FETCH", str(uid), "(BODY.PEEK[])"), "fetch")
                raw = next((d[1] for d in data if isinstance(d, tuple) and len(d) == 2), b"")
                if not isinstance(raw, bytes) or not raw:
                    raise RuntimeError("IMAP fetch returned no message")
            except Exception:
                # Never include server text: it can contain login identifiers or message content.
                receipt.update(failed_uid=uid, detail="mailbox fetch failed; message retained for retry")
                break
            messages.append((uid, raw))
        return messages, receipt
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _limit(cfg: dict) -> int:
    return max(1, min(MAX_MESSAGES, int(cfg.get("max_messages", MAX_MESSAGES))))


def _load_state(path: Path) -> dict:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError:
        raise AlertError("alert checkpoint could not be read; retained for inspection") from None
    try:
        state = json.loads(text)
        if (not isinstance(state, dict) or type(state.get("last_uid")) is not int or
                state["last_uid"] < 0 or not isinstance(state.get("uidvalidity"), str) or
                not state["uidvalidity"]):
            raise ValueError
        if "first_since" in state:
            dt.date.fromisoformat(state["first_since"])
        return state
    except (TypeError, ValueError):
        raise AlertError("alert checkpoint is invalid; retained for inspection") from None


def _flush(paths: list[Path]) -> None:
    """Persist evidence before acknowledging its mail, including the atomic rename entries."""
    for path in paths:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    for directory in {path.parent for path in paths}:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _eligibility(jobs, whole: dict) -> None:
    for job in jobs:
        title_ok, title_reason = title_match(job["title"], whole.get("title_filter", {}))
        where_ok, where_reason = location_ok(job["location"], "", whole.get("location_filter", {}))
        job.update(observed_terms(dict(job, rate=job.get('pay_text', '')), whole.get("engagement_filter", {})))
        job["eligible"] = bool(title_ok and where_ok)
        job["eligibility_reason"] = f"{title_reason}; {where_reason}"


def _spool_path(cfg: dict, env: dict | None) -> Path:
    # Source location never follows --out: ingest dry runs redirect only output/checkpoints.
    value = credentials.get(SPOOL_PATH_KEY, env) or cfg.get("spool_path", "")
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise AlertConfigError("set an absolute WO_ALERTS_SPOOL or alerts.spool_path")
    return Path(value)


def _read_spool_json(path: Path):
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        return json.loads(path.read_text(), object_pairs_hook=unique)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise AlertError("spool processing ledger is unreadable or invalid; retained for inspection") from None


def _receipt_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _receipt_time(value: str) -> dt.datetime:
    when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ValueError("receipt timestamp requires a timezone")
    return when


def _spool_message(spool, sequence: int, receipt_id: str) -> dict:
    rows = spool.list_messages(after=sequence - 1, limit=1)
    if (not rows or rows[0]["sequence"] != sequence or rows[0]["receipt_id"] != receipt_id
            or hashlib.sha256(rows[0]["raw"]).hexdigest() != receipt_id):
        raise AlertError("spool restore or cursor mismatch; attended recovery required")
    return rows[0]


def _save_spool_ledger(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    atomic_write(path, json.dumps(payload, sort_keys=True))
    path.chmod(0o600)
    _flush([path])


def _source_statuses(senders, status, jobs, counts, unresolved=()):
    """Source failures stay independent; a capped shared read cannot claim complete coverage."""
    result = {}
    for kind in senders:
        selected = [j for j in jobs.values() if j['alert'] == kind]
        quarantined = sum(item.get('source', 'linkedin') == kind for item in unresolved)
        complete = not quarantined and not status.get('capped')
        state = 'error' if quarantined else 'partial' if not complete else 'ok' if selected else 'empty'
        detail = dict(state=state, complete=complete, messages=counts.get(kind, {}).get('messages', 0), jobs=len(selected),
                      eligible=sum(j['eligible'] for j in selected), quarantined=quarantined,
                      last_sequence=status.get('last_sequence'), high_watermark=status.get('high_watermark'))
        detail.update({key: counts.get(kind, {}).get(key, 0) for key in logo_counts([], _receipt_time(status['observed_at']))})
        if quarantined:
            detail['error'] = 'unsupported alert retained in spool quarantine'
        result['alert:' + kind] = detail
    return result


def _receipt_order(job, received_at=None):
    """One ordering for in-pass coalescing and later quarantine recovery; legacy mail has no sequence."""
    return _receipt_time(received_at or job['received_at']), job.get('receipt_sequence', 0)


def _collect_job(jobs, kind, job):
    key = (kind, job['source_id'])
    old = jobs.get(key)
    first = min((old.get('first_received_at') or old['received_at']) if old else job['received_at'], job['received_at'], key=_receipt_time)
    if old is None or _receipt_order(job) > _receipt_order(old):
        jobs[key] = job
    jobs[key]['first_received_at'] = first


def _stage_spool_jobs(observations: ObservationStore, out_dir: Path, today: str, jobs: dict,
                      status: dict, observed_at: str) -> None:
    store = PendingStore(out_dir / "pending" / "alerts", COLUMNS, "source_id")
    rows, seen = store.resume(today)
    status["new"] = 0
    for source in status['sources'].values():
        source['new'] = 0
    for job in jobs.values():
        # Retain existing LinkedIn TSV identities; other providers are explicitly scoped.
        sid = job['source_id'] if job['alert'] == 'linkedin' else job['alert'] + ':' + job['source_id']
        if job["eligible"] and sid not in seen:
            seen.add(sid)
            rows.append({"date_seen": job["received_at"][:10], **{k: job.get(k, "") for k in COLUMNS[1:]}, 'source_id': sid})
            status["new"] += 1
            status['sources']['alert:' + job['alert']]['new'] += 1
    store.save(today, rows, seen, status)
    wanted = {ObservationStore.key('alert:' + j['alert'], j['source_id']) for j in jobs.values()}
    previous = {key: value for key, value in observations.load()["items"].items() if key in wanted} if jobs else {}
    groups = {}
    for job in jobs.values():
        scope = 'alert:' + job['alert']
        old = previous.get(ObservationStore.key(scope, job["source_id"]))
        job['first_received_at'] = min(job.get('first_received_at') or job['received_at'],
                                      old['first_seen_at'] if old else job['received_at'], key=_receipt_time)
        # Replays and quarantine recovery cannot replace later source evidence or refresh old mail.
        if old and _receipt_order(old.get('current', {}), old['last_seen_at']) > _receipt_order(job):
            continue
        if old and old.get('current') == job and old['first_seen_at'] == job['first_received_at']:
            continue
        groups.setdefault((scope, job["received_at"]), []).append({"source_id": job["source_id"], "current": job,
                                                                  'first_seen_at': job['first_received_at']})
    for (scope, received_at), items in sorted(groups.items(), key=lambda pair: _receipt_time(pair[0][1])):
        observations.observe(scope=scope, source_type="job", provider=scope.split(':')[1] + '_alert', observed_at=received_at,
                                    state="partial", complete=False, absence_authoritative=False, items=items)
    del previous
    # Collection health is current; item chronology always uses the retained receipt time.
    for scope, detail in status['sources'].items():
        data = observations.observe(scope=scope, source_type="job", provider=scope.split(':')[1] + '_alert', observed_at=observed_at,
                                    state=detail["state"], complete=detail["complete"], absence_authoritative=False,
                                    items=[], detail=detail)
        data["sources"][scope]["item_ids"] = sorted(j['source_id'] for j in jobs.values() if scope == 'alert:' + j['alert'])
        atomic_write_json(observations.path, data)
    _flush([store.day_path(today), store.seen_path, store.dir / "last-run.json", observations.path])


def _stage_failure(observations, cfg, observed_at, status):
    for kind in cfg['senders']:
        observations.observe(scope='alert:' + kind, source_type='job', provider=kind + '_alert', observed_at=observed_at,
                             state=status['state'], complete=False, absence_authoritative=False, items=[], detail=status)


def _run_spool(cfg: dict, whole: dict, out_dir: Path, today: str, env: dict | None,
               retry_quarantine: bool) -> dict:
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    observations = ObservationStore(out_dir / "pending" / "observations.json")
    state_path = out_dir / "pending" / "alerts" / "spool-state.json"
    quarantine_path = state_path.with_name("spool-quarantine.json")
    try:
        from mail_intake import MailSpool
        spool = MailSpool(_spool_path(cfg, env), readonly=True)
        info = spool.info()
        high, store_id = info["max_sequence"], info["store_id"]
        state = _read_spool_json(state_path)
        if state is None:
            state = dict(version=1, store_id=store_id, last_sequence=0, last_receipt_id=None,
                         high_watermark=0, quarantine_receipts=[])
        if (not isinstance(state, dict) or set(state) != {"version", "store_id", "last_sequence", "last_receipt_id",
                                                        "high_watermark", "quarantine_receipts"}
                or type(state["version"]) is not int or state["version"] != 1 or state["store_id"] != store_id
                or type(state["last_sequence"]) is not int or type(state["high_watermark"]) is not int
                or not 0 <= state["last_sequence"] <= state["high_watermark"] <= high
                or (state["last_sequence"] == 0 and state["last_receipt_id"] is not None)
                or (state["last_sequence"] > 0 and not _receipt_id(state["last_receipt_id"]))
                or not isinstance(state["quarantine_receipts"], list)
                or any(not _receipt_id(r) for r in state["quarantine_receipts"])):
            raise AlertError("spool checkpoint or identity mismatch; attended recovery required")
        if state["last_sequence"]:
            _spool_message(spool, state["last_sequence"], state["last_receipt_id"])
        ledger = _read_spool_json(quarantine_path)
        if ledger is None:
            ledger = dict(version=1, store_id=store_id, items=[])
        if (not isinstance(ledger, dict) or set(ledger) != {"version", "store_id", "items"}
                or type(ledger["version"]) is not int or ledger["version"] != 1
                or ledger["store_id"] != store_id or not isinstance(ledger["items"], list)):
            raise AlertError("spool quarantine ledger is invalid; retained for inspection")
        quarantine = {}
        for item in ledger["items"]:
            if (not isinstance(item, dict) or set(item) not in ({"sequence", "receipt_id", "reason", "resolved"}, {"sequence", "receipt_id", "reason", "resolved", "source"})
                    or type(item["sequence"]) is not int or not 0 < item["sequence"] <= high
                    or item["sequence"] in quarantine or not _receipt_id(item["receipt_id"])
                    or item.get("source", "linkedin") not in PARSERS
                    or item["reason"] != "unsupported-alert" or type(item["resolved"]) is not bool):
                raise AlertError("spool quarantine ledger is invalid; retained for inspection")
            quarantine[item["sequence"]] = item
        if not set(state["quarantine_receipts"]) <= {x["receipt_id"] for x in quarantine.values()}:
            raise AlertError("spool quarantine evidence is missing; attended recovery required")
        jobs, resolved, source_counts = {}, set(), {}
        failed_source = "linkedin"
        processed = skipped = alerts = unreadable = 0
        image_counts = logo_counts([], _receipt_time(observed_at))
        after, anchor = state["last_sequence"], state["last_receipt_id"]
        deadline = time.monotonic() + FETCH_SECONDS
        limit = _limit(cfg)

        def collect(message):
            nonlocal processed, skipped, alerts, unreadable, failed_source
            # A corrupt receipt is an integrity failure, not a parser quarantine.
            if (not _receipt_id(message["receipt_id"]) or not isinstance(message["raw"], bytes)
                    or hashlib.sha256(message["raw"]).hexdigest() != message["receipt_id"]):
                raise AlertError("spool receipt integrity failed; attended recovery required")
            _receipt_time(message["received_at"])
            processed += 1
            try:
                kind, found, alert_date = read_message(message["raw"], cfg.get("senders", {}))
            except Exception:
                unreadable += 1
                failed_source = _sender_kind(email.message_from_bytes(message['raw'], policy=policy.default), cfg['senders']) or 'linkedin'
                return False
            if not kind:
                skipped += 1
                return True
            alerts += 1
            source_counts.setdefault(kind, {'messages': 0})['messages'] += 1
            for key, count in logo_counts(found, _receipt_time(observed_at)).items():
                image_counts[key] += count
                source_counts[kind][key] = source_counts[kind].get(key, 0) + count
            _eligibility(found, whole)
            for job in found:
                job.update(posted="", alert_date=alert_date, alert=kind, received_at=message["received_at"],
                           receipt_id=message["receipt_id"], receipt_sequence=message["sequence"])
                _collect_job(jobs, kind, job)
            return True

        if retry_quarantine:
            for sequence, item in sorted(quarantine.items()):
                if item["resolved"]:
                    continue
                if processed >= limit or time.monotonic() >= deadline:
                    break
                if collect(_spool_message(spool, sequence, item["receipt_id"])):
                    resolved.add(sequence)
        while after < high and processed < limit and time.monotonic() < deadline:
            batch = spool.list_messages(after=after, limit=min(10, limit - processed))
            batch = [message for message in batch if message["sequence"] <= high]
            if not batch:
                raise AlertError("spool snapshot changed; attended recovery required")
            for message in batch:
                sequence = message["sequence"]
                if type(sequence) is not int or not after < sequence <= high:
                    raise AlertError("spool sequence is invalid; attended recovery required")
                if not collect(message):
                    quarantine[sequence] = dict(sequence=sequence, receipt_id=message["receipt_id"], reason="unsupported-alert", resolved=False, source=failed_source)
                    # The immutable source retains raw MIME; the output ledger records what to retry.
                    _save_spool_ledger(quarantine_path, dict(version=1, store_id=store_id, items=list(quarantine.values())))
                else:
                    resolved.add(sequence)
                after, anchor = sequence, message["receipt_id"]
        unresolved = {seq: item for seq, item in quarantine.items() if seq not in resolved and not item["resolved"]}
        capped = after < high
        status = dict(date=today, transport="spool", state="error" if unresolved else "partial" if capped else "ok" if jobs else "empty",
                      complete=not unresolved and not capped, messages=alerts, processed=processed, skipped=skipped,
                      unreadable=unreadable, jobs=len(jobs), eligible=sum(j["eligible"] for j in jobs.values()),
                      quarantined=len(unresolved), quarantined_receipts=[x["receipt_id"] for x in unresolved.values()],
                      capped=capped, last_sequence=after, high_watermark=high)
        status.update(image_counts)
        status['observed_at'] = observed_at
        status['sources'] = _source_statuses(cfg['senders'], status, jobs, source_counts, unresolved.values())
        if unresolved:
            status.update(error="unsupported alert retained in spool quarantine", detail="retry quarantine after parser review")
        _stage_spool_jobs(observations, out_dir, today, jobs, status, observed_at)
        # Stage recovered evidence first. A crash before this update simply retries the same mail.
        # Keep resolution tombstones: if checkpoint writing fails, its old quarantine references
        # still resolve to durable evidence on the next run rather than falsely reporting loss.
        for sequence in resolved:
            if sequence in quarantine:
                quarantine[sequence]["resolved"] = True
        _save_spool_ledger(quarantine_path, dict(version=1, store_id=store_id, items=list(quarantine.values())))
        _save_spool_ledger(state_path, dict(version=1, store_id=store_id, last_sequence=after, last_receipt_id=anchor,
                                          high_watermark=high, quarantine_receipts=status["quarantined_receipts"]))
        return status
    except Exception as error:
        state_name = "config" if isinstance(error, AlertConfigError) else "error"
        detail = str(error) if isinstance(error, AlertError) else "spool read or durable staging failed; retained for retry"
        status = dict(date=today, transport="spool", state=state_name, complete=False, detail=detail, error=detail)
        try:
            _stage_failure(observations, cfg, observed_at, status)
        except Exception:
            pass  # A corrupt/full output remains untouched; the returned failure is still safe to print.
        return status


def run(portals_path: Path, out_dir: Path, today: str, fixture: Path | None = None, env: dict | None = None,
        *, retry_quarantine: bool = False) -> dict:
    import yaml
    whole = yaml.safe_load(portals_path.read_text()) or {}
    cfg = dict(whole.get("alerts") or {})
    cfg.setdefault('senders', email_sources.senders())
    if not cfg.get("enabled"):
        return {"date": today, "state": "disabled"}
    if cfg.get("transport", "imap") == "spool":
        return _run_spool(cfg, whole, out_dir, today, env, retry_quarantine)
    env = {key: credentials.get(key, env) for key in ENV_KEYS}
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    observations = ObservationStore(out_dir / "pending" / "observations.json")
    state_path = out_dir / "pending" / "alerts" / "state.json"
    try:
        state = _load_state(state_path)
        if fixture:
            files = sorted(fixture.glob("*.eml"))
            last = state.get("last_uid", 0) if state.get("uidvalidity") == "fixture" else 0
            pending = [(i + 1, f) for i, f in enumerate(files) if i + 1 > last]
            batch = {"uidvalidity": "fixture", "last_uid": last,
                     "capped": len(pending) > _limit(cfg), "pending_messages": len(pending)}
            messages = []
            for uid, path in pending[:_limit(cfg)]:
                try:
                    messages.append((uid, path.read_bytes()))
                except OSError:
                    batch.update(failed_uid=uid, detail="fixture read failed; message retained for retry")
                    break
        else:
            gaps = [k for k in ENV_KEYS if not (env.get(k) or "").strip()]
            if gaps:
                raise AlertConfigError("not set: " + ", ".join(gaps))
            messages, batch = fetch_imap(cfg, env, state, today)
    except AlertConfigError as error:
        status = {"date": today, "state": "config", "complete": False, "detail": str(error), "error": str(error)}
        _stage_failure(observations, cfg, observed_at, status)
        return status
    except Exception as error:  # network, login, IMAP protocol
        detail = str(error) if isinstance(error, AlertError) else f"mailbox read failed ({type(error).__name__}); retained for retry"
        status = {"date": today, "state": "error", "complete": False, "detail": detail, "error": detail}
        if isinstance(error, AlertError):
            status["kind"] = "config"
        _stage_failure(observations, cfg, observed_at, status)
        return status

    jobs, alert_messages, unreadable, source_counts = {}, 0, 0, {}
    failed_source = "linkedin"
    image_counts = logo_counts([], _receipt_time(observed_at))
    checkpoint = {"uidvalidity": batch["uidvalidity"], "last_uid": batch["last_uid"]}
    if "first_since" in batch:
        checkpoint["first_since"] = batch["first_since"]
    for uid, raw in messages:
        try:
            kind, found, alert_date = read_message(raw, cfg.get("senders", {}))
        except Exception:
            unreadable += 1
            failed_source = _sender_kind(email.message_from_bytes(raw, policy=policy.default), cfg['senders']) or 'linkedin'
            batch.update(failed_uid=uid, detail="unsupported or unreadable alert; message retained for retry")
            break
        checkpoint["last_uid"] = uid
        if not kind:
            continue
        alert_messages += 1
        source_counts.setdefault(kind, {'messages': 0})['messages'] += 1
        for key, count in logo_counts(found, _receipt_time(observed_at)).items():
            image_counts[key] += count
            source_counts[kind][key] = source_counts[kind].get(key, 0) + count
        for job in found:
            job.update(posted="", alert_date=alert_date, alert=kind, received_at=observed_at)
            job['receipt_sequence'] = uid
            _collect_job(jobs, kind, job)
    _eligibility(jobs.values(), whole)
    failed = "failed_uid" in batch
    complete = not failed and not batch.get("capped", False)
    status = {"date": today, "state": "error" if failed else "partial" if not complete else "ok" if jobs else "empty",
              "complete": complete, "messages": alert_messages,
              "unreadable": unreadable, "jobs": len(jobs), "eligible": sum(j["eligible"] for j in jobs.values())}
    status.update(image_counts)
    status.update({key: batch[key] for key in ("failed_uid", "detail", "capped", "pending_messages") if key in batch})
    if failed:
        status["error"] = status["detail"]
    status['observed_at'] = observed_at
    status['sources'] = _source_statuses(cfg['senders'], status, jobs, source_counts,
                                        [{'source': failed_source}] if failed else [])
    _stage_spool_jobs(observations, out_dir, today, jobs, status, observed_at)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Durable source staging precedes the reader checkpoint on both transports.
    atomic_write(state_path, json.dumps(checkpoint))
    _flush([state_path])
    return status


def probe(portals_path: Path, today: str, env: dict | None = None) -> int:
    """Log in read-only and report how many alert emails and parsed jobs the window holds. Counts only."""
    import yaml
    cfg = dict((yaml.safe_load(portals_path.read_text()) or {}).get("alerts") or {})
    cfg.setdefault('senders', email_sources.senders())
    if cfg.get("transport", "imap") == "spool":
        try:
            from mail_intake import MailSpool
            spool = MailSpool(_spool_path(cfg, env), readonly=True)
            high, after = spool.info()["max_sequence"], 0
            processed = jobs = malformed = 0
            deadline = time.monotonic() + FETCH_SECONDS
            while after < high and processed < _limit(cfg) and time.monotonic() < deadline:
                batch = spool.list_messages(after=after, limit=min(10, _limit(cfg) - processed))
                batch = [message for message in batch if message["sequence"] <= high]
                if not batch:
                    raise AlertError("spool snapshot changed")
                for message in batch:
                    try:
                        _, found, _ = read_message(message["raw"], cfg.get("senders", {}))
                        jobs += len(found)
                    except Exception:
                        malformed += 1
                    processed += 1
                    after = message["sequence"]
            print(f"alerts: spool checked read-only; {processed} messages, {jobs} jobs, {malformed} unreadable" +
                  ("; backlog capped" if after < high else ""))
            return 0 if jobs and not malformed else 1
        except Exception:
            print("alerts: spool or parser check failed; no content logged")
            return 1
    env = {key: credentials.get(key, env) for key in ENV_KEYS}
    gaps = [k for k in ENV_KEYS if not env[k]]
    if gaps:
        print("alerts: not callable — not set: " + ", ".join(gaps))
        return 1
    try:
        messages, batch = fetch_imap(cfg, env, {}, today)
        parsed = [read_message(raw, cfg.get("senders", {})) for _, raw in messages]
    except Exception as error:
        print(f"alerts: mailbox or parser check failed ({type(error).__name__}); no content logged")
        return 1
    alerts = [jobs for kind, jobs, _ in parsed if kind]
    fields = {f: sum(1 for jobs in alerts for j in jobs if j.get(f)) for f in ("title", "company", "location", "url")}
    total = sum(len(j) for j in alerts)
    print(f"alerts: {len(alerts)} alert emails read, {total} jobs parsed" +
          ("; backlog capped" if batch.get("capped") else "") +
          ("; a fetch failed" if batch.get("failed_uid") else ""))
    for f, n in fields.items():
        print(f"    {'ok ' if n == total and total else 'GAP'} {f:10} {n}/{total}")
    return 0 if not batch.get("failed_uid") and alerts and total and all(n == total for n in (fields['title'], fields['url'])) else 1


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--portals", type=Path, default=root / "config/portals.yml")
    ap.add_argument("--out", type=Path, default=root / "data")
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--offline-fixture", type=Path, help="directory of .eml files instead of the mailbox")
    ap.add_argument("--probe", action="store_true", help="read-only check of the mailbox and parser; counts only")
    ap.add_argument("--retry-quarantine", action="store_true", help="retry retained spool failures after parser review")
    a = ap.parse_args(argv)
    if a.probe:
        return probe(a.portals, a.today)
    status = run(a.portals, a.out, a.today, a.offline_fixture, retry_quarantine=a.retry_quarantine)
    print(json.dumps(status, indent=2))
    return 0 if status["state"] in ("ok", "empty", "partial", "disabled") else 1


if __name__ == "__main__":
    sys.exit(main())
