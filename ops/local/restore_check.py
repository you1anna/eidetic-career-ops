#!/usr/bin/env python3
"""The restore drill's database checks (ops/restore-drill.sh), run on a scratch copy of a backup.

A backup restores when its integrity check passes, it holds records, and its schema is this
checkout's migrations or an earlier prefix of them. An earlier schema is brought forward with the
code's migrations, as restoring it onto newer code would be: the Mac's code is often ahead of the
deployed site. A migration this checkout does not have means the checkout is behind; pull and rerun.

Usage: restore_check.py <scratch.db>
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workspace"))
import db  # noqa: E402


class DrillFailed(Exception):
    pass


def check(path: Path, migrations: Path = db.MIGRATIONS) -> str:
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if ro.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DrillFailed("integrity check failed")
        try:
            have = sorted(r[0] for r in ro.execute("SELECT version FROM schema_version"))
        except sqlite3.OperationalError:
            raise DrillFailed("no schema_version table: not a work-ops database") from None
    finally:
        ro.close()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
    import backup_collection
    try:accounting=backup_collection.verify(path)
    except (OSError,ValueError,sqlite3.Error) as error:raise DrillFailed('Collection accounting restore failed') from error
    want = [v for v, _ in db.migration_files(migrations)]
    if unknown := sorted(set(have) - set(want)):
        raise DrillFailed(f"the backup has migrations {unknown} that this checkout lacks: git pull, then rerun")
    if not have or have != want[:len(have)]:
        raise DrillFailed(f"backup schema {have} is not a prefix of the code's migrations {want}")
    conn = db.connect(path)
    try:
        try:
            applied = db.migrate(conn, migrations)
        except sqlite3.Error as e:
            raise DrillFailed(f"bringing schema {have[-1]} forward failed: {e}") from None
        records = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        observations = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    finally:
        conn.close()
    if not records:
        raise DrillFailed("no records")
    moved = f", migrated to {want[-1]}" if applied else ""
    collection="collection accounting verified" if accounting else "historical backup without collection accounting"
    return f"restore ok: integrity ok, schema {have[-1]}{moved}, {records} records, {observations} observations; {collection}"


def main(argv: list[str]) -> int:
    try:
        print(check(Path(argv[1])))
    except DrillFailed as e:
        print(f"restore FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
