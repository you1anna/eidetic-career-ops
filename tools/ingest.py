"""One ingest run, identical on the Mac and the VPS: `wo ingest`.

Only the VPS schedules it (`wo-scan.timer`, `--keyless`, since the 27 September cutover); a host is a
change of paths and environment, not of code. A run:

  1. loads credentials from an env file (names only are ever reported, never values);
  2. takes the shared writer lock, so two runs or a backup job never overlap;
  3. runs each adapter as an isolated subprocess: one failing source never costs the others. Their
     requests share tools/web_read.py's retry policy and log to <data>/logs/fetch.jsonl under this run.
     Adapters never open the database;
  4. backs up the database (rotating, keep 7) and imports everything through the one door,
     `wo import-scans`. An `incremental` run (the 15-minute email run) whose reads staged nothing
     records those reads in source health instead, as the import would, with no backup or replay;
  6. records one receipt in `scan_runs`: adapters, source states, feed calls, requests (recovered on
     retry, failed by kind), import and quality counts;
  7. raises each failure as a task for the owner on the site, and clears those that work again
     (tools/failure_tasks.py), including an overdue nightly backup.

It never regenerates committed views, commits, or runs `wo repair` (repair can merge records, which
needs an attended decision). `--dry-run` does all of this on a copy of the database and pending store.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "workspace")]
import db  # noqa: E402
import pending_store  # noqa: E402
import web_read  # noqa: E402

PORTALS = ROOT / "config" / "portals.yml"
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
FEED_KEYS = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "REED_API_KEY")
# Keyed sources the owner has approved one by one, which a `--keyless` run may still call by name:
# Alerts additionally require their enabled configuration gate; naming the group installs no account.
KEYED = ("companies-house", "feeds", "alerts")
CH_TARGETS = "companies-house-targets.json"
ALERT_KEYS = ("WO_ALERTS_IMAP_HOST", "WO_ALERTS_IMAP_USER", "WO_ALERTS_IMAP_PASSWORD")
ADAPTER_TIMEOUT = 900


class Busy(RuntimeError):
    """Another writer holds the lock."""


def load_env(path: Path) -> dict[str, str]:
    """KEY=value lines (optionally `export`ed or quoted). A missing file is an empty environment."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return {}
    env = {}
    for line in lines:
        m = ENV_LINE.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        value = m[2]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[m[1]] = value
    return env


def missing(env: dict, names) -> list[str]:
    return [n for n in names if not (env.get(n) or "").strip()]


def _job_fields():
    return os.getpid(), os.environ.get('WO_RUN_ID') or '-'


CGROUP_EVENTS = ("high", "max", "oom", "oom_kill")


def _peak_rss(who) -> int:
    scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes on macOS, KiB on Linux
    return resource.getrusage(who).ru_maxrss * scale


def memory_evidence(proc_cgroup: Path = Path("/proc/self/cgroup"),
                    cgroup_root: Path = Path("/sys/fs/cgroup")) -> dict:
    """This run's memory, read as it finishes. The service cgroup's peak includes reclaimable file
    cache (store rewrites, the pre-import backup); anon/file split its current use; events count
    limit pressure and OOM kills since the run began. Process figures are resident high-water marks
    of this process and of its largest child, never a sum. Unreadable values stay None."""
    evidence = {"process_peak_rss_bytes": _peak_rss(resource.RUSAGE_SELF),
                "largest_child_peak_rss_bytes": _peak_rss(resource.RUSAGE_CHILDREN),
                "cgroup": {"state": "unknown"}}
    try:
        line = next(x for x in Path(proc_cgroup).read_text().splitlines() if x.startswith("0::"))
    except (OSError, StopIteration):
        return evidence
    relative = Path(line[3:].strip().lstrip("/"))
    group = Path(cgroup_root) / relative
    if ".." in relative.parts or not group.is_dir():
        return evidence

    def read(name):
        try:
            return (group / name).read_text()
        except OSError:
            return ""

    def number(text):
        return int(text) if re.fullmatch(r"\d{1,20}", text.strip()) else None

    def pairs(text):
        return {k: number(v) for k, v in (x.split(" ", 1) for x in text.splitlines() if " " in x)}

    stat, events = pairs(read("memory.stat")), pairs(read("memory.events"))
    evidence["cgroup"] = {"state": "observed", "peak_bytes": number(read("memory.peak")),
                          "limit_bytes": number(read("memory.max")),
                          "anon_bytes": stat.get("anon"), "file_bytes": stat.get("file"),
                          "events": {k: events[k] for k in CGROUP_EVENTS if events.get(k) is not None}}
    return evidence


