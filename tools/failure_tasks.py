"""Failures raised as tasks for the owner: one "failure" decision on the site per thing that is broken.

`wo ingest` raises one for each source that failed. Kinds that retrying cannot fix (a bot challenge,
a refusal, a missing page, bad configuration) are raised at once. A timeout, network or server
error, or an anomalous empty read, is raised when it fails two runs in a row, so one bad night is
not a task. A scanner that crashes without recording any source failure is raised as well.

`wo watch` raises one when the daily ingest or the nightly backup is overdue. Ingest runs the watch,
and so does the backup job, so a stopped timer is still reported by the other job.

Asking again is safe: `records.create_decision` returns the live decision for the same subject.
An open task is withdrawn once its subject works again. After the owner answers "Leave it for now" or
"Stop this source" and the agent closes it, the same failure is not raised again until the source
has read successfully in between.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re

import db
import records as store

KIND = "failure"
AT_ONCE = frozenset({"challenged", "refused", "disallowed", "not-found", "config", "http"})
HEALTHY = ("ok", "empty", "partial")
OVERDUE_HOURS = 30
STATE_KINDS = {"bad-slug": "not-found", "config": "config", "http-429": "rate-limited"}
# Adapter names are those emitted by ingest.default_adapters. An unknown adapter is never assumed
# to be covered by an unrelated source failure; a receipt can also name its adapter explicitly.
ADAPTER_SCOPES = {
    'boards': ('board:',), 'boards and feeds': ('board:', 'feed:'),
    'tenders': ('tender:',), 'contract awards': ('award:',), 'hacker news': ('hn:',), 'job alerts': ('alert:',),
    'funding news': ('news:',), 'companies house': ('companies-house',),
}


def subject_for(scope: str) -> str:
    return "workspace:" + ("source-" + re.sub(r"[^a-z0-9]+", "-", scope.lower()).strip("-"))[:100].rstrip("-")


def kind_of(source: dict) -> str:
    """A failed read's kind: from the scanner's classification, else from its state."""
    if source.get("kind"):
        return source["kind"]
    state = source.get("state") or ""
    if state in STATE_KINDS:
        return STATE_KINDS[state]
    m = re.fullmatch(r"http-(\d{3})", state)
    if m:
        code = int(m[1])
        return "refused" if code in (401, 403) else "server" if code >= 500 else "http"
    return state or "error"


def _time(value: str | None) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


def _parked(conn, subject: str, last_success: str | None) -> bool:
    """The owner chose to leave or stop this, and it has not worked since he did."""
    row = conn.execute("SELECT status, answer, processed_at FROM decisions WHERE subject=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (subject, KIND)).fetchone()
    if not row or row["status"] != "done" or row["answer"] not in ("wait", "drop"):
        return False
    success, closed = _time(last_success), _time(row["processed_at"])
    return not (success and closed and success > closed)


def raise_task(conn, subject: str, question: str, context: str, recommendation: str, reason: str,
               last_success: str | None = None) -> bool:
    if _parked(conn, subject, last_success):
        return False
    return store.create_decision(conn, dict(subject=subject, kind=KIND, question=question[:240], context=context,
                                            recommendation=recommendation, reason=reason), actor="agent")["saved"]


def clear(conn, subject: str, message: str, verified_at: str = '', evidence_kind: str = '') -> bool:
    with store.transaction(conn):
        row = conn.execute("SELECT id,revision FROM decisions WHERE subject=? AND kind=? AND status='open'", (subject, KIND)).fetchone()
        if row:
            result = store.process_decision(conn, row['id'], 'withdraw', message, actor='agent', revision=row['revision'])
            if verified_at:
                thread = result['decision']['thread']
                thread[-1]['verification'] = dict(at=verified_at, kind=evidence_kind, complete=True)
                conn.execute('UPDATE decisions SET thread_json=? WHERE id=?', (json.dumps(thread), row['id']))
    return bool(row)


def _sentence(source: dict, kind: str) -> str:
    template = store.FAILURE_TEXT.get(kind)
    if template:
        return template.format(status=source.get("status") or "?", attempts=source.get("attempts") or 1)
    if kind == "anomalous-empty":
        return "it returned nothing although it listed items before, so nothing was closed. Check whether it moved."
    return f"{kind}: the read did not complete. Inspect the source diagnostics."


def for_ingest(conn, receipt: dict, previous_failures: list[str]) -> dict:
    """Raise and clear source tasks from one ingest receipt."""
    raised, cleared = [], []
    day = (receipt.get("started_at") or "")[:10]
    if 'import' in receipt:
        error = receipt['import'].get('error')
        if error:
            if raise_task(conn, 'workspace:ingest-import', 'The daily run could not import its source evidence.',
                          f"Import at {receipt.get('started_at')} failed: {error}", 'fix',
                          'The import transaction was rolled back. Preserve and inspect the pending evidence before retrying.'):
                raised.append('import')
        elif clear(conn, 'workspace:ingest-import', f'The source evidence imported successfully on {day}; nothing to do.', receipt.get('finished_at') or receipt.get('started_at'), 'import_run'):
            cleared.append('import')
    if receipt.get('reused_pending'):
        return {'raised': raised, 'cleared': cleared}
    health = {r["scope"]: r["last_success"] for r in conn.execute("SELECT scope, last_success FROM source_health")}
    failed_sources = []
    for scope, source in sorted((receipt.get("sources") or {}).items()):
        subject = subject_for(scope)
        if source.get("state") in HEALTHY:
            if source.get('state') in ('ok', 'empty') and source.get('complete') is True and clear(conn, subject, f"A new complete read succeeded on {day}; recovery verified.", source.get("observed_at") or receipt.get("started_at"), "source_read"):
                cleared.append(scope)
            continue
        if source.get("state") in ("budget", "deferred"):  # a local per-run cap, not a failure of the source
            continue
        failed_sources.append((scope, source))
        kind = kind_of(source)
        if kind not in AT_ONCE and scope not in previous_failures:
            continue
        label = source.get("label") or scope
        at_once = kind in AT_ONCE
        question = f"The daily run cannot read {label}: {_sentence(source, kind)}"
        context = (f"Source {scope}; failure kind {kind}; state {source.get('state')}; first raised by the ingest of "
                   f"{receipt.get('started_at')}. Recent requests: ssh ops@work-ops-vps wo fetch-log --failures.")
        reason = ("Retrying cannot fix this kind of failure." if at_once
                  else "It failed in two daily runs in a row; one more night may be enough, and this closes itself if it reads again.")
        if raise_task(conn, subject, question, context, "fix" if at_once else "wait", reason, health.get(scope)):
            raised.append(scope)
    crashed = [n for n, a in (receipt.get("adapters") or {}).items() if a.get("returncode") != 0]
    for name in (receipt.get("adapters") or {}):
        if name not in crashed and receipt["adapters"][name].get("state") in ("disabled", "skipped", "deferred"):
            continue  # An intentional no-op neither failed nor proves scanner recovery.
        subject = "workspace:scanner-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if name not in crashed:
            if clear(conn, subject, f"The scanner ran cleanly on {day}; nothing to do.", receipt.get("finished_at") or receipt.get("started_at"), "scanner_run"):
                cleared.append(name)
        elif not any(source.get('adapter') == name or scope.startswith(ADAPTER_SCOPES.get(name, ()))
                     for scope, source in failed_sources):
            # Source tasks cover failures of this adapter only. Another adapter may have failed
            # before it could leave any receipt, and must still be visible.
            if raise_task(conn, subject, f"The {name} scanner stopped with an error in the daily run (exit {receipt['adapters'][name].get('returncode')}).",
                          f"Exit {receipt['adapters'][name].get('returncode')}; see SSH sudo journalctl -u wo-scan -n 60.",
                          "fix", "The scanner stopped without a source failure explaining its exit; some reads may be missing."):
                raised.append(name)
    return {"raised": raised, "cleared": cleared}


def watch(conn, data: Path, now: dt.datetime | None = None) -> dict:
    """Raise a task when the daily ingest or the nightly backup is overdue; clear it once fresh."""
    now = now or dt.datetime.now(dt.timezone.utc)
    last = conn.execute("SELECT receipt_json,status FROM scan_runs WHERE run_id LIKE 'ingest:%' ORDER BY run_id DESC LIMIT 1").fetchone()
    ingest_at = json.loads(last["receipt_json"]).get("started_at") if last else None
    try:
        backup_at = json.loads((Path(data) / "backups" / "last-backup.json").read_text()).get("at")
    except (OSError, ValueError):
        backup_at = None
    out = {"raised": [], "cleared": []}
    jobs = [("daily-ingest", "daily ingest", ingest_at, "SSH systemctl list-timers wo-scan.timer; SSH sudo journalctl -u wo-scan -n 60", OVERDUE_HOURS),
            ("nightly-backup", "nightly backup", backup_at, "SSH sudo journalctl -u wo-backup -n 40", OVERDUE_HOURS)]
    # Nothing else watches the caretaker. Only once it has run: a fresh install has no timer yet.
    care = conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'caretaker:%' ORDER BY run_id DESC LIMIT 1").fetchone()
    if care:
        cause = db.failure_summary(data, "wo-caretaker.service", now)[0]
        jobs.append(("site-caretaker", "site caretaker", json.loads(care["receipt_json"]).get("started_at"),
                     "SSH systemctl status wo-caretaker.timer wo-caretaker; SSH sudo journalctl -u wo-caretaker -n 40." + cause.rstrip("."), 2))
    for slug, name, at, where, hours in jobs:
        subject = f"workspace:{slug}"
        seen = _time(at)
        if slug == 'daily-ingest' and last and last['status'] == 'failed':
            if raise_task(conn, subject, 'The daily ingest did not finish; the latest run failed.',
                          'Inspect the ingest receipt and service journal before retrying.', 'fix',
                          'The run did not finish, so current source coverage is not confirmed.'):
                out['raised'].append(slug)
            continue
        if seen and now - seen < dt.timedelta(hours=hours):
            if clear(conn, subject, f"The {name} ran at {at}; nothing to do.", at, "job_run"):
                out["cleared"].append(slug)
            continue
        when = f"last ran {at}" if at else "has no recorded run"
        if raise_task(conn, subject, f"The {name} is overdue: it {when}, more than {hours} hours ago.",
                      f"Where to look: {where}.", "fix", "The site's data or its backups are going stale.", at):
            out["raised"].append(slug)
    return out
