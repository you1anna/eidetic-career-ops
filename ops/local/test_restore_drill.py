"""The restore drill accepts a backup one deploy behind this checkout and fails only on real faults."""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import restore_check  # noqa: E402

BASE = """CREATE TABLE schema_version(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
CREATE TABLE records(id TEXT PRIMARY KEY);
CREATE TABLE observations(id TEXT PRIMARY KEY);"""


class RestoreCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.code = self.dir / "code"
        self.code.mkdir()
        (self.code / "001_base.sql").write_text(BASE)
        (self.code / "002_more.sql").write_text("ALTER TABLE records ADD COLUMN name TEXT;")

    def backup(self, versions, records=1, extra_sql=""):
        path = self.dir / "restored.db"
        conn = sqlite3.connect(path)
        conn.executescript(BASE + extra_sql)
        conn.executemany("INSERT INTO schema_version(version,name) VALUES(?,'x')", [(v,) for v in versions])
        if 2 in versions:
            conn.execute("ALTER TABLE records ADD COLUMN name TEXT")
        conn.executemany("INSERT INTO records(id) VALUES(?)", [(f"r{i}",) for i in range(records)])
        conn.commit()
        conn.close()
        return path

    def test_a_backup_at_the_code_schema_passes(self):
        self.assertEqual(restore_check.check(self.backup([1, 2]), self.code),
                         "restore ok: integrity ok, schema 2, 1 records, 0 observations; historical backup without collection accounting")

    def test_a_backup_one_deploy_behind_is_migrated_forward_on_the_scratch_copy(self):
        path = self.backup([1])
        self.assertEqual(restore_check.check(path, self.code),
                         "restore ok: integrity ok, schema 1, migrated to 2, 1 records, 0 observations; historical backup without collection accounting")
        conn = sqlite3.connect(path)
        self.assertEqual([r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY 1")], [1, 2])
        self.assertIn("name", [r[1] for r in conn.execute("PRAGMA table_info(records)")])

    def test_a_backup_ahead_of_this_checkout_asks_for_a_pull(self):
        with self.assertRaisesRegex(restore_check.DrillFailed, r"migrations \[3\] that this checkout lacks: git pull"):
            restore_check.check(self.backup([1, 2, 3]), self.code)

    def test_a_schema_with_a_gap_is_not_a_prefix(self):
        (self.code / "003_last.sql").write_text("SELECT 1;")
        with self.assertRaisesRegex(restore_check.DrillFailed, r"not a prefix"):
            restore_check.check(self.backup([1, 3]), self.code)

    def test_an_empty_backup_fails(self):
        with self.assertRaisesRegex(restore_check.DrillFailed, "no records"):
            restore_check.check(self.backup([1, 2], records=0), self.code)

    def test_a_file_that_is_not_a_work_ops_database_fails(self):
        path = self.dir / "other.db"
        sqlite3.connect(path).executescript("CREATE TABLE t(x);")
        with self.assertRaisesRegex(restore_check.DrillFailed, "no schema_version table"):
            restore_check.check(path, self.code)

    def test_a_migration_that_fails_on_the_backup_fails_the_drill(self):
        (self.code / "003_broken.sql").write_text("ALTER TABLE missing ADD COLUMN x TEXT;")
        with self.assertRaisesRegex(restore_check.DrillFailed, "bringing schema 2 forward failed: no such table: missing"):
            restore_check.check(self.backup([1, 2]), self.code)

    def test_the_command_reports_a_failure_on_stderr_with_exit_1(self):
        path = self.dir / "other.db"
        sqlite3.connect(path).executescript("CREATE TABLE t(x);")
        result = subprocess.run([sys.executable, str(ROOT / "ops/local/restore_check.py"), str(path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "restore FAILED: no schema_version table: not a work-ops database\n")


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age is not installed")
class DrillScriptTests(unittest.TestCase):
    def drill(self, sql=""):
        """Run ops/restore-drill.sh on an encrypted synthetic fixture, with ssh unusable."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = tmp / "fixture.db"
            subprocess.run([sys.executable, str(ROOT / "tools/make_fixture.py"), str(db)], check=True, capture_output=True)
            if sql:
                conn = sqlite3.connect(db)
                conn.execute(sql)
                conn.commit()
                conn.close()
            key = tmp / "key.txt"
            subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
            recipient = subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout.strip()
            archive = tmp / "work-ops-20261014T021500Z.db.age"
            subprocess.run(["age", "-r", recipient, "-o", str(archive), str(db)], check=True)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            (bin_dir / "ssh").write_text("#!/bin/bash\necho 'ssh must not run' >&2; exit 99\n")
            (bin_dir / "ssh").chmod(0o755)
            env = {**os.environ, "AGE_KEY": str(key), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            return subprocess.run(["/bin/bash", str(ROOT / "ops/restore-drill.sh"), str(archive)],
                                  capture_output=True, text=True, env=env)

    def test_the_drill_restores_a_local_encrypted_copy_without_fetching_again(self):
        result = self.drill()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("decrypted work-ops-20261014T021500Z.db.age", result.stdout)
        self.assertIn("restore ok: integrity ok", result.stdout)
        self.assertIn("drill passed", result.stdout)

    def test_quality_defects_are_reported_apart_from_the_restore(self):
        # A copy that restores is not a failed backup; its defects are a data problem (plan item 9, 1 Oct).
        result = self.drill("UPDATE records SET next_action='or call them' WHERE id=(SELECT MIN(id) FROM records WHERE archived_at IS NULL)")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("restore ok: integrity ok", result.stdout)
        self.assertIn("1 defects", result.stdout)
        self.assertIn("truncated_action", result.stderr)
        self.assertIn("the copy restores, but wo quality --strict found defects", result.stderr)
        self.assertNotIn("drill passed", result.stdout)

    def test_a_quality_check_that_cannot_run_is_a_failed_restore_not_defects(self):
        # quality reads records.next_action; without the column it crashes, which must not read as "defects".
        result = self.drill("ALTER TABLE records DROP COLUMN next_action")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("restore FAILED: wo quality did not run on the restored copy", result.stderr)


if __name__ == "__main__":
    unittest.main()
