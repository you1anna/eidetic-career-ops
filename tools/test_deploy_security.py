"""Exercise deploy's actual Python blocks using scratch Git objects and real flock descriptors."""
import fcntl
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = (ROOT / "ops/vps/deploy.sh").read_text()


def block(marker):
    return DEPLOY.split(f"<<'{marker}'\n", 1)[1].split(f"\n{marker}\n", 1)[0]


class DeployArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deploy-security-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.staging = self.root / "staging"
        self.staging.mkdir(mode=0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        sudo = self.bin / "sudo"
        sudo.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
        sudo.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
        self.git("init", "-q")
        for name, data in {"ops/vps/wo": b"reviewed wrapper", "ops/vps/test.service": b"reviewed unit",
                           "tools/research_codex.py": b"reviewed research", "records/private": b"excluded"}.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Independent installer inputs: a missing mandatory file must refuse before any install.
        for name in ('wo-offsite-fetch', 'put-credential', 'work-ops-workspace.service',
                     'wo-research-codex.socket', 'wo-research-codex@.service',
                     'wo-research-acceptance.service', 'wo-research-acceptance.timer',
                     'wo-daily-review.service', 'wo-daily-review.timer', 'wo-scan.service',
                     'wo-scan.timer', 'wo-backup.service', 'wo-backup.timer', 'wo-email.service',
                     'wo-email.timer', 'wo-research.service', 'wo-research.timer',
                     'wo-caretaker.service', 'wo-caretaker.timer'):
            (self.repo / 'ops/vps' / name).write_text('reviewed input')
        for name in ('research_schemas.py', 'codex_maintenance.py'):
            (self.repo / 'tools' / name).write_text('reviewed input')
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        self.rev = self.git("rev-parse", "HEAD").stdout.strip()
        self.manifest = self.python("MANIFEST_PY", self.repo, self.rev).stdout.strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], text=True, capture_output=True, check=True)

    def python(self, marker, *args):
        return subprocess.run([sys.executable, "-I", "-", *map(str, args)], input=block(marker),
                              env=self.env, text=True, capture_output=True, timeout=10)

    def stage(self, manifest=None):
        return self.python("ARTIFACTS_PY", self.repo, self.rev, self.staging, manifest or self.manifest)

    def test_only_commit_bytes_reach_staging_not_mutable_checkout_or_private_records(self):
        (self.repo / "ops/vps/wo").write_text("malicious working-tree replacement")
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.staging / "ops/vps/wo").read_text(), "reviewed wrapper")
        self.assertEqual((self.staging / "ops/vps/test.service").read_text(), "reviewed unit")
        self.assertFalse((self.staging / "records").exists())

    def test_corrupted_git_output_is_refused_before_it_can_be_installed(self):
        (self.bin / "sudo").write_text('#!/bin/sh\nprintf "untrusted output"\n')
        result = self.stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact hash mismatch", result.stderr)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_oversized_artifact_is_refused_before_staging(self):
        import shlex
        (self.bin / "sudo").write_text('#!/bin/sh\nexec ' + shlex.join([
            sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"x" * (5 * 1024 * 1024))'
        ]) + '\n')
        result = self.stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("oversized artifact", result.stderr)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_manifest_cannot_escape_private_staging(self):
        import base64
        import json
        for name in ("/tmp/escape", "ops/vps/../../../escape", "records/private"):
            manifest = base64.b64encode(json.dumps({name: "bad"}).encode()).decode()
            result = self.stage(manifest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid artifact path", result.stderr)

    def test_missing_mandatory_artifact_refuses_before_staging(self):
        manifest = json.loads(base64.b64decode(self.manifest))
        del manifest['ops/vps/wo-email.service']
        encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
        result = self.stage(encoded)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing required artifacts', result.stderr)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_every_privileged_install_uses_verified_staging(self):
        change = DEPLOY.split("<<'CHANGE'\n", 1)[1].split("\nCHANGE\n", 1)[0]
        for line in change.splitlines():
            if ("install " in line or line.startswith("sed ")) and not line.startswith("as_workops "):
                self.assertNotIn('"$CODE/', line)
        self.assertIn('"$TRUSTED/ops/vps/wo"', change)
        self.assertIn('"$TRUSTED/ops/vps/work-ops-workspace.service"', change)


class DeployLockTests(unittest.TestCase):
    def setUp(self):
        self.namespace = {"__name__": "security_test"}
        exec(compile(block("LOCKS_PY"), "deploy.sh:LOCKS_PY", "exec"), self.namespace)
        self.temp = tempfile.TemporaryDirectory(prefix="deploy-locks-")
        self.addCleanup(self.temp.cleanup)
        # /var can be a symlink on macOS; production paths are Linux. Resolve only our fixture root.
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.lock = self.data / ".writer.lock"
        self.lock.write_text("preserved")
        (self.data / ".research-worker.lock").touch()
        self.directory = self.namespace["directory_fd"](str(self.data))
        self.addCleanup(os.close, self.directory)

    def acquire(self, name=".writer.lock", timeout=0):
        return self.namespace["acquire"](self.directory, name, timeout)

    def test_lock_excludes_second_writer_without_modifying_file_and_releases(self):
        fd = self.acquire()
        try:
            with self.assertRaises(TimeoutError):
                self.acquire()
            self.assertEqual(self.lock.read_text(), "preserved")
        finally:
            os.close(fd)
        os.close(self.acquire())

    def test_symlink_hardlink_and_fifo_fail_closed(self):
        outside = self.root / "outside"
        outside.write_text("unchanged")
        self.lock.unlink()
        for make in (lambda: self.lock.symlink_to(outside), lambda: os.link(outside, self.lock),
                     lambda: os.mkfifo(self.lock)):
            make()
            with self.assertRaises((ValueError, OSError)):
                self.acquire()
            self.lock.unlink()
        self.assertEqual(outside.read_text(), "unchanged")

    def test_parent_symlink_is_refused(self):
        link = self.root / "linked"
        link.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(OSError):
            self.namespace["directory_fd"](str(link))

    def test_child_holds_both_locks_and_failure_releases_them(self):
        child = self.root / "child.py"
        child.write_text('import fcntl,sys\n'
            'for name in (".research-worker.lock", ".writer.lock"):\n'
            ' with open(sys.argv[1]+"/"+name) as f:\n'
            '  try: fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
            '  except BlockingIOError: pass\n'
            '  else: raise SystemExit(99)\n'
            'raise SystemExit(17)\n')
        script = self.root / "change.sh"
        import shlex
        script.write_text(shlex.join([sys.executable, str(child), str(self.data)]) + "\n")
        self.assertEqual(self.namespace["run"](str(self.data), str(script)), 17)
        for name in (".research-worker.lock", ".writer.lock"):
            os.close(self.acquire(name))


class LockedPreflightTests(unittest.TestCase):
    """Execute the actual pre-mutation shell against synthetic code and schema changes."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='locked preflight ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'tracked').write_text('initial')
        self.commit()
        self.rev = self.git('rev-parse', 'HEAD').strip()
        self.db = self.root / 'fixture.db'
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE schema_version(version INTEGER)')
            conn.execute('INSERT INTO schema_version VALUES(20)')
        self.trusted = self.root / 'trusted'
        self.trusted.mkdir(mode=0o700)
        if "<<'SCHEMA_PY'" in DEPLOY:
            (self.trusted / 'schema.py').write_text(block('SCHEMA_PY'))
        function = re.search(r'schema_check\(\) \{.*?\n\}', DEPLOY, re.S)
        self.schema_function = function[0] + '\n' if function else ''
        # The locked preflight also takes its last timing read; here no server answers.
        self.schema_function += re.search(r'^timings_before\(\) \{\n.*?^\}\n', DEPLOY, re.S | re.M)[0]
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        sudo = self.bin / 'sudo'
        sudo.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
        sudo.chmod(0o700)
        curl = self.bin / 'curl'
        curl.write_text('#!/bin/sh\nexit 7\n')  # never a real loopback request from a test
        curl.chmod(0o700)
        self.trace = self.root / 'mutation'

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')

    def check(self, **overrides):
        change = block('CHANGE')
        prefix = change[:change.index('as_workops sh -c ')]
        return subprocess.run(['bash'], input=self.schema_function + prefix + '\nprintf mutation > "$TRACE"\n',
                              capture_output=True, text=True, timeout=5,
                              env={**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                                   'CODE': str(self.repo), 'DB': str(self.db), 'PY': sys.executable,
                                   'CUTOVER': '0', 'MIGRATE': '0', 'WANT': '20', 'SCHEMA': 'same',
                                   'BEFORE_REV': self.rev, 'TRUSTED': str(self.trusted),
                                   'DATA': str(self.root), 'TRACE': str(self.trace), **overrides})

    def test_schema_advance_while_waiting_refuses_before_mutation(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('INSERT INTO schema_version VALUES(21)')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('migration', result.stderr)
        self.assertFalse(self.trace.exists())

    def test_changed_checkout_while_waiting_refuses_before_mutation(self):
        (self.repo / 'tracked').write_text('another deployment')
        self.commit()
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checkout changed', result.stderr)
        self.assertFalse(self.trace.exists())

    def test_dirty_checkout_while_waiting_refuses_before_mutation(self):
        (self.repo / 'tracked').write_text('uncommitted change')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local changes', result.stderr)
        self.assertFalse(self.trace.exists())

    def test_unchanged_checkout_and_schema_allow_the_change(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trace.read_text(), 'mutation')

    def test_changed_pending_migrations_and_an_already_created_cutover_refuse(self):
        result = self.check(MIGRATE='1', WANT='20 21 22', SCHEMA='migrate 21 22')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.trace.unlink()
        with sqlite3.connect(self.db) as conn:
            conn.execute('INSERT INTO schema_version VALUES(21)')
        result = self.check(MIGRATE='1', WANT='20 21 22', SCHEMA='migrate 21 22')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('schema changed', result.stderr)
        self.assertFalse(self.trace.exists())
        result = self.check(CUTOVER='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('hosted database already exists', result.stderr)
        self.assertFalse(self.trace.exists())

    def test_locked_version_check_does_not_repeat_full_integrity_scan(self):
        with patch('sqlite3.connect') as connect:
            conn = connect.return_value
            conn.execute.return_value = [(20,)]
            output = io.StringIO()
            with patch.object(sys, 'argv', ['schema.py', str(self.db), '0', 'versions', '20']), \
                    contextlib.redirect_stdout(output):
                exec(compile(block('SCHEMA_PY'), 'schema.py', 'exec'), {})
            self.assertEqual(output.getvalue(), 'same\n')
            conn.execute.assert_called_once_with('select version from schema_version')
            conn.close.assert_called_once()


class ResearchCredentialBoundaryTests(unittest.TestCase):
    def test_scheduled_service_hides_github_credentials_and_keeps_codex_login(self):
        unit = (ROOT / "ops/vps/wo-research-codex@.service").read_text()
        hidden = " ".join(line.partition("=")[2] for line in unit.splitlines()
                          if line.startswith("InaccessiblePaths="))
        self.assertIn("-/home/codex/.config/gh", hidden.split())
        self.assertNotIn("/home/codex/.codex", hidden)
        self.assertIn("NoNewPrivileges=yes", unit)


if __name__ == "__main__":
    unittest.main()


class ResponsivenessCaptureTests(unittest.TestCase):
    """The Dashboard's timings live in the server's memory and a restart clears them: every release
    log keeps the figures from before it, and a dry run reads them, without blocking either."""

    DASHBOARD = {
        'capacity': {'note': 'PRIVATE capacity text'}, 'hunt': {'people': ['PRIVATE Person']},
        'responsiveness': {'since': '2026-11-10T03:55:01+00:00', 'per_kind': 50, 'kinds': {
            'save': {'count': 3, 'median_ms': 12.5, 'p90_ms': 20.1, 'max_ms': 31, 'note': 'PRIVATE note'},
            'build': {'count': 4, 'median_ms': 1800.2, 'p90_ms': 2100, 'max_ms': 2500.9,
                      'phase_median_ms': {'relevance': 400.5, 'records': 610, 'label': 'PRIVATE phase'}},
            'refresh_other': {'count': 0},
            'PRIVATE kind name': {'count': 9, 'median_ms': 1}}}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deploy-timings-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "timings.py"
        self.script.write_text(block("TIMINGS_PY") + "\n")

    def filtered(self, text):
        return subprocess.run([sys.executable, "-I", str(self.script)], input=text, capture_output=True, text=True)

    def test_only_counts_and_durations_of_known_kinds_leave_the_server(self):
        done = self.filtered(json.dumps(self.DASHBOARD))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("PRIVATE", done.stdout)
        self.assertEqual(json.loads(done.stdout), {'since': '2026-11-10T03:55:01+00:00', 'kinds': {
            'save': {'count': 3, 'median_ms': 12.5, 'p90_ms': 20.1, 'max_ms': 31},
            'build': {'count': 4, 'median_ms': 1800.2, 'p90_ms': 2100, 'max_ms': 2500.9,
                      'phase_median_ms': {'relevance': 400.5, 'records': 610}}}})
        self.assertEqual(done.stdout.count("\n"), 1)

    def test_a_missing_or_malformed_reply_prints_nothing(self):
        for text in ("", "not json", "[]", json.dumps({'capacity': {}}), json.dumps({'responsiveness': {'kinds': []}})):
            with self.subTest(text=text[:20]):
                done = self.filtered(text)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(done.stdout, "")

    def shell(self, curl_body, curl_status):
        function = re.search(r"^timings_before\(\) \{\n.*?^\}\n", DEPLOY, re.S | re.M)[0]
        self.assertIn("/usr/bin/python3 -I", function)
        self.assertIn("http://127.0.0.1:8769/api/dashboard", function)
        function = function.replace("/usr/bin/python3", sys.executable)  # the server's interpreter path
        tools = self.root / "bin"; tools.mkdir(exist_ok=True)
        (self.root / "reply").write_text(curl_body)
        (tools / "curl").write_text(f"#!/bin/sh\ncat '{self.root / 'reply'}'\nexit {curl_status}\n")
        (tools / "curl").chmod(0o755)
        return subprocess.run(["bash", "-c", "set -euo pipefail\n" + function + "timings_before\necho continued\n"],
                              env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "TRUSTED": str(self.root)},
                              capture_output=True, text=True, timeout=20)

    def test_the_release_continues_whether_or_not_the_figures_can_be_read(self):
        ok = self.shell(json.dumps(self.DASHBOARD), 0)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        first, second = ok.stdout.splitlines()
        self.assertTrue(first.startswith('responsiveness since the last restart: {"kinds":'), first)
        self.assertNotIn("PRIVATE", ok.stdout)
        self.assertEqual(second, "continued")
        for body, status in (("", 7), ("<html>sign in</html>", 0), (json.dumps(self.DASHBOARD), 22)):
            with self.subTest(status=status, body=body[:12]):
                failed = self.shell(body, status)
                self.assertEqual(failed.returncode, 0, failed.stderr)
                self.assertEqual(failed.stdout.splitlines(),
                                 ["responsiveness since the last restart: unavailable", "continued"])

    def test_figures_are_read_in_the_dry_run_and_before_the_site_is_stopped_or_restarted(self):
        dry = DEPLOY.split('if [[ "$DRY_RUN" == 1 ]]; then\n  echo "dry run:', 1)[1].split("\n  exit 0\n", 1)[0]
        self.assertIn("\n  timings_before\n", "\n" + dry + "\n")
        change = block("CHANGE")
        call = change.index("\ntimings_before\n")
        for command in ("systemctl stop work-ops-workspace.service", "systemctl restart work-ops-workspace.service",
                        "checkout -q --detach", "trap 'echo \"deploy FAILED"):
            self.assertLess(call, change.index(command), command)
        self.assertRegex(DEPLOY, r"export -f [^\n]*\btimings_before\b")
