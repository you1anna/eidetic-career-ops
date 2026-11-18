"""The work-ops SQLite store: connection, numbered migrations, backup and restore.

Standard library only, and no `sqlite3` command-line tool: the VPS has the module but not the
binary, so backups go through `Connection.backup()`.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import traceback
import weakref

import normalise

HERE = Path(__file__).resolve().parent
MIGRATIONS = HERE / "migrations"
# Scanner observations are kept once per content: a daily re-sighting of an unchanged advert updates
# its row's timing instead of adding a row. Every other observation (research, LinkedIn, review, news)
# is one row per sighting. The same predicate names both partial unique indexes (migration 005).
SCANNER_SOURCES = ("board:", "feed:", "hn:", "tender:")
SCANNER_SQL = "(source GLOB 'board:*' OR source GLOB 'feed:*' OR source GLOB 'hn:*' OR source GLOB 'tender:*')"
TIMING_FIELDS = ("last_seen_at", "last_complete_seen_at", "observed_at")


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def observation_sha(source: str, payload_json: str) -> str:
    """The content key: scanners and awards omit reread timing, retaining source facts and identity.

    Awards use the same content rule but are deduplicated transactionally by add_observation;
    they are deliberately outside SCANNER_SQL, which must still match migration 005's indexes.
    """
    text = payload_json
    award = source.startswith('award:')
    if source.startswith(SCANNER_SOURCES) or award:
        try:
            payload = json.loads(payload_json)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            timing = (*TIMING_FIELDS, 'last_observation_at') if award else TIMING_FIELDS
            text = json.dumps({k: v for k, v in payload.items() if k not in timing}, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def observation_time_key(value) -> str:
    """Fixed-width UTC comparison key; retain the original timestamp in stored evidence."""
    try:
        stamp = normalise.observation_time(value)
    except OverflowError:  # An offset can place an otherwise valid ISO date outside datetime's range.
        stamp = normalise.observation_time(None)
    return stamp.isoformat(timespec='microseconds')


def failure_location(error: BaseException) -> str:
    """Where a failure was raised: the last three repository frames as `file:line function`, innermost
    first. Never the exception's message, which can carry page, model or record text."""
    root = str(HERE.parent) + os.sep
    frames = [f for f in traceback.extract_tb(error.__traceback__) if f.filename.startswith(root)][-3:]
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames))


def note_failure(data: Path | str, job: str, error: BaseException, now: dt.datetime | None = None) -> None:
    """Leave a failure's cause in <data>/crashes/<job>.json for the caretaker. A plain file still works
    when the database is what failed. Only sqlite's own message is kept: other text may hold input."""
    try:
        where = failure_location(error)
        text = type(error).__name__ + (f": {str(error)[:120]}" if isinstance(error, sqlite3.Error) else "")
        folder = Path(data) / "crashes"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f"{job}.json"
        at = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
        try:
            recent = [a for a in json.loads(path.read_text()).get("recent", []) if isinstance(a, str)][-9:]
        except (OSError, ValueError, AttributeError):
            recent = []
        part = path.with_suffix(".part")
        part.write_text(json.dumps(dict(job=job, at=at, error=text, where=where, pid=os.getpid(),
                                        run_id=os.environ.get('WO_RUN_ID') or '', recent=recent + [at])))
        part.chmod(0o600)
        part.replace(path)
    except Exception:
        pass  # Never hide the original failure behind a note that could not be written.


@contextmanager
def failures_noted(data: Path | str, job: str):
    try:
        yield
    except Exception as error:
        note_failure(data, job, error)
        raise


def failure_summary(data: Path | str, job: str, now: dt.datetime) -> tuple[str, int]:
    """The latest noted cause for a card, and how many failures fell in the last 24 hours."""
    try:
        note = json.loads((Path(data) / "crashes" / f"{job}.json").read_text())
        recent = []
        for at in note.get("recent") or [note.get("at")]:
            t = dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
            if t.tzinfo and dt.timedelta(0) <= now - t < dt.timedelta(hours=24):
                recent.append(at)
    except (OSError, ValueError, AttributeError, TypeError):
        return "", 0
    if not recent:
        return "", 0
    count = f" ({len(recent)} in 24 h)" if len(recent) > 1 else ""
    run = note.get('run_id') or ''
    run_text = f" run {run}" if run else ""
    return f" Last error {note.get('at')}: {note.get('error')} at {note.get('where') or 'unknown'}{run_text}{count}.", len(recent)


# Seconds a background worker waits for another writer, such as an email import every 15 minutes;
# the site keeps the shorter default so a click never hangs that long.
WORKER_LOCK_WAIT = 120.0
# One journal line once a writer has already been waiting this long. The handler still gives up
# at the connection timeout; it does not record the statement or its values.
BUSY_LOG_SECONDS = 0.2