@contextmanager
def writer_lock(data: Path, wait: float = 0):
    """The same lock file the VPS backup job uses, so ingest and backups never overlap. Waits up to
    `wait` seconds for the other writer to finish."""
    data.mkdir(parents=True, exist_ok=True)
    handle = open(data / ".writer.lock", "a")
    started = time.monotonic()
    deadline = started + wait
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    pid, run_id = _job_fields()
                    logging.warning('writer lock timeout wait_ms=%d pid=%s run_id=%s',
                                    round((time.monotonic() - started) * 1000), pid, run_id)
                    raise Busy(f"another writer holds {data / '.writer.lock'}") from None
                time.sleep(2)
        pid, run_id = _job_fields()
        logging.info('writer lock acquired wait_ms=%d pid=%s run_id=%s',
                     round((time.monotonic() - started) * 1000), pid, run_id)
        held = time.monotonic()
        try:
            yield
        finally:
            logging.info('writer lock released hold_ms=%d pid=%s run_id=%s',
                         round((time.monotonic() - held) * 1000), pid, run_id)
    finally:
        handle.close()


def backup(db_path: Path, backups: Path, keep: int = 7, stamp: str | None = None) -> Path:
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = backups / f"work-ops-pre-ingest-{stamp or dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    conn = db.connect(db_path)
    try:
        db.backup(conn, dest)
    finally:
        conn.close()
    for old in sorted(backups.glob("work-ops-pre-ingest-*.db"))[:-keep]:
        old.unlink()
    db.prune_backups(backups)  # the other backup kinds, by the shared retention rule
    return dest


def default_adapters(keyless: bool = False, keyed=()) -> list[tuple[str, list[str]]]:
    """Every source, or with `keyless` only those that need no key or account: the employer boards
    without the Adzuna and Reed feeds, tenders, contract awards, Hacker News and funding news, and no mailbox. `keyed`
    adds approved keyed sources back to a keyless run by name (`KEYED`)."""
    unknown = sorted(set(keyed) - set(KEYED))
    if unknown:
        raise ValueError(f"not an approved keyed source: {', '.join(unknown)}")
    py = sys.executable
    boards = [py, str(ROOT / "tools/scan_boards.py"), "--portals", "{portals}", "--out", "{data}"]
    adapters = [
        ("boards", boards + ["--no-feeds"]) if keyless and "feeds" not in keyed else ("boards and feeds", boards),
        ("tenders", [py, str(ROOT / "tools/scan_tenders.py"), "--portals", "{portals}", "--out", "{data}"]),
        ("contract awards", [py, str(ROOT / "tools/scan_awards.py"), "--portals", "{portals}", "--out", "{data}"]),
        ("hacker news", [py, str(ROOT / "tools/scan_hn.py"), "--portals", "{portals}", "--out", "{data}"]),
        ("job alerts", [py, str(ROOT / "tools/scan_alerts.py"), "--portals", "{portals}", "--out", "{data}"]),
        ("funding news", [py, str(ROOT / "tools/scan_news.py"), "--portals", "{portals}", "--out", "{data}"]),
        # After funding news, so a raise read this morning makes its company a target tomorrow.
        ("companies house", [py, str(ROOT / "tools/scan_companies_house.py"), "--out", "{data}"]),
    ]
    dropped = ({"job alerts"} if "alerts" not in keyed else set()) | ({"companies house"} if "companies-house" not in keyed else set())
    return [a for a in adapters if not (keyless and a[0] in dropped)]


def companies_house_targets(conn, state=None, chosen_ids=()) -> list[dict]:
    """The companies to approach, as the site ranks them, with any register number already mapped."""
    import records
    numbers = {r["record_id"]: r["source_id"] for r in
               conn.execute("SELECT record_id,source_id FROM source_records WHERE source_scope='companies-house'")}
    return [{"id": r["id"], "name": r["name"], "company_number": numbers.get(r["id"], "")}
            for r in (state or records.state_context(conn))["records"]
            if r["type"] == "company" and ((r.get("relevance") or {}).get("approach") or r["id"] in chosen_ids)]


