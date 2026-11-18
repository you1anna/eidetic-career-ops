"""Run the nightly backup script in scratch storage, including interrupted encryption."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workspace"))
import db as wodb


class NightlyBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wo backup test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.offsite = self.root / "offsite"
        self.bin = self.root / "bin"
        self.offsite.mkdir()
        self.bin.mkdir()
        conn = wodb.connect(self.data / "work-ops.db")
        wodb.migrate(conn)
        conn.close()
        import collection_budget
        collection_budget.initialize(self.data/'collection.db')
        # Redirect only deployment paths; run the production shell control flow and SQLite backup.
        script = (ROOT / "ops/vps/wo-backup.sh").read_text()
        for name, original, value in (
            ("DATA", "/srv/work-ops/data", self.data),
            ("CODE", "/srv/work-ops/work-ops", ROOT),
            ("PY", "/srv/work-ops/venv/bin/python", Path(sys.executable)),
            ("OFFSITE", "/srv/offsite/work-ops", self.offsite),
        ):
            self.assertEqual(script.count(f"{name}={original}\n"), 1)
            script = script.replace(f"{name}={original}\n", f"{name}={shlex.quote(str(value))}\n")
        self.script = self.root / "backup.sh"
        self.script.write_text(script)
        # flock is a Linux utility; this test runs sequentially, including on the Mac.
        self.command("flock", "#!/bin/sh\nexit 0\n")
        self.command("date", "#!/bin/sh\ncase \"$2\" in\n"
                     "+%F) echo 2026-09-28;;\n"
                     "+%Y%m%dT%H%M%SZ) echo \"$WO_TEST_STAMP\";;\n"
                     "+%FT%TZ) echo 2026-09-28T02:15:00Z;;\n"
                     "*) exit 2;;\nesac\n")
        # Inject a mid-write encryption failure; archive publication remains the real shell code.
        self.command("age", f"#!{sys.executable}\n"
                     "import os, sys\nfrom pathlib import Path\n"
                     "assert sys.argv[1] == '-R' and sys.argv[3] == '-o'\n"
                     "assert Path(sys.argv[2]).is_file() and Path(sys.argv[5]).is_file()\n"
                     "failed = os.environ.get('WO_TEST_FAIL_ENCRYPTION') == '1'\n"
                     "Path(sys.argv[4]).write_bytes(b'incomplete' if failed else b'completed')\n"
                     "sys.exit(1 if failed else 0)\n")

    def command(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def backup(self, stamp, fail=False):
        return subprocess.run(
            ["bash", str(self.script)], capture_output=True, text=True, timeout=20,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                 "WO_TEST_STAMP": stamp, "WO_TEST_FAIL_ENCRYPTION": "1" if fail else "0"},
        )

    def test_success_publishes_the_completed_archive_and_receipt(self):
        result = self.backup("20260928T021500Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.offsite / "work-ops-20260928T021500Z.db.age").read_bytes(), b"completed")
        self.assertEqual(list(self.offsite.glob("*.part")), [])
        receipt = json.loads((self.data / "backups/last-backup.json").read_text())
        self.assertEqual(receipt["file"], "work-ops-2026-09-28.db")
        import backup_collection
        self.assertTrue(backup_collection.verify(self.data/'backups/work-ops-2026-09-28.db'))

    def test_encryption_failure_preserves_the_previous_backup_and_receipt(self):
        previous = self.backup("20260928T011500Z")
        self.assertEqual(previous.returncode, 0, previous.stderr)
        receipt = self.data / "backups/last-backup.json"
        before = receipt.read_bytes(), receipt.stat().st_mtime_ns
        result = self.backup("20260928T021500Z", fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((receipt.read_bytes(), receipt.stat().st_mtime_ns), before)
        self.assertEqual((self.offsite / "work-ops-20260928T011500Z.db.age").read_bytes(), b"completed")
        self.assertFalse((self.offsite / "work-ops-20260928T021500Z.db.age").exists())
        self.assertNotIn("backup ok:", result.stdout)

    def test_success_after_failure_never_publishes_the_abandoned_partial(self):
        failed = self.backup("20260928T011500Z", fail=True)
        self.assertNotEqual(failed.returncode, 0)
        partial = self.offsite / "work-ops-20260928T011500Z.db.age.part"
        self.assertEqual(partial.read_bytes(), b"incomplete")
        result = self.backup("20260928T021500Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in self.offsite.glob("*.age")),
                         ["work-ops-20260928T021500Z.db.age"])
        self.assertEqual(partial.read_bytes(), b"incomplete")


if __name__ == "__main__":
    unittest.main()