def configure_logging():
    """Stderr lines for one-shot jobs. The site process configures its own handler first."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='%(levelname)s %(message)s')


def activate_job(run_id: str, unit: str) -> tuple:
    """Publish the current run on the process so a lock wait can name it. Returns the previous pair."""
    previous = (os.environ.get('WO_RUN_ID'), os.environ.get('WO_UNIT'))
    os.environ['WO_RUN_ID'] = run_id
    os.environ['WO_UNIT'] = unit
    return previous


def restore_job(previous: tuple) -> None:
    for key, value in zip(('WO_RUN_ID', 'WO_UNIT'), previous):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@contextmanager
def job_context(run_id: str, unit: str):
    previous = activate_job(run_id, unit)
    try:
        yield
    finally:
        restore_job(previous)


class _BusyWait:
    """One connection's writer wait, read by the busy callback."""
    __slots__ = ('timeout', 'started', 'logged', '__weakref__')

    def __init__(self, timeout: float):
        self.timeout, self.started, self.logged = timeout, None, False


class Connection(sqlite3.Connection):
    """A connection that carries its own lock wait.

    The base class accepts neither attributes nor weak references, so the wait used to live in
    a 256-entry table keyed by `id()`. A connection still open after 256 later ones lost its
    entry, and with it the whole wait: its next contended write failed at once.
    """
    lock_wait: float = 0.0
    _busy: _BusyWait | None = None


# Python 3.12 has no Connection.set_busy_handler, so the callback is registered on the sqlite3*
# that sits immediately after PyObject_HEAD in that version's connection struct. Each wait is
# owned by its connection and found here by that pointer; the entry goes when the connection does.
_BUSY_STATE: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_SQLITE = None
_BUSY_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int)


@_BUSY_CALLBACK
def _on_busy(data, count):
    state = _BUSY_STATE.get(data)
    if state is None:
        return 0
    try:
        now = time.monotonic()
        if count == 0 or state.started is None:
            state.started = now
            state.logged = False
        elapsed = now - state.started
        timeout = state.timeout
        if not state.logged and elapsed >= BUSY_LOG_SECONDS:
            state.logged = True
            logging.warning(
                'database is locked unit=%s run_id=%s pid=%s wait_ms=%d',
                os.environ.get('WO_UNIT') or '-', os.environ.get('WO_RUN_ID') or '-',
                os.getpid(), round(elapsed * 1000))
        if elapsed >= timeout:
            state.started = None
            return 0
        time.sleep(min(0.02, max(0.0, timeout - elapsed)))
        return 1
    except Exception:
        return 0


def _linked_libsqlite() -> str | None:
    """The libsqlite the sqlite3 module is linked to. Another copy on the machine has a different struct."""
    import _sqlite3
    import subprocess
    so = getattr(_sqlite3, '__file__', None)
    if not so:  # compiled into the interpreter (uv's Python on the VPS): symbols hidden, keep busy_timeout
        return None
    try:
        if sys.platform == 'darwin':
            lines = subprocess.check_output(['otool', '-L', so], text=True).splitlines()[1:]
            for line in lines:
                path = line.strip().split(' (', 1)[0]
                if 'libsqlite3' in Path(path).name:
                    return path
        else:
            for line in subprocess.check_output(['ldd', so], text=True).splitlines():
                if 'libsqlite3' not in line or '=>' not in line:
                    continue
                path = line.split('=>', 1)[1].strip().split()[0]
                if path.startswith('/'):
                    return path
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _sqlite_lib():
    global _SQLITE
    if _SQLITE is False:
        return None
    if _SQLITE is not None:
        return _SQLITE
    path = _linked_libsqlite()
    if not path:
        _SQLITE = False
        return None
    lib = ctypes.CDLL(path)
    version = lib.sqlite3_libversion
    version.restype = ctypes.c_char_p
    if version().decode() != sqlite3.sqlite_version:
        _SQLITE = False
        return None
    lib.sqlite3_busy_handler.argtypes = [ctypes.c_void_p, _BUSY_CALLBACK, ctypes.c_void_p]
    lib.sqlite3_busy_handler.restype = ctypes.c_int
    _SQLITE = lib
    return lib


def _sqlite_db(conn: sqlite3.Connection) -> int | None:
    if sys.version_info[:2] != (3, 12):
        return None
    return ctypes.cast(id(conn), ctypes.POINTER(ctypes.c_void_p))[2]


def lock_timeout(conn: sqlite3.Connection) -> float:
    """Seconds this connection waits for a writer. The busy handler owns the wait, not busy_timeout."""
    return conn.lock_wait