def _run_adapter(argv: list[str], env: dict) -> dict:
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=ADAPTER_TIMEOUT)
        code, tail = proc.returncode, (proc.stdout + proc.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        code, tail = -1, f"timed out after {ADAPTER_TIMEOUT}s"
    result = {"returncode": code, "seconds": round(time.monotonic() - started, 1), "output_tail": tail,
              # The largest child so far: when it rises, this adapter set it.
              "children_peak_rss_bytes": _peak_rss(resource.RUSAGE_CHILDREN)}
    if code == 0:
        # Some adapters return a JSON receipt for an intentional no-op. Exit zero says the
        # process succeeded, but a disabled source supplied no read or recovery evidence.
        try:
            status = json.loads(proc.stdout)
        except ValueError:
            # Human-readable adapters append one additive machine receipt.
            line=next((line[len('collection-receipt: '):] for line in proc.stdout.splitlines() if line.startswith('collection-receipt: ')),None)
            try:status=json.loads(line) if line else None
            except ValueError:status=None
        if isinstance(status, dict) and status.get("state") in ("disabled", "skipped", "deferred"):
            result["state"] = status["state"]
        if isinstance(status,dict):
            import collection_schedule
            result.update(collection_schedule.dispatch_summary(status))
            if status.get('collection_deferrals'):result['collection_deferrals']=status['collection_deferrals']
    return result


def _source_states(data: Path, since: str) -> dict:
    """Each source read during this run and its state, from the adapters' own receipts."""
    try:
        from observation_reader import load
        payload = load(data / "pending" / "observations.json", sources_only=True)
    except (OSError, ValueError):
        return {}
    sources = payload.get('sources', {}) if isinstance(payload, dict) else {}
    if not isinstance(sources, dict):
        return {}
    # Compare to the second: older adapters may stamp only whole seconds.
    out = {}
    for scope, s in sources.items():
        if not isinstance(s, dict) or not isinstance(s.get('observed_at', ''), str):
            continue
        if (s.get("observed_at") or "")[:19] < since[:19]:
            continue
        detail = s.get("detail") if isinstance(s.get("detail"), dict) else {}
        out[scope] = {"state": s.get("state"), "complete": s.get("complete"), "items": len(s['item_ids']) if isinstance(s.get('item_ids'), list) else 0,
                      "observed_at": s.get("observed_at", "")}
        if detail.get('stop_reason') == 'budget':
            out[scope]['stop_reason'] = 'budget'
        if scope.startswith('alert:'):
            out[scope].update({k: detail[k] for k in ('messages', 'jobs', 'eligible', 'new', 'quarantined',
                                                   'last_sequence', 'high_watermark', 'cards', 'logo_cards',
                                                   'logo_expired', 'logo_expiring_soon', 'logo_expiry_unknown') if k in detail})
        if s.get("state") not in ("ok", "empty", "partial"):
            out[scope].update({k: detail[k] for k in ("kind", "status", "attempts") if detail.get(k)},
                              label=detail.get("name") or scope, error=str(detail.get("error") or "")[:200])
    return out


def _feed_calls(data: Path) -> dict:
    try:
        feeds = json.loads((data / "pending" / "last-run.json").read_text()).get("feeds", {})
    except (OSError, ValueError):
        return {}
    return {name: f.get("calls", 0) for name, f in feeds.items() if isinstance(f, dict)}


def unseen_adverts(conn) -> dict:
    """Adverts the ranking marks as no longer listed by any scanner (relevance.unseen_since)."""
    import record_views
    import relevance
    f = relevance.load_config()["freshness"]
    seen, today = record_views.last_sightings(conn), dt.date.today()
    rows = conn.execute("SELECT id, type, source_lifecycle, published_deadline FROM records WHERE archived_at IS NULL")
    count = sum(1 for r in rows if r["type"] in relevance.ADVERT_TYPES
                and relevance.unseen_since(dict(r), seen.get(r["id"]), today, f))
    return {"unseen_adverts": count, "days": f["unseen_days"]}


def _staged_nothing(adapters: dict, sources: dict) -> bool:
    """Every adapter exited cleanly and every source read in this run was complete and returned nothing."""
    return (bool(adapters) and bool(sources)
            and all(a.get("returncode") == 0 and not a.get("state") for a in adapters.values())
            and all(s.get("state") == "empty" and s.get("complete") is True and not s.get("items") for s in sources.values()))


def _store_imported(conn) -> bool:
    """The latest run that staged or imported evidence (the daily ingest, or an email import that did
    not skip) imported it without error. Until one does, every email run replays the store."""
    def receipt(row):
        return dict(json.loads(row["receipt_json"]), _status=row["status"]) if row else None
    # Range scans use the run_id key; a run_id is its prefix and its ISO start time.
    last = receipt(conn.execute("SELECT receipt_json, status FROM scan_runs WHERE run_id >= 'ingest:' AND run_id < 'ingest;' "
                                "ORDER BY run_id DESC LIMIT 1").fetchone())
    for row in conn.execute("SELECT receipt_json, status FROM scan_runs WHERE run_id > ? AND run_id < 'email-import;' "
                            "ORDER BY run_id DESC", ("email-import:" + (last or {}).get("started_at", ""),)):
        later = receipt(row)
        if later["_status"] == "failed" or not later.get("import_skipped"):
            last = later
            break
    return (last is not None and last["_status"] != "failed" and isinstance(last.get("import"), dict)
            and not last["import"].get("error"))


def run(db_path: Path, data: Path, env: dict, adapters=None, dry_run: bool = False, keep: int = 7,
        lock_wait: float = 0, reuse_pending: Path | None = None, keyless: bool = False, keyed=(), run_prefix="ingest",
        incremental: bool = False) -> dict:
    """One run. ``incremental`` (the 15-minute email run) skips the backup, replay and quality pass when
    its reads staged nothing and the pending store's last import succeeded: see ``_store_imported``."""
    import failure_tasks
    import quality
    import record_imports
    from observation_reader import load as load_observations
    import records

    db.configure_logging()
    # A corrected import can be retried immediately; its receipt must not collide with the failed run.
    started_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    clock = time.monotonic()
    adapters = default_adapters(keyless, keyed) if adapters is None else adapters
    receipt = {"started_at": started_at, "adapters": {}}
    if keyless:
        receipt["keyless"] = True
        if keyed:
            receipt["keyed"] = sorted(keyed)
    if reuse_pending is not None:
        reuse_pending = Path(reuse_pending).resolve()
        evidence = reuse_pending / "observations.json"
        if not evidence.is_file() or not load_observations(evidence, sources_only=True).get("sources"):
            raise ValueError("reuse-pending requires observations with source evidence")
        adapters = []
        receipt["reused_pending"] = True
    backups_home = data  # the nightly backup's receipt stays in the real data folder, even for a dry run
    if dry_run:
        scratch = Path(tempfile.mkdtemp(prefix="wo-ingest-dry-"))
        copy = scratch / "work-ops.db"
        source = db.connect(db_path)
        try:
            db.backup(source, copy)
        finally:
            source.close()
        if (data / "pending").exists():
            shutil.copytree(data / "pending", scratch / "data" / "pending")
        db_path, data = copy, scratch / "data"
        receipt["dry_run"] = str(scratch)
    fetch_log = data / "logs" / "fetch.jsonl"
    if run_prefix not in ("ingest", "email-import"):
        raise ValueError("unsupported run prefix")
    run_id = f"{run_prefix}:{started_at}"
    unit = 'wo-email.service' if run_prefix == 'email-import' else 'wo-scan.service'
    job = db.activate_job(run_id, unit)
    child_env = {**os.environ, **env, "WO_FETCH_LOG": str(fetch_log), "WO_FETCH_RUN": run_id}
    try:
        check = db.connect(db_path)
        try:
            if db.pending(check):
                raise RuntimeError(f"{db_path}: run `wo migrate` first")
        finally:
            check.close()
        with writer_lock(data, wait=lock_wait):
            if reuse_pending is not None:
                saved = load_observations(reuse_pending / "observations.json", sources_only=True)["sources"]
                current_path = data / "pending" / "observations.json"
                current = load_observations(current_path, sources_only=True).get("sources", {}) if current_path.exists() else {}
                if any(scope not in saved or (saved[scope].get("observed_at") or "") < (source.get("observed_at") or "")
                       for scope, source in current.items()):
                    raise ValueError("reuse-pending would replace newer or missing source observations")
            phases = receipt["phase_seconds"] = {}
            peaks = receipt["phase_peak_rss_bytes"] = {}
            mark = time.monotonic()
            def phase(name):
                nonlocal mark
                phases[name] = round(time.monotonic() - mark, 1); mark = time.monotonic()
                peaks[name] = _peak_rss(resource.RUSAGE_SELF)  # this process's high-water mark so far
            if reuse_pending is not None and reuse_pending != (data / "pending").resolve():
                shutil.copytree(reuse_pending, data / "pending", dirs_exist_ok=True)
            context_path = data / 'pending' / 'collection-context.json'
            if run_prefix == 'ingest' and adapters and reuse_pending is None:
                import collection_schedule
                conn = db.connect(db_path)
                try:
                    import record_read
                    with record_read.snapshot(conn):
                        collection_state = records.state_context(conn)
                        context = collection_schedule.context(conn, collection_state, started_at)
                        targets = companies_house_targets(conn, collection_state, context['companies'])
                finally:
                    conn.close()
                # Keep only the small manifest/targets while child scanners and import allocate.
                del collection_state
                context_path.parent.mkdir(parents=True, exist_ok=True)
                pending_store.atomic_write_json(context_path, context)
                child_env.update(WO_COLLECTION_CONTEXT=str(context_path), WO_COLLECTION_CALLER='scan')
                if any(name == 'companies house' for name,_ in adapters):
                    pending_store.atomic_write(data / 'pending' / CH_TARGETS, json.dumps(targets))
            boards = [name for name, _ in adapters if name.startswith("boards")]
            protection = data / "pending" / pending_store.PROTECTION
            list_error = None
            if boards:
                # Without the list the scan keeps every advert whole: a lost saving, never lost collection.
                try:
                    conn = db.connect(db_path)
                    try:
                        pending_store.write_protection(protection, at=started_at, **record_imports.protected_sources(conn))
                    finally:
                        conn.close()
                except Exception as error:
                    list_error = type(error).__name__
                    import logging
                    logging.exception("Could not list the source evidence records hold")
            try:
                for name, argv in adapters:
                    argv = [a.replace("{data}", str(data)).replace("{portals}", str(PORTALS)) for a in argv]
                    receipt["adapters"][name] = _run_adapter(argv, child_env)
            finally:
                # Only this run's scan may trust it; a later standalone scan keeps every advert whole.
                protection.unlink(missing_ok=True)
                context_path.unlink(missing_ok=True)
            if boards:
                retention = {"manifest": "unreported"}
                if receipt["adapters"][boards[0]]["returncode"] == 0:
                    try:
                        retention = json.loads((data / "pending" / "last-run.json").read_text())["retention"]
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                receipt["retention"] = dict(retention, **({"list_error": list_error} if list_error else {}))
            phase("adapters")
            conn = db.connect(db_path)
            try:
                receipt["sources"] = _source_states(data, "" if reuse_pending is not None else started_at)
                if (incremental and reuse_pending is None and _staged_nothing(receipt["adapters"], receipt["sources"])
                        and _store_imported(conn)):
                    # A replay would change only these reads' source health: record them the same way,
                    # without the backup, the whole-store replay and the whole-database quality pass.
                    receipt["import_skipped"] = "no new items"
                    observed = load_observations(data / "pending" / "observations.json", sources_only=True)["sources"]
                    with records.transaction(conn):
                        record_imports.record_source_reads(conn, {s: observed[s] for s in receipt["sources"]}, [])
                else:
                    if not dry_run:
                        receipt["backup"] = str(backup(db_path, data / "backups", keep=keep))
                    phase("backup")
                    try:
                        report = record_imports.cmd_import_scans(conn, data / "pending", run_id=run_id)
                    except Exception as error:
                        # cmd_import_scans owns its transaction: failures roll back records and receipts.
                        # Keep the pending evidence untouched and record this separate operational failure.
                        receipt['import'] = {'error': f'{type(error).__name__}: source evidence could not be imported; inspect the service journal.'}
                        receipt['error_kind'] = type(error).__name__
                        import logging
                        logging.exception('Source evidence import failed')
                    else:
                        receipt["import"] = {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()
                                             if isinstance(v, (int, list))}
                        receipt["import"]["write_lock_seconds"] = report.get("write_lock_seconds")
                        receipt["import"]["intake_rejected"] = report.get("intake_rejected", {})
                        receipt["import"]["capacity"] = report.get("capacity", {})
                        # Only after a successful import, so the database already holds what is dropped. Saving
                        # space never fails the run: a store it cannot read is left exactly as it was.
                        try:
                            receipt["import"]["compacted"] = record_imports.compact_observations(conn, data / "pending", db.now())
                        except (OSError, ValueError) as error:
                            receipt["import"]["compaction_error"] = type(error).__name__
                    phase("import")
                    try:
                        receipt["store"] = {"bytes": (data / "pending" / "observations.json").stat().st_size}
                    except OSError:
                        receipt["store"] = {"bytes": None}
                    q = quality.check(conn)
                    receipt["quality"] = {"defects": q["defect_total"], "gaps": q["gap_total"]}
                    receipt["stale"] = unseen_adverts(conn)
                if run_prefix == 'ingest':
                    import collection_reads
                    receipt['collection'] = collection_reads.summary()
                receipt["feed_calls"] = {} if reuse_pending is not None else _feed_calls(data)
                receipt["fetch"] = web_read.summarise(web_read.read_log(fetch_log, run=run_id))
                # A page/request-capped read retains its results: incomplete, not failed.
                failed = [n for n, a in receipt["adapters"].items() if a["returncode"] != 0]
                failed += [s for s, v in receipt["sources"].items() if v["state"] not in ("ok", "empty", "partial")]
                capped = [s for s, v in receipt["sources"].items() if v["state"] == "partial"]
                import_error = receipt.get('import', {}).get('error')
                if import_error:
                    failed.append('import')
                receipt['deferred']={name:a.get('collection_deferrals',{}) for name,a in receipt['adapters'].items() if a.get('state')=='deferred'}
                receipt["status"] = "failed" if import_error else "partial" if failed or capped or receipt['deferred'] else "ok"
                receipt["failures"] = failed
                receipt["capped"] = capped
                previous = conn.execute("SELECT receipt_json FROM scan_runs WHERE run_id LIKE 'ingest:%' ORDER BY run_id DESC LIMIT 1").fetchone()
                phase("checks")
                receipt["finished_at"] = db.now()
                receipt["tasks"] = failure_tasks.for_ingest(conn, receipt, json.loads(previous[0]).get("failures", []) if previous else [])
                receipt["memory"] = memory_evidence()
                receipt["seconds"] = round(time.monotonic() - clock, 1)
                with records.transaction(conn):
                    conn.execute("INSERT INTO scan_runs(run_id, date, receipt_json, status) VALUES (?,?,?,?)",
                                 (run_id, started_at[:10], json.dumps(receipt, sort_keys=True), receipt["status"]))
                # After this run's receipt, so the watch sees it; it reports the backup job's health.
                watched = failure_tasks.watch(conn, backups_home)
                for key in ("raised", "cleared"):
                    receipt["tasks"][key] += watched[key]
                with records.transaction(conn):
                    conn.execute("UPDATE scan_runs SET receipt_json=? WHERE run_id=?", (json.dumps(receipt, sort_keys=True), run_id))
            except Exception as error:
                # Preserve evidence of a failed import/quality phase; never put exception text or raw
                # input into the user-visible receipt. The original failure still propagates to the job.
                receipt.update(status='failed', error_kind=type(error).__name__,
                               failures=sorted(set(receipt.get('failures', [])) | {'daily-ingest'}),
                               message='The ingest did not finish. Inspect the service log before retrying.',
                               seconds=round(time.monotonic() - clock, 1), finished_at=db.now(),
                               memory=memory_evidence())
                with records.transaction(conn):
                    conn.execute("INSERT INTO scan_runs(run_id,date,receipt_json,status) VALUES(?,?,?,?) "
                                 "ON CONFLICT(run_id) DO UPDATE SET receipt_json=excluded.receipt_json,status=excluded.status",
                                 (run_id, started_at[:10], json.dumps(receipt, sort_keys=True), 'failed'))
                raise
            finally:
                conn.close()
        return receipt
    finally:
        db.restore_job(job)


def probe(env: dict) -> int:
    """Live schema probes for the credentialled sources. Reports names, never values."""
    import os
    import credentials
    import yaml
    gaps = missing(env | {k: v for k, v in os.environ.items() if k in FEED_KEYS}, FEED_KEYS)
    if gaps:
        print("Not set: " + ", ".join(gaps))
    child = {**os.environ, **env}
    codes = [subprocess.run([sys.executable, str(ROOT / "tools/scan_boards.py"), "--probe", "all"], env=child).returncode]
    # The local spool uses its own read-only path, not IMAP credentials. Let its probe validate
    # configuration without constructing output stores or advancing a scanner checkpoint.
    alerts = (yaml.safe_load(PORTALS.read_text()) or {}).get("alerts") or {}
    alert_gaps = [] if alerts.get("transport", "imap") == "spool" else [
        key for key in ALERT_KEYS if not credentials.get(key, child)]
    if alert_gaps:
        print("alerts: mailbox not set up (" + ", ".join(alert_gaps) + ")")
    else:
        codes.append(subprocess.run([sys.executable, str(ROOT / "tools/scan_alerts.py"), "--portals", str(PORTALS), "--probe"], env=child).returncode)
    return max(codes)


def format_receipt(r: dict) -> str:
    lines = [f"Ingest {r['status']} in {r.get('seconds', '?')}s" + (f" (dry run: {r['dry_run']})" if r.get("dry_run") else "")]
    if r.get("reused_pending"):
        lines.append("  reused saved observations; no source requests")
    if r.get("keyless"):
        selected = r.get("keyed") or ()
        lines.append(("  free sources" if "alerts" in selected else "  free sources, no mailbox" if "feeds" in selected
                      else "  free sources only: no Adzuna, Reed or mailbox calls")
                     + (f"; approved keyed: {', '.join(r['keyed'])}" if r.get("keyed") else ""))
    for name, a in r["adapters"].items():
        if a["returncode"] == 0 and a.get("state") in ("disabled", "skipped", "deferred"):
            reason="collection tasks deferred; see actual source reads" if a["state"]=="deferred" else a["state"]+"; no source read"
            lines.append(f"  SKIP {name} ({reason}; {a['seconds']}s)")
        else:
            lines.append(f"  {'ok  ' if a['returncode'] == 0 else 'FAIL'} {name} ({a['seconds']}s)")
    fetch = r.get("fetch") or {}
    if fetch.get("requests"):
        lines.append(f"  requests: {fetch['requests']}, {fetch.get('recovered', 0)} recovered on retry"
                     + "".join(f", {n} {kind}" for kind, n in sorted((fetch.get("failed") or {}).items())))
    if r.get("feed_calls"):
        lines.append("  feed calls: " + ", ".join(f"{k}: {v}" for k, v in r["feed_calls"].items()))
    imp = r.get("import", {})
    if r.get("import_skipped"):
        lines.append(f"  import: skipped ({r['import_skipped']})")
    elif imp.get('error'):
        lines.append('  import failed: ' + imp['error'])
    else:
        lines.append("  import: " + ", ".join(f"{k} {imp[k]}" for k in ("new_observed", "attached_sightings", "possible_matches",
                                                                       "commercial_signals", "commercial_organisations_created",
                                                                       "stale_observations", "conflicts", "malformed")
                                              if k in imp))
    if "quality" in r:
        lines.append(f"  quality: {r['quality']['defects']} defects, {r['quality']['gaps']} gaps")
    if r.get("failures"):
        lines.append("  failing: " + ", ".join(r["failures"][:12]))
    tasks = r.get("tasks") or {}
    if tasks.get("raised") or tasks.get("cleared"):
        lines.append(f"  tasks for the owner: {len(tasks.get('raised', []))} raised, {len(tasks.get('cleared', []))} cleared"
                     + (" (" + ", ".join(tasks["raised"][:8]) + ")" if tasks.get("raised") else ""))
    if r.get("capped"):
        lines.append("  read partly (page/request caps): " + ", ".join(r["capped"][:12]))
    return "\n".join(lines)
