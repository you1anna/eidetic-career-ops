import os
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import deploy_session

HERE = Path(__file__).resolve().parent


class DeploySessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name) / 'logs'
        self.key = Path(self.tmp.name) / 'key'
        self.key.write_text('not a real key')

    def run_with(self, command, wait=10, reachable=True, key=None):
        return deploy_session.run(command=command, key=str(key or self.key), logs=self.logs, wait=wait,
                                  reachable=lambda key: reachable)

    def test_no_key_or_no_route_refuses_before_anything_runs(self):
        marker = Path(self.tmp.name) / 'ran'
        for kwargs in (dict(key=Path(self.tmp.name) / 'missing'), dict(reachable=False)):
            code, text = self.run_with(['touch', str(marker)], **kwargs)
            self.assertEqual(code, 2)
            self.assertIn('deploy refused', text)
        self.assertFalse(marker.exists())

    def test_a_finished_deploy_prints_its_log_and_exit_status(self):
        code, text = self.run_with(['sh', '-c', 'echo deployed abc1234; exit 3'])
        self.assertEqual(code, 3)
        self.assertIn('deployed abc1234', text)
        self.assertIn('deploy exit status: 3', text)
        self.assertFalse((self.logs / 'deploy.pid').exists())

    def test_a_long_deploy_keeps_running_in_its_own_session_and_blocks_a_second(self):
        code, text = self.run_with(['sh', '-c', 'echo started; sleep 3; echo done'], wait=1)
        self.assertEqual(code, 0)
        self.assertIn('still running', text)
        pid = int((self.logs / 'deploy.pid').read_text().split()[0])
        self.assertNotEqual(os.getpgid(pid), os.getpgid(0))  # a session timeout cannot stop it with ours
        again, refused = self.run_with(['true'])
        self.assertEqual(again, 2)
        self.assertIn('already running', refused)
        log = next(self.logs.glob('deploy-*.log'))
        for _ in range(100):
            if 'deploy exit status: 0' in log.read_text():
                break
            time.sleep(0.1)
        self.assertIn('done\ndeploy exit status: 0', log.read_text())

    def test_the_command_takes_no_arguments(self):
        out = subprocess.run([sys.executable, str(HERE / 'deploy_session.py'), '--migrate'], capture_output=True, text=True)
        self.assertIn('takes no arguments', out.stdout)
        self.assertEqual(out.returncode, 0)  # the text, not the exit code, reaches Claude

    def test_simultaneous_launches_start_only_one_detached_command(self):
        ready = threading.Barrier(2)
        spawn = subprocess.Popen

        def slow_spawn(*args, **kwargs):
            time.sleep(.15)  # both old PID-file checkers pass before either writes the PID
            return spawn(*args, **kwargs)

        def launch():
            return deploy_session.run(command=[sys.executable, '-c', 'import time; time.sleep(.25)'],
                                      key=str(self.key), logs=self.logs, wait=2,
                                      reachable=lambda key: (ready.wait(timeout=3), True)[1])

        with patch.object(deploy_session.subprocess, 'Popen', side_effect=slow_spawn) as popen:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: launch(), range(2)))
        self.assertEqual(popen.call_count, 1, results)
        self.assertEqual(sorted(code for code, _ in results), [0, 2])
        self.assertIn('already running', next(text for code, text in results if code == 2))

    def test_successive_launches_have_unique_private_logs_and_release_the_lock(self):
        with patch.object(deploy_session.time, 'strftime', return_value='deploy-same-second.log'):
            for _ in range(2):
                code, text = self.run_with(['true'])
                self.assertEqual(code, 0, text)
        logs = list(self.logs.glob('deploy-*.log'))
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.logs / 'deploy.pid').exists())

    def test_log_is_private_even_with_a_permissive_umask(self):
        previous = os.umask(0)
        try:
            self.run_with(['true'])
        finally:
            os.umask(previous)
        self.assertEqual(next(self.logs.glob('deploy-*.log')).stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