def _install_busy_handler(conn: Connection, timeout: float) -> None:
    """Replace SQLite's busy timeout with the same deadline, and log one wait past BUSY_LOG_SECONDS."""
    conn.lock_wait = float(timeout)
    db_ptr = _sqlite_db(conn)
    lib = _sqlite_lib() if db_ptr else None
    if not lib:
        return
    state = _BusyWait(conn.lock_wait)
    _BUSY_STATE[db_ptr] = state
    if lib.sqlite3_busy_handler(ctypes.c_void_p(db_ptr), _on_busy, ctypes.c_void_p(db_ptr)) != 0:
        _BUSY_STATE.pop(db_ptr, None)
        return
    conn._busy = state


def connect(path: Path | str, timeout: float = 15) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None, factory=Connection)
    os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    _install_busy_handler(conn, timeout)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.create_function("wo_observation_sha", 2, observation_sha, deterministic=True)
    conn.create_function("wo_observation_time", 1, observation_time_key, deterministic=True)
    return conn


def migration_files(folder: Path = MIGRATIONS) -> list[tuple[int, Path]]:
    return sorted((int(m.group(1)), p) for p in folder.glob("[0-9][0-9][0-9]_*.sql")
                  if (m := re.match(r"(\d{3})_", p.name)))


def pending(conn: sqlite3.Connection, folder: Path = MIGRATIONS) -> list[tuple[int, Path]]:
    files = migration_files(folder)
    have = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")} if have else set()
    return [(v, p) for v, p in files if v not in done]


def migrate(conn: sqlite3.Connection, folder: Path = MIGRATIONS) -> list[str]:
    """Apply every unapplied numbered migration, in order. A no-op when all are applied."""
    applied = []
    for version, path in pending(conn, folder):
        try:
            # executescript commits anything pending first, so the transaction lives in the script.
            name = path.stem.replace("'", "''")
            stamp = now().replace("'", "''")
            conn.executescript("BEGIN;\n" + path.read_text(encoding="utf-8") +
                               f"\nINSERT INTO schema_version(version,name,applied_at) VALUES({version},'{name}','{stamp}');\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(path.name)
    return applied


def version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


# How many of each automatic backup to keep, newest first; any other backup is kept for 30 days.
BACKUP_KEEP = {"work-ops-pre-ingest-": 7, "work-ops-pre-phone-": 3}
BACKUP_DAYS = 30


def prune_backups(folder: Path | str, now: float | None = None, apply: bool = True) -> list[Path]:
    """The backups past the retention rules (with their -wal/-shm files), deleted unless `apply` is
    false. The newest backup is always kept, and files that are not `.db` backups are never touched."""
    import time
    folder = Path(folder)
    now = time.time() if now is None else now
    dbs = sorted(folder.glob("*.db"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)  # names carry the time
    if not dbs:
        return []
    doomed = []
    for prefix, keep in BACKUP_KEEP.items():
        doomed += [p for p in dbs if p.name.startswith(prefix)][keep:]
    counted = tuple(BACKUP_KEEP)
    doomed += [p for p in dbs[1:] if not p.name.startswith(counted) and now - p.stat().st_mtime > BACKUP_DAYS * 86400]
    doomed = [p for p in doomed if p != dbs[0]]
    out = []
    for path in sorted(set(doomed)):
        out += [x for x in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if x.exists()]
    if apply:
        for x in out:
            x.unlink()
    return out


def backup(conn: sqlite3.Connection, dest: Path | str) -> Path:
    """Consistent copy of a live database, WAL included, without the sqlite3 binary."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    os.chmod(dest, 0o600)
    return dest


def restore(source: Path | str, dest: Path | str) -> Path:
    """Validate and publish a backup at a new path without touching a live store."""
    source = Path(source).resolve()
    dest = Path(dest).resolve()
    if not source.is_file():
        raise ValueError('Backup does not exist')
    if source == dest:
        raise ValueError('Backup and destination must be different')
    if dest.exists():
        raise ValueError('Destination already exists; restore to a new path, stop the service, then switch databases')
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    temp_path = None
    try:
        if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup failed integrity check')
        if not src.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone():
            raise ValueError('Not a work-ops backup')
        fd, name = tempfile.mkstemp(prefix='.' + dest.name + '.restore-', suffix='.db', dir=dest.parent)
        os.close(fd)
        temp_path = Path(name)
        target = sqlite3.connect(temp_path)
        try:
            src.backup(target)
        finally:
            target.close()
        check = sqlite3.connect(temp_path.as_uri() + '?mode=ro', uri=True)
        try:
            if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Restored database failed integrity check')
            if not check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
                raise ValueError('Restored database has no work-ops schema')
        finally:
            check.close()
        os.chmod(temp_path, 0o600)
        try:
            os.link(temp_path, dest)
        except FileExistsError as exc:
            raise ValueError('Destination was created during restore; choose a new restored path') from exc
    finally:
        src.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return dest
