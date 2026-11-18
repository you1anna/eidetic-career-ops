"""Run deployment's local gate on scratch Git commits, never SSH or real suites."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DeployRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='deploy revision ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.trace = self.root / 'check.json'
        self.ssh_trace = self.root / 'ssh'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        ssh = self.bin / 'ssh'
        ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$DEPLOY_TEST_SSH"\nexit 99\n')
        ssh.chmod(0o700)
        files = {
            '.gitignore': '.env\n.venv\nnode_modules\ndata/\n',
            '.python-version': '3.12\n', 'requirements.txt': '', 'requirements-dev.txt': '',
            'tests/ui/package.json': '{}', 'tests/ui/package-lock.json': '{}',
            'ops/email-intake/package.json': '{}', 'ops/email-intake/package-lock.json': '{}',
            'workspace/marker': 'GOOD', 'docs/required.txt': 'committed documentation',
            'conftest.py': '# network guard placeholder\n', 'pytest.ini': '[pytest]\n',
            'ops/vps/wo-backup.sh': '# decision #28\n',
        }
        for name, value in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        (self.repo / 'ops/check.sh').write_text(f'#!{sys.executable}\n' + '''import json, os, pathlib, stat, subprocess, sys
root = pathlib.Path(__file__).resolve().parents[1]
if os.environ.get('DEPLOY_TEST_EDIT_SCRIPT'):
    script = pathlib.Path(os.environ['DEPLOY_TEST_REPO']) / 'ops/vps/deploy.sh'
    script.write_text('\\n' * 1000 + 'exit 87\\n' + script.read_text())
if os.environ.get('DEPLOY_TEST_EDIT_PINS'):
    (pathlib.Path(os.environ['DEPLOY_TEST_REPO']) / 'requirements.txt').write_text('changed during checks')
if os.environ.get('DEPLOY_TEST_EDIT'):
    source = pathlib.Path(os.environ['DEPLOY_TEST_REPO'])
    (source / 'workspace/marker').write_text('RACED')
    if os.environ.get('DEPLOY_TEST_COMMIT'):
        subprocess.run(['git', '-C', str(source), 'add', 'workspace/marker'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'concurrent edit'], check=True)
marker = (root / 'workspace/marker').read_text()
result = dict(marker=marker, root=str(root), args=sys.argv[1:], private=stat.S_IMODE(root.parent.stat().st_mode),
    has_data=(root / 'data/work-ops.db').exists(), has_env=(root / '.env').exists(),
    overrides=[k for k in ('WO_DB', 'WO_CONTRACT_DB', 'WO_UI_DB', 'PYTHONPATH', 'PYTHONHOME') if k in os.environ],
    ignored=subprocess.run(['git', '-C', str(root), 'check-ignore', '-q', '.env']).returncode == 0)
pathlib.Path(os.environ['DEPLOY_TEST_TRACE']).write_text(json.dumps(result))
raise SystemExit(0 if marker == 'GOOD' else 7)
''')
        (self.repo / 'ops/check.sh').chmod(0o755)
        shutil.copy(ROOT / 'ops/vps/deploy.sh', self.repo / 'ops/vps/deploy.sh')
        (self.repo / 'ops/local').mkdir()
        helper = ROOT / 'ops/local/deploy_checks.py'
        if helper.exists():
            shutil.copy(helper, self.repo / 'ops/local/deploy_checks.py')
        (self.repo / '.venv').symlink_to(sys.prefix, target_is_directory=True)
        for relative in ('tests/ui/node_modules', 'ops/email-intake/node_modules'):
            (self.repo / relative).mkdir()
        self.git('init', '-q', '--initial-branch=main')
        self.commit('fixture')
        self.git('remote', 'add', 'origin', str(self.repo))
        self.rev = self.git('rev-parse', 'HEAD').strip()
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'DEPLOY_TEST_TRACE': str(self.trace), 'DEPLOY_TEST_SSH': str(self.ssh_trace),
                    'DEPLOY_TEST_REPO': str(self.repo)}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def commit(self, message):
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', message)

    def deploy(self, *args, **env):
        return subprocess.run(['bash', str(self.repo / 'ops/vps/deploy.sh'), '--local-approved', *args],
                              env={**self.env, **env}, capture_output=True, text=True, timeout=20)

    def test_rollback_checks_selected_commit_and_stops_before_ssh_when_it_fails(self):
        (self.repo / 'workspace/marker').write_text('BAD')
        self.commit('bad rollback')
        bad = self.git('rev-parse', 'HEAD').strip()
        (self.repo / 'workspace/marker').write_text('GOOD')
        self.commit('fixed current checkout')
        result = self.deploy('--rev', bad)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.trace.read_text())['marker'], 'BAD')
        self.assertFalse(self.ssh_trace.exists(), result.stdout + result.stderr)

    def test_current_checkout_edits_and_new_commits_cannot_change_the_tested_revision(self):
        for commit in ('', '1'):
            with self.subTest(commit=bool(commit)):
                self.ssh_trace.unlink(missing_ok=True)
                result = self.deploy('--rev', self.rev, DEPLOY_TEST_EDIT='1', DEPLOY_TEST_COMMIT=commit)
                observed = json.loads(self.trace.read_text())
                self.assertEqual(observed['marker'], 'GOOD', result.stdout + result.stderr)
                self.assertTrue(self.ssh_trace.exists())  # the only SSH is our offline refusal stub
                self.assertEqual(result.returncode, 99, result.stdout + result.stderr)
                self.assertIn(self.rev, self.ssh_trace.read_text().splitlines())
                self.assertIn(self.rev, result.stdout)
                self.assertFalse(Path(observed['root']).exists(), 'snapshot must be removed')

    def test_an_edit_to_the_deploy_script_cannot_change_the_running_release(self):
        result = self.deploy('--rev', self.rev, DEPLOY_TEST_EDIT_SCRIPT='1')
        self.assertEqual(result.returncode, 99, result.stdout + result.stderr)
        self.assertIn(self.rev, self.ssh_trace.read_text().splitlines())

    def test_snapshot_contains_committed_inputs_only_and_uses_private_scratch(self):
        (self.repo / 'data').mkdir()
        (self.repo / 'data/work-ops.db').write_text('synthetic local data must not be linked')
        (self.repo / '.env').write_text('synthetic private file')
        result = self.deploy('--rev', self.rev, WO_DB='/must/not/read', WO_CONTRACT_DB='/must/not/read',
                             WO_UI_DB='/must/not/read', PYTHONPATH='/must/not/import')
        observed = json.loads(self.trace.read_text())
        self.assertEqual(observed['args'], ['--strict'])
        self.assertEqual(observed['private'], 0o700)
        self.assertFalse(observed['has_data'])
        self.assertFalse(observed['has_env'])
        self.assertEqual(observed['overrides'], [])
        self.assertTrue(observed['ignored'])
        self.assertFalse(Path(observed['root']).exists(), result.stdout + result.stderr)

    def test_mismatched_dependency_pins_refuse_before_checks_or_ssh(self):
        for name in ('requirements.txt', 'requirements-dev.txt', '.python-version',
                     'tests/ui/package-lock.json', 'ops/email-intake/package-lock.json'):
            path = self.repo / name
            original = path.read_text()
            with self.subTest(name=name):
                path.write_text(original + '\nchanged')
                result = self.deploy('--rev', self.rev)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('dependency inputs differ', result.stdout + result.stderr)
                self.assertFalse(self.trace.exists())
                self.assertFalse(self.ssh_trace.exists())
            path.write_text(original)

    def test_dependency_input_changes_during_checks_refuse_before_ssh(self):
        result = self.deploy('--rev', self.rev, DEPLOY_TEST_EDIT_PINS='1')
        self.assertEqual(json.loads(self.trace.read_text())['marker'], 'GOOD')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('dependency inputs differ', result.stdout + result.stderr)
        self.assertFalse(self.ssh_trace.exists())


if __name__ == '__main__':
    unittest.main()
