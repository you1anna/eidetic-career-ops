"""Exercise alerts scheduling and credential transport with synthetic values and no SSH."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from credentials import get  # noqa: E402
from scan_alerts import ENV_KEYS  # noqa: E402


class AlertCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wo alerts ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.creds = self.root / "credentials"
        self.creds.mkdir()
        self.trace = self.root / "trace.json"

    def test_explicit_environment_has_precedence_and_directory_is_fallback(self):
        (self.creds / ENV_KEYS[2]).write_text("fixture-file-secret\n")
        environment = {"CREDENTIALS_DIRECTORY": str(self.creds)}
        self.assertEqual(get(ENV_KEYS[2], environment), "fixture-file-secret")
        self.assertEqual(get(ENV_KEYS[2], environment | {ENV_KEYS[2]: "fixture-env-secret"}), "fixture-env-secret")
        self.assertEqual(get(ENV_KEYS[0], environment), "")

    def scan_args(self):
        recorder = self.root / "record.py"
        recorder.write_text("import json,os,sys\nfrom pathlib import Path\n"
                            "Path(os.environ['WO_TEST_TRACE']).write_text(json.dumps(sys.argv[1:]))\n")
        script = (ROOT / "ops/vps/wo-scan.sh").read_text()
        script = script.replace("/srv/work-ops/venv/bin/python", shlex.quote(sys.executable))
        script = script.replace("/srv/work-ops/work-ops/tools/wo.py", shlex.quote(str(recorder)))
        path = self.root / "scan.sh"
        path.write_text(script)
        result = subprocess.run(["bash", str(path)], text=True, capture_output=True, timeout=10,
                                env={**os.environ, "CREDENTIALS_DIRECTORY": str(self.creds), "WO_TEST_TRACE": str(self.trace)})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.trace.read_text())

    def test_daily_run_leaves_alerts_to_the_dedicated_email_timer(self):
        args = self.scan_args()
        self.assertIn("--keyless", args)
        self.assertEqual([args[i + 1] for i, v in enumerate(args) if v == "--keyed"], [])

    def test_existing_feed_groups_still_use_their_complete_credentials(self):
        for key in ("COMPANIES_HOUSE_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "REED_API_KEY", *ENV_KEYS):
            (self.creds / key).write_text("synthetic")
        args = self.scan_args()
        self.assertEqual([args[i + 1] for i, v in enumerate(args) if v == "--keyed"], ["companies-house", "feeds"])
        self.assertNotIn("synthetic", json.dumps(args))

    def test_credentials_are_imported_only_into_scan_service(self):
        scan = (ROOT / "ops/vps/wo-scan.service").read_text()
        web = (ROOT / "ops/vps/work-ops-workspace.service").read_text()
        for name in ENV_KEYS:
            self.assertIn("LoadCredentialEncrypted=" + name, scan)
            self.assertNotIn("LoadCredentialEncrypted=" + name, web)
        self.assertIn("-/run/credentials", web)

    def test_transport_passes_secret_on_stdin_only_and_rejects_unknown_names(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        python = self.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('fixture-secret')\n")
        python.chmod(0o755)
        ssh = bindir / "ssh"
        ssh.write_text(f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
                       "assert sys.stdin.read() == 'fixture-secret'\n"
                       "assert not any('fixture-secret' in arg for arg in sys.argv)\n"
                       "Path(os.environ['WO_TEST_TRACE']).write_text(json.dumps(sys.argv))\n"
                       "print('credential stored')\n")
        ssh.chmod(0o755)
        script = (ROOT / "ops/vps/send-credential.sh").read_text()
        script = script.replace('cd "$(dirname "$0")/../.."', 'cd ' + shlex.quote(str(self.root)))
        path = self.root / "send.sh"
        path.write_text(script)
        environment = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"], "WO_TEST_TRACE": str(self.trace)}
        for name in ENV_KEYS:
            with self.subTest(name=name):
                result = subprocess.run(["bash", str(path), name], capture_output=True, text=True, timeout=10, env=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("fixture-secret", result.stdout + result.stderr)
                self.assertTrue(json.loads(self.trace.read_text())[-1].endswith("put-credential " + name))
        self.trace.unlink()
        result = subprocess.run(["bash", str(path), "INVALID;command"], capture_output=True, text=True, timeout=10, env=environment)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.trace.exists())


if __name__ == "__main__":
    unittest.main()
