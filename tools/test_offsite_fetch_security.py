"""Only scratch bytes: run the forced-command download helper without SSH or backups."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class OffsiteFetchSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wo-offsite-security-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = self.root / "store"
        self.store.mkdir()
        self.name = "work-ops-20261024T120000Z.db.age"
        self.backup = self.store / self.name
        self.outside = self.root / "outside"
        self.outside.write_bytes(b"must not be disclosed")
        # Replace only the fixed deployment directory, never consult a real backup.
        source = (ROOT / "ops/vps/wo-offsite-fetch").read_text()
        source = source.replace("/srv/offsite/work-ops", str(self.store))
        self.script = self.root / "fetch.sh"
        self.script.write_text(source)

    def run_fetch(self, *args):
        return subprocess.run(["bash", str(self.script), *map(str, args)],
            env={**os.environ, "SSH_ORIGINAL_COMMAND": ""},
            capture_output=True, timeout=5)

    def test_regular_archive_round_trips_bytes(self):
        self.backup.write_bytes(b"synthetic ciphertext\x00\xff")
        result = self.run_fetch("get", self.backup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.backup.read_bytes())
        self.assertEqual(self.run_fetch("latest").stdout.strip(), os.fsencode(self.backup))

    def test_symlink_is_never_downloaded_or_advertised(self):
        self.backup.symlink_to(self.outside)
        for args in (("get", self.backup), ("latest",)):
            result = self.run_fetch(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")

    def test_hard_link_and_fifo_are_refused_without_bytes_or_hang(self):
        os.link(self.outside, self.backup)
        result = self.run_fetch("get", self.backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertNotEqual(self.run_fetch("latest").returncode, 0)
        self.backup.unlink()
        os.mkfifo(self.backup)
        result = self.run_fetch("get", self.backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_replaced_directory_cannot_redirect_a_download(self):
        self.backup.write_bytes(b"old ciphertext")
        moved = self.root / "moved"
        self.store.rename(moved)
        self.store.symlink_to(moved, target_is_directory=True)
        result = self.run_fetch("get", self.backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_parent_directory_symlink_is_refused(self):
        self.backup.write_bytes(b"ciphertext")
        linked = self.root / "linked-parent"
        linked.symlink_to(self.root, target_is_directory=True)
        script = self.script.read_text().replace(str(self.store), str(linked / "store"))
        self.script.write_text(script)
        result = self.run_fetch("get", linked / "store" / self.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_extra_arguments_and_outside_paths_are_refused(self):
        self.backup.write_bytes(b"ciphertext")
        for args in (("get", self.outside), ("get", self.backup, "extra"), ("latest", "extra")):
            self.assertNotEqual(self.run_fetch(*args).returncode, 0)


if __name__ == "__main__":
    unittest.main()
