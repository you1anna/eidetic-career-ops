"""Run the hosted check script offline: the Mac's off-host section must never be silently absent."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class OffHostSectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wo vps check test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # The script finds data/offsite relative to itself, so run a copy in a scratch checkout.
        (self.root / "ops/vps").mkdir(parents=True)
        self.script = self.root / "ops/vps/check.sh"
        self.script.write_text((ROOT / "ops/vps/check.sh").read_text())
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        # No network: the server and public checks fail or stay pending; only the Mac section matters.
        for name, status in (("ssh", 255), ("curl", 7), ("nc", 1), ("dig", 1)):
            stub = bin_dir / name
            stub.write_text(f"#!/bin/bash\nexit {status}\n")
            stub.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:/usr/bin:/bin", SSH_KEY=str(self.root / "no-key"))

    def run_check(self):
        return subprocess.run(["/bin/bash", str(self.script)], env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=60).stdout

    def offhost_lines(self, out):
        return [line for line in out.splitlines() if "off-host" in line or "restore drill" in line]

    def test_mac_without_offsite_copy_reports_pending(self):
        lines = self.offhost_lines(self.run_check())
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("PENDING", lines[0])
        self.assertIn("data/offsite", lines[0])

    def test_mac_with_passing_drill_reports_it_and_no_pending_note(self):
        offsite = self.root / "data/offsite"
        offsite.mkdir(parents=True)
        (offsite / "work-ops-20261103.db.age").write_bytes(b"synthetic")
        (offsite / "last-run.json").write_text(json.dumps({"result": "passed", "at": "2026-11-03T07:30:00Z"}))
        lines = self.offhost_lines(self.run_check())
        self.assertTrue(any("PASS" in line and "restore drill passed" in line for line in lines), lines)
        self.assertFalse(any("PENDING" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()


class PublicAccessRouteTests(unittest.TestCase):
    def test_every_data_read_the_server_serves_is_checked_behind_access(self):
        import re
        source = (ROOT / 'workspace/server.py').read_text()
        reads = source[source.index('    def do_GET(self):'):source.index('    def do_PATCH(self):')]
        served = set(re.findall(r"self\.path=='(/api/[a-z/]+)'", reads)) - {'/api/health'}
        loop = re.search(r'for path in ([^;]+); do', (ROOT / 'ops/vps/check.sh').read_text())[1].split()
        self.assertIn('/api/dashboard', served)
        self.assertLessEqual(served, set(loop), 'a data route is not checked for Access on the public host')


class ReleaseEvidenceTests(unittest.TestCase):
    REV = 'a' * 40

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release check ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'ops/vps').mkdir(parents=True)
        self.script = self.root / 'ops/vps/check.sh'
        self.script.write_text((ROOT / 'ops/vps/check.sh').read_text())
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.remote = self.root / 'remote'
        self.remote.write_text('\n'.join([
            'unit:work-ops-workspace.service active', 'unit:wo-scan.timer active',
            'unit:wo-backup.timer active', 'unit:cloudflared.service active', 'db ok',
            'backupage 1', 'scanage 1', 'fetchage 1', 'codedirty 0',
            f'coderev {self.REV}', f'healthrev {self.REV}', 'backupresult Finished',
            'backupat 2026-11-05T02:15:00Z', 'listeners 0.0.0.0:22',
            'rootlogin no', 'ufw OpenSSH@tailscale0', 'local 200', 'oomkills 0',
            'reboot none', 'python 3.12', 'chkey present', 'feedkeys 111',
            'plainenv absent', 'webcreds 1', 'legacy', 'journalcap present', '']))
        stubs = {
            'ssh': 'cat "$CHECK_TEST_REMOTE"',
            'dig': '[ "${CHECK_TEST_DNS:-1}" = 1 ] && echo 203.0.113.1',
            'curl': 'case "$*" in *http://*) exit 7;; esac\n'
                    'printf "%s %s\\n" "${CHECK_TEST_HTTP:-302}" https://example.cloudflareaccess.com/login',
            'nc': 'exit 1',
        }
        for name, body in stubs.items():
            path = self.bin / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o700)

    def check(self, expected=True, **env):
        return subprocess.run(['bash', str(self.script), *(['--expect-rev', self.REV] if expected else [])],
                              env={**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                                   'CHECK_TEST_REMOTE': str(self.remote), **env},
                              capture_output=True, text=True, timeout=10)

    def test_a_different_healthy_release_is_not_evidence_for_the_requested_revision(self):
        self.remote.write_text(self.remote.read_text().replace(self.REV, 'b' * 40))
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('expected release', result.stdout)

    def test_missing_health_revision_is_a_release_failure(self):
        self.remote.write_text(self.remote.read_text().replace('healthrev ' + self.REV, 'healthrev'))
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('FAIL', result.stdout)

    def test_unavailable_public_dns_and_tunnel_fail_release_verification(self):
        for env in ({'CHECK_TEST_DNS': '0'}, {'CHECK_TEST_HTTP': '502'}, {'CHECK_TEST_HTTP': '530'}):
            with self.subTest(env=env):
                result = self.check(**env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_ordinary_diagnostics_keep_setup_pending_and_release_keeps_unrelated_pending(self):
        ordinary = self.check(expected=False, CHECK_TEST_DNS='0')
        self.assertEqual(ordinary.returncode, 0, ordinary.stdout + ordinary.stderr)
        self.assertIn('no DNS record yet', ordinary.stdout)
        release = self.check()
        self.assertEqual(release.returncode, 0, release.stdout + release.stderr)
        self.assertIn('off-host copy and restore drill not checked', release.stdout)

    def test_release_requires_a_database_and_running_tunnel(self):
        original = self.remote.read_text()
        for old, new in (('db ok', 'db absent'), ('unit:cloudflared.service active', 'unit:cloudflared.service inactive')):
            with self.subTest(missing=new):
                self.remote.write_text(original.replace(old, new))
                result = self.check()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_invalid_release_arguments_refuse_before_any_network_check(self):
        for args in (['--expect-rev'], ['--expect-rev', 'abc'], ['--unknown']):
            with self.subTest(args=args):
                result = subprocess.run(['bash', str(self.script), *args], capture_output=True, text=True,
                                        env={**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                                             'CHECK_TEST_REMOTE': str(self.remote)}, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertIn('Usage:', result.stderr)
                self.assertEqual(result.stdout, '')
