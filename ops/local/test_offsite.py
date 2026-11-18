"""The Mac's daily off-host copy waits for the network, drills the copy it kept and leaves a receipt."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = "/srv/offsite/work-ops/work-ops-20261001T021501Z.db.age"
FAKE_SSH = """#!/bin/bash
# Fails like an offline Mac for the first $FAIL_FIRST calls, then answers wo-offsite-fetch.
n=$(($(cat "$CALLS" 2>/dev/null || echo 0) + 1)); echo "$n" > "$CALLS"
if ((n <= FAIL_FIRST)); then echo "ssh: connect to host: Network is unreachable" >&2; exit 255; fi
case "$*" in
  *"wo-offsite-fetch latest") echo "%s" ;;
  *"wo-offsite-fetch get "*) printf 'encrypted-bytes' ;;
esac
""" % BACKUP


class OffsiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "ops").mkdir(parents=True)
        shutil.copyfile(ROOT / "ops/offsite-weekly.sh", self.repo / "ops/offsite-weekly.sh")
        (self.repo / "ops/restore-drill.sh").write_text('#!/bin/bash\n[[ -z "$DRILL_EXIT" ]] || exit "$DRILL_EXIT"\necho "drill passed $*"\n')
        (self.repo / "ops/restore-drill.sh").chmod(0o755)
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "ssh").write_text(FAKE_SSH)
        (bin_dir / "ssh").chmod(0o755)
        self.calls = Path(self.tmp.name) / "calls"
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(self.calls),
                    "OFFSITE_PAUSE": "0", "SSH_KEY": str(bin_dir / "ssh")}

    def run_job(self, fail_first, tries, **env):
        return subprocess.run(["/bin/bash", str(self.repo / "ops/offsite-weekly.sh")], capture_output=True, text=True,
                              env={**self.env, "FAIL_FIRST": str(fail_first), "OFFSITE_TRIES": str(tries), **env})

    def receipt(self):
        return json.loads((self.repo / "data/offsite/last-run.json").read_text())

    def test_it_waits_for_the_network_then_keeps_the_copy_and_runs_the_drill(self):
        result = self.run_job(fail_first=3, tries=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = self.repo / "data/offsite" / Path(BACKUP).name
        self.assertEqual(kept.read_text(), "encrypted-bytes")
        self.assertIn(f"drill passed {kept}", result.stdout)  # the drill restores the copy just kept, not a second fetch
        self.assertNotIn("FAILED", result.stdout)
        self.assertEqual(self.receipt()["result"], "passed")
        self.assertEqual(self.receipt()["backup"], kept.name)

    def test_it_fails_loudly_in_its_own_log_when_the_server_stays_unreachable(self):
        result = self.run_job(fail_first=99, tries=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text().strip(), "3")
        self.assertIn("FAILED: the off-host copy or its restore drill did not complete", result.stdout)
        self.assertIn("not reachable after 3 tries", result.stderr)
        self.assertFalse(list((self.repo / "data").glob("offsite/*.age")))
        self.assertEqual(self.receipt()["result"], "failed")

    def test_a_failed_drill_leaves_a_failed_receipt(self):
        result = self.run_job(fail_first=0, tries=1, DRILL_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED", result.stdout)
        self.assertEqual(self.receipt(), {**self.receipt(), "result": "failed", "backup": Path(BACKUP).name})

    def test_a_copy_that_restores_with_quality_defects_is_not_a_failed_backup(self):
        result = self.run_job(fail_first=0, tries=1, DRILL_EXIT="3")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("FAILED", result.stdout)
        self.assertIn("RESTORED, with data defects", result.stdout)
        self.assertEqual(self.receipt()["result"], "defects")

    def test_an_interrupted_earlier_download_does_not_stay_forever(self):
        stale = self.repo / "data/offsite/work-ops-20261003T021512Z.db.age.part"
        stale.parent.mkdir(parents=True)
        stale.write_text("partial")
        os.utime(stale, (1, 1))
        self.assertEqual(self.run_job(fail_first=0, tries=1).returncode, 0)
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
