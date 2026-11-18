"""Exercise archive boundaries and remote scratch cleanup without SSH or live data."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class VpsVerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wo verify ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "trace"
        files = {
            ".gitignore": ".env\n", ".python-version": "3.12\n",
            "requirements.txt": "", "workspace/example.py": "# committed\n",
            "tools/example.py": "", "config/example.yml": "",
            "tests/ui/package.json": "{}", "ops/local/example.py": "",
            "ops/vps/example.service": "", "ops/vps/example.timer": "",
            "ops/vps/example.conf": "", "ops/vps/wo": "#!/bin/bash\n",
            "ops/vps/wo-offsite-fetch": "#!/bin/bash\n",
            "ops/backup-recipient.txt": "public recipient\n",
            "records/private.json": "private", "evidence/private.txt": "private",
            "provenance/private.txt": "private", "data/work-ops.db": "private",
            ".env": "private", "keys/backup.agekey": "private",
            "ops/check.sh": '#!/bin/bash\nset -eu\n'
                '[[ "$*" == "--strict unit contract" ]]\n'
                '[[ -z ${WO_DB:-}${WO_CONTRACT_DB:-}${WO_UI_DB:-} ]]\n'
                '[[ ! -e data/work-ops.db && ! -e records && ! -e .env ]]\n'
                'git check-ignore -q .env\n'
                'cat workspace/example.py > "$WO_VERIFY_TRACE"\n'
                'exit "${WO_VERIFY_EXIT:-0}"\n',
        }
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.script = self.repo / "ops/vps/verify.sh"
        shutil.copyfile(ROOT / "ops/vps/verify.sh", self.script)
        (self.repo / "ops/check.sh").chmod(0o755)
        self.git("init", "-q")
        self.git("add", "-f", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        # Run the actual remote shell program, replacing only host-specific paths and sudo.
        ssh = self.bin / "ssh"
        ssh.write_text("#!/usr/bin/env python3\nimport os,subprocess,sys\n"
                       "command=sys.argv[-1].removeprefix('sudo -n -u workops ')\n"
                       "command=command.replace('/srv/work-ops/run', '\"$WO_VERIFY_RUN\"')\n"
                       "sys.exit(subprocess.run(command,shell=True).returncode)\n")
        ssh.chmod(0o755)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True)

    def verify(self, *args, **env):
        return subprocess.run(["bash", str(self.script), *args], capture_output=True,
                              text=True, timeout=15, env={**os.environ,
                              "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                              "WO_VERIFY_TRACE": str(self.trace),
                              "WO_VERIFY_RUN": str(self.run_dir),
                              "WO_DB": "/must/not/open", "WO_CONTRACT_DB": "/must/not/open",
                              "WO_UI_DB": "/must/not/open", **env})

    def test_list_excludes_private_data_and_does_not_connect(self):
        result = self.verify("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("workspace/example.py", result.stdout)
        self.assertIn("ops/vps/verify.sh", result.stdout)
        for private in ("records/", "evidence/", "provenance/", "data/", ".env", "keys/"):
            self.assertNotIn(private, result.stdout)
        self.assertFalse(self.trace.exists())

    def test_uses_committed_content_and_removes_scratch_on_success(self):
        (self.repo / "workspace/example.py").write_text("# uncommitted\n")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trace.read_text(), "# committed\n")
        self.assertIn("uncommitted changes are excluded", result.stdout)
        self.assertEqual(list(self.run_dir.iterdir()), [])

    def test_failure_propagates_and_removes_scratch(self):
        result = self.verify(WO_VERIFY_EXIT="17")
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertNotIn("Production was not deployed", result.stdout)
        self.assertEqual(list(self.run_dir.iterdir()), [])

    def test_invalid_argument_does_not_connect(self):
        result = self.verify("--deploy")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.trace.exists())


if __name__ == "__main__":
    unittest.main()
