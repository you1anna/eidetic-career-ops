"""Check infrastructure fails early and preserves useful evidence, without real services."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch

import check_runner


class CheckRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_denied_loopback_has_one_actionable_failure(self):
        output = io.StringIO()
        with patch('check_runner.socket.socket', side_effect=PermissionError('Operation not permitted')), contextlib.redirect_stdout(output):
            self.assertEqual(check_runner.preflight(), 1)
        self.assertIn('local-server permissions', output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())

    def test_preflight_closes_the_temporary_listener(self):
        with patch('check_runner.socket.socket') as factory, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_runner.preflight(), 0)
        listener = factory.return_value.__enter__.return_value
        listener.bind.assert_called_once_with(('127.0.0.1', 0))
        listener.listen.assert_called_once()
        factory.return_value.__exit__.assert_called_once()

    def run_check(self, code, interval=30):
        log = self.root / 'suite.log'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = check_runner.run('example', log, [sys.executable, '-u', '-c', code], interval=interval)
        return result, output.getvalue(), log

    def test_success_retains_full_private_log_and_reports_duration(self):
        code, output, log = self.run_check('print("details")')
        self.assertEqual(code, 0)
        self.assertIn('RUN   example', output)
        self.assertIn('ok    example', output)
        self.assertIn('s)', output)
        self.assertEqual(log.read_text(), 'details\n')
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_failure_preserves_exit_code_full_log_and_only_prints_tail(self):
        code, output, log = self.run_check('import sys; print("first-line"); print("last-line\\n" * 50); sys.exit(7)')
        self.assertEqual(code, 7)
        self.assertIn('FAIL  example', output)
        self.assertIn(str(log), output)
        self.assertNotIn('first-line', output)
        self.assertIn('first-line', log.read_text())

    def test_slow_command_reports_progress(self):
        code, output, _ = self.run_check('import time; time.sleep(.15)', interval=.05)
        self.assertEqual(code, 0)
        self.assertIn('WAIT  example', output)

    def test_missing_command_is_a_readable_failure(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(check_runner.run('missing', self.root / 'missing.log', ['/nonexistent/work-ops-command']), 1)
        self.assertIn('FAIL  missing', output.getvalue())

    def test_shell_stops_before_suites_when_preflight_fails(self):
        ops = self.root / 'ops'
        ops.mkdir()
        shutil.copy(Path(__file__).resolve().parents[1] / 'check.sh', ops / 'check.sh')
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\ncase "$*" in\n*preflight*) echo "probe denied"; exit 1;;\n*) echo "suite ran"; exit 0;;\nesac\n')
        python.chmod(0o700)
        result = subprocess.run(['bash', str(ops / 'check.sh'), 'unit'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('probe denied', result.stdout)
        self.assertNotIn('suite ran', result.stdout)

    def shell_fixture(self, test_exit=0):
        ops = self.root / 'ops'
        (ops / 'local').mkdir(parents=True)
        source = Path(__file__).resolve().parents[1]
        shutil.copy(source / 'check.sh', ops / 'check.sh')
        shutil.copy(source / 'local/check_runner.py', ops / 'local/check_runner.py')
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\n'
                          'case "$*" in\n'
                          '*preflight*) echo "probe ok"; exit 0;;\n'
                          f'*check_runner.py*) exec {shlex.quote(sys.executable)} "$@";;\n'
                          f'*"-m pytest"*) echo "test output"; exit {test_exit};;\n'
                          '*) exit 0;;\nesac\n')
        python.chmod(0o700)
        (self.root / 'data').mkdir()
        (self.root / 'data/work-ops.db').write_text('synthetic placeholder, never opened')
        return ops / 'check.sh'

    def test_shell_preserves_failure_and_runs_remaining_checks(self):
        script = self.shell_fixture(test_exit=7)
        result = subprocess.run(['bash', str(script), '--strict', 'unit'], capture_output=True, text=True,
                                env={**os.environ, 'TMPDIR': str(self.root)})
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL  Python tests', result.stdout)
        self.assertIn('ok    views parity', result.stdout)
        logs = list(self.root.glob('work-ops-check.*/*.log'))
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0].parent.stat().st_mode & 0o777, 0o700)

    def test_preflight_only_cannot_be_mistaken_for_a_strict_run(self):
        script = self.shell_fixture()
        for args, code in [(['--preflight'], 0), (['--preflight', '--strict'], 2), (['--preflight', 'unit'], 2)]:
            result = subprocess.run(['bash', str(script), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, code)
            self.assertNotIn('RUN   ', result.stdout)

    def test_missing_worker_dependencies_fail_strict_mode(self):
        script = self.shell_fixture()
        for args, code in [(['worker'], 0), (['--strict', 'worker'], 1)]:
            result = subprocess.run(['bash', str(script), *args], capture_output=True, text=True,
                                    env={**os.environ, 'TMPDIR': str(self.root)})
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
            self.assertIn('SKIP  email Worker', result.stdout)
            self.assertIn('npm ci --prefix ops/email-intake', result.stdout)

    def test_browser_skip_still_fails_strict_mode(self):
        script = self.shell_fixture()
        binaries = self.root / 'bin'
        binaries.mkdir()
        for name in ('dirname', 'mktemp'):
            (binaries / name).symlink_to(shutil.which(name))
        for args, code in [(['ui'], 0), (['--strict', 'ui'], 1)]:
            result = subprocess.run(['/bin/bash', str(script), *args], capture_output=True, text=True,
                                    env={**os.environ, 'PATH': str(binaries), 'TMPDIR': str(self.root)})
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
            self.assertIn('SKIP  browser smoke', result.stdout)

    def test_termination_stops_the_running_child(self):
        pid_file = self.root / 'pid'
        command = [sys.executable, str(Path(check_runner.__file__)), 'run', 'interrupt', str(self.root / 'interrupt.log'),
                   sys.executable, '-c', f'import os,time; open({str(pid_file)!r},"w").write(str(os.getpid())); time.sleep(30)']
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            # Creation and writing are separate operations; an empty file is not child readiness.
            while (not pid_file.exists() or not pid_file.read_text().strip()) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(pid_file.exists())
            child = int(pid_file.read_text())
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 143, error)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=8)

    def test_cleanup_retries_transient_probe_permission_error_until_group_is_absent(self):
        child = Mock(pid=123)
        with patch('check_runner.os.killpg', side_effect=[None, PermissionError('exiting group'), ProcessLookupError()]) as killpg, \
             patch('check_runner.time.monotonic', side_effect=[10, 10.02]), \
             patch('check_runner.time.sleep') as sleep:
            check_runner.stop_group(child)
        self.assertEqual(killpg.call_args_list, [call(123, signal.SIGTERM), call(123, 0), call(123, 0)])
        self.assertEqual(child.poll.call_count, 2, 'each probe must first reap an exited leader')
        child.wait.assert_called_once_with()
        sleep.assert_called_once_with(.02)

    def test_cleanup_preserves_probe_permission_failure_at_existing_deadline(self):
        child = Mock(pid=123)
        expired = PermissionError('still denied at deadline')
        with patch('check_runner.os.killpg', side_effect=[None, PermissionError('initial denial'), expired]) as killpg, \
             patch('check_runner.time.monotonic', side_effect=[10, 14.99, 15]), \
             patch('check_runner.time.sleep') as sleep:
            with self.assertRaises(PermissionError) as raised:
                check_runner.stop_group(child)
        self.assertIs(raised.exception, expired, 'retry only within the existing five-second deadline')
        self.assertEqual(killpg.call_args_list, [call(123, signal.SIGTERM), call(123, 0), call(123, 0)])
        self.assertEqual(child.poll.call_count, 2)
        child.wait.assert_not_called()
        sleep.assert_called_once_with(.02)

    def test_cleanup_never_retries_or_swallows_signal_permission_failures(self):
        for denied, responses, times, expected in [
            (signal.SIGTERM, [], [], [signal.SIGTERM]),
            (signal.SIGKILL, [None, None], [10, 15], [signal.SIGTERM, 0, signal.SIGKILL]),
        ]:
            with self.subTest(signal=denied):
                child = Mock(pid=123)
                error = PermissionError('signal delivery denied')
                with patch('check_runner.os.killpg', side_effect=[*responses, error]) as killpg, \
                     patch('check_runner.time.monotonic', side_effect=times), \
                     patch('check_runner.time.sleep') as sleep:
                    with self.assertRaises(PermissionError) as raised:
                        check_runner.stop_group(child)
                self.assertIs(raised.exception, error)
                self.assertEqual(killpg.call_args_list, [call(123, value) for value in expected])
                child.wait.assert_not_called()
                sleep.assert_not_called()

    def test_parallel_stages_overlap_with_a_fixed_limit_and_keep_private_logs(self):
        stages = []
        for index in range(4):
            code = f'''import fcntl,json,time
from pathlib import Path
root=Path({str(self.root)!r})
with (root/'counter').open('a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 p=root/'counts.json'; counts=json.loads(p.read_text()) if p.exists() else [0,0]
 counts[0]+=1; counts[1]=max(counts); p.write_text(json.dumps(counts))
 (root/'started-{index}').touch()
if {index} < 2:
 deadline=time.monotonic()+3
 while not (root/'started-{1-index}').exists():
  if time.monotonic()>deadline: raise SystemExit('stages did not overlap')
  time.sleep(.01)
with (root/'counter').open('a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 p=root/'counts.json'; counts=json.loads(p.read_text()); counts[0]-=1; p.write_text(json.dumps(counts))
print('stage {index}')
'''
            stages.append({'name': f'stage {index}', 'log': str(self.root / f'{index}.log'),
                           'command': [sys.executable, '-c', code]})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_runner.batch(stages, jobs=2), 0)
        self.assertEqual(json.loads((self.root / 'counts.json').read_text()), [0, 2])
        for index in range(4):
            log = self.root / f'{index}.log'
            self.assertEqual(log.read_text(), f'stage {index}\n')
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_parallel_failure_runs_remaining_stages_and_fails_the_batch(self):
        stages = [{'name': str(n), 'log': str(self.root / f'{n}.log'),
                   'command': [sys.executable, '-c', f'print({n}); raise SystemExit({7 if n == 0 else 0})']}
                  for n in range(3)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_runner.batch(stages, jobs=2), 1)
        self.assertEqual((self.root / '2.log').read_text(), '2\n')

    def test_parallel_termination_stops_active_children_and_never_starts_queued_work(self):
        stages = [{'name': str(n), 'log': str(self.root / f'{n}.log'),
                   'command': [sys.executable, '-c',
                    f'import os,time; open({str(self.root / f"pid-{n}")!r},"w").write(str(os.getpid())); time.sleep(30)']}
                  for n in range(3)]
        manifest = self.root / 'stages.json'
        manifest.write_text(json.dumps(stages))
        process = subprocess.Popen([sys.executable, str(Path(check_runner.__file__)), 'batch', '2', str(manifest)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if all((self.root / f'pid-{n}').exists() and (self.root / f'pid-{n}').read_text() for n in range(2)):
                    break
                time.sleep(.01)
            children = [int((self.root / f'pid-{n}').read_text()) for n in range(2)]
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 143, error)
            self.assertFalse((self.root / 'pid-2').exists())
            for child in children:
                with self.assertRaises(ProcessLookupError):
                    os.kill(child, 0)
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=8)

    def test_cancellation_also_stops_a_descendant_that_ignores_sigterm(self):
        descendant = self.root / 'descendant.pid'
        code = ('import os,signal,time; from pathlib import Path; '
                'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                f'Path({str(descendant)!r}).write_text(str(os.getpid())); time.sleep(30)')
        parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{code!r}]); time.sleep(30)'
        process = subprocess.Popen([sys.executable, str(Path(check_runner.__file__)), 'run', 'descendant',
                                    str(self.root / 'descendant.log'), sys.executable, '-c', parent],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        pid = None
        try:
            deadline = time.monotonic() + 5
            while (not descendant.exists() or not descendant.read_text()) and time.monotonic() < deadline:
                time.sleep(.01)
            pid = int(descendant.read_text())
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 143, error)
            # An orphan can briefly be a zombie until init reaps it; it must no longer run.
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith('Z'), f'descendant survived: {state}')
        finally:
            if process.poll() is None:
                process.terminate()
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=8)

    def test_dependency_check_rejects_stale_or_missing_installed_versions(self):
        project = self.root / 'worker'
        package = project / 'node_modules/example'
        package.mkdir(parents=True)
        (project / 'package-lock.json').write_text(json.dumps({'packages': {
            '': {'name': 'fixture'}, 'node_modules/example': {'version': '2.0.0'},
            'node_modules/platform-optional': {'version': '1.0.0', 'optional': True}}}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_runner.node_dependencies(project), 1)
            (package / 'package.json').write_text('{"version":"1.0.0"}')
            self.assertEqual(check_runner.node_dependencies(project), 1)
            (package / 'package.json').write_text('{"version":"2.0.0"}')
            self.assertEqual(check_runner.node_dependencies(project), 0)

    def test_browser_capacity_defaults_are_conservative(self):
        cases = [('vps', 8, 8, 1), ('mac', 2, 8, 1), ('mac', 4, 8, 1), ('mac', 8, 1, 1), ('mac', 8, 8, 2)]
        for profile, gib, cores, expected in cases:
            with self.subTest(profile=profile, gib=gib, cores=cores), \
                 patch('check_runner.os.sysconf', side_effect=[gib * 1024**3 // 4096, 4096]), \
                 patch('check_runner.os.cpu_count', return_value=cores):
                self.assertEqual(check_runner.browser_jobs(profile), expected)
        with patch('check_runner.sys.platform', 'linux'), patch('check_runner.os.sysconf', side_effect=ValueError):
            self.assertEqual(check_runner.browser_jobs('mac'), 1)


if __name__ == '__main__':
    unittest.main()
