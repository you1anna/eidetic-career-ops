"""Safety checks for database restore and whole-store JSON replacement."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "workspace"))

import db as wodb  # noqa: E402
import wo  # noqa: E402


def database(path: Path, names: tuple[str, ...]) -> sqlite3.Connection:
    conn = wodb.connect(path)
    wodb.migrate(conn)
    for name in names:
        wo.store.create(conn, {"type": "person", "name": name})
    return conn


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_restore_only_publishes_a_new_validated_database(self):
        live = database(self.root / "live.db", ("Keep me",))
        backup = self.root / "backup.db"
        wodb.backup(live, backup)
        live.close()

        restored = self.root / "restored.db"
        self.assertEqual(wodb.restore(backup, restored), restored.resolve())
        self.assertTrue(backup.is_file())
        check = sqlite3.connect(restored)
        try:
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(check.execute("SELECT name FROM records").fetchone()[0], "Keep me")
        finally:
            check.close()

        with self.assertRaisesRegex(ValueError, "already exists"):
            wodb.restore(backup, restored)

    def test_restore_rejects_non_work_ops_file_without_publishing(self):
        source = self.root / "other.db"
        sqlite3.connect(source).close()
        destination = self.root / "must-not-exist.db"
        with self.assertRaisesRegex(ValueError, "work-ops"):
            wodb.restore(source, destination)
        self.assertFalse(destination.exists())


class ImportReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = database(self.root / "target.db", ("One", "Two"))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def payload_with(self, names: tuple[str, ...]) -> dict:
        path = self.root / "source.db"
        source = database(path, names)
        try:
            return wo.dump(source)
        finally:
            source.close()

    def test_nonempty_database_requires_replace_and_count_reduction_permission(self):
        payload = self.payload_with(("Replacement",))
        with self.assertRaisesRegex(ValueError, "--replace"):
            wo.load_dump(self.conn, payload)
        with self.assertRaisesRegex(ValueError, "--allow-count-reduction"):
            wo.load_dump(self.conn, payload, replace=True)
        wo.load_dump(self.conn, payload, replace=True, allow_count_reduction=True)
        self.assertEqual(self.conn.execute("SELECT name FROM records").fetchone()[0], "Replacement")

    def test_empty_and_logically_orphaned_exports_are_rejected_atomically(self):
        payload = wo.dump(self.conn)
        payload["tables"]["records"] = []
        with self.assertRaisesRegex(ValueError, "no records"):
            wo.load_dump(self.conn, payload, replace=True, allow_count_reduction=True)

        payload = wo.dump(self.conn)
        payload["tables"]["drafts"] = [{"record_id": "missing", "title": "", "text": "private",
                                        "version_id": None, "purpose": "approval"}]
        with self.assertRaisesRegex(ValueError, "drafts.record_id"):
            wo.load_dump(self.conn, payload, replace=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)

    def test_cli_replace_makes_a_consistent_backup(self):
        payload_path = self.root / "dump.json"
        payload_path.write_text(json.dumps(self.payload_with(("Three", "Four"))))
        self.assertEqual(wo.main(["--db", str(self.root / "target.db"), "import-json",
                                  str(payload_path), "--replace"]), 0)
        backups = list(self.root.glob("target.db.pre-import-*.db"))
        self.assertEqual(len(backups), 1)
        backup = sqlite3.connect(backups[0])
        try:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)
        finally:
            backup.close()


if __name__ == "__main__":
    unittest.main()
