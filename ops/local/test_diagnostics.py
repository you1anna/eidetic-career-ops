"""Security boundaries use only synthetic files and child processes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

VPS = Path(__file__).resolve().parents[1] / 'vps'
sys.path.insert(0, str(VPS))


class DiagnosticTests(unittest.TestCase):
    def module(self):
        self.assertTrue((VPS / 'wo_diagnostics.py').exists(), 'diagnostics helper is not implemented')
        import wo_diagnostics
        return wo_diagnostics

    def test_only_fixed_commands_and_units_are_accepted(self):
        d = self.module()
        self.assertEqual(d.validate('wo-diagnostics status'), ('status', None))
        self.assertEqual(d.validate('wo-diagnostics logs wo-email.service'), ('logs', 'wo-email.service'))
        for bad in ('', 'id', 'wo-diagnostics', 'wo-diagnostics status; id',
                    'wo-diagnostics status --file /etc/shadow', 'wo-diagnostics logs ssh.service',
                    'wo-diagnostics logs --all', 'wo-diagnostics logs ../wo-email.service',
                    'wo-diagnostics logs wo-email.service --since yesterday'):
            with self.subTest(bad=bad), self.assertRaises(d.Refused):
                d.validate(bad)

    def test_journal_projection_never_returns_input_messages(self):
        d = self.module()
        rows = [dict(__REALTIME_TIMESTAMP='1792857600000000', PRIORITY='3',
                     MESSAGE="wo-email.service: Failed with result 'exit-code'."),
                dict(__REALTIME_TIMESTAMP='1792857600000000', PRIORITY='3',
                     MESSAGE='SECRET alice@example.test Bearer abc'),
                dict(__REALTIME_TIMESTAMP='SECRET', PRIORITY='SECRET', MESSAGE='SECRET')]
        result = d.project_journal('\n'.join(json.dumps(r) for r in rows))
        text = json.dumps(result)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('alice', text)
        self.assertEqual(result['events'][0]['event'], 'failed')
        self.assertEqual(result['events'][0]['result'], 'exit-code')
        self.assertEqual(result['unclassified'], 2)
        self.assertLessEqual(len(d.project_journal('\n'.join(json.dumps(rows[0]) for _ in range(90)))['events']), 50)

    def test_crash_and_backup_projection_allow_only_metadata(self):
        d = self.module()
        at = '2026-10-24T06:30:09Z'
        crash = d.project_crash(dict(at=at, error='OperationalError: SECRET',
                                     where='SECRET', run_id='SECRET', recent=[at, 'SECRET']))
        self.assertEqual(crash['error_type'], 'OperationalError')
        self.assertNotIn('SECRET', json.dumps(crash))
        backup = d.project_backup(dict(at=at, result='passed', backup='SECRET', arbitrary='SECRET'))
        self.assertEqual(backup['at'], at)
        self.assertNotIn('SECRET', json.dumps(backup))
        self.assertIsNone(d.project_backup(dict(at='SECRET', result='SECRET'))['at'])

    def test_file_reader_refuses_symlinks_parents_fifo_and_oversize(self):
        d = self.module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'safe').write_text('{}')
            self.assertEqual(d.read_file(root / 'safe'), b'{}')
            (root / 'link').symlink_to(root / 'safe')
            (root / 'dirlink').symlink_to(root, target_is_directory=True)
            os.mkfifo(root / 'fifo')
            (root / 'large').write_bytes(b'x' * 65537)
            for name in ('link', 'dirlink/safe', 'fifo', 'large'):
                with self.subTest(name=name), self.assertRaises((OSError, ValueError)):
                    d.read_file(root / name)

    def test_command_runner_bounds_output_timeout_and_stderr(self):
        d = self.module()
        r = d.command([sys.executable, '-c', 'print("x"*300000)'])
        self.assertEqual(r['state'], 'output-limit')
        self.assertNotIn('output', r)
        r = d.command([sys.executable, '-c', 'import time; time.sleep(2)'], timeout=.05)
        self.assertEqual(r['state'], 'timeout')
        r = d.command([sys.executable, '-c', 'import sys; print("SECRET",file=sys.stderr); sys.exit(1)'])
        self.assertEqual(r['state'], 'unavailable')
        self.assertNotIn('SECRET', json.dumps(r))

    def test_status_failure_is_unknown_not_green_and_has_no_write_commands(self):
        d = self.module()
        calls = []
        def failed(argv, **kwargs):
            calls.append(argv)
            return {'state': 'unavailable'}
        result = d.status(run=failed, read=lambda p: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertEqual(result['origin']['state'], 'unavailable')
        self.assertEqual(result['units']['state'], 'unavailable')
        self.assertEqual(result['backup']['state'], 'unavailable')
        self.assertNotIn('SECRET', json.dumps(result))
        for args in calls:
            self.assertNotIn('systemd-run', ' '.join(args))
            self.assertNotIn('start', args)
            self.assertNotIn('restart', args)

    def test_service_memory_separates_file_cache_and_counts_oom_kills(self):
        d = self.module()
        base = '/sys/fs/cgroup/system.slice/work-ops-workspace.service/'
        files = {base + 'memory.peak': b'300000000\n',
                 base + 'memory.stat': b'anon 90000000\nfile 150000000\nsock 0\nSECRET 1\n',
                 base + 'memory.events': b'low 0\nhigh 0\nmax 2\noom 1\noom_kill 1\noom_group_kill 0\n',
                 '/sys/fs/cgroup/system.slice/wo-email.service/memory.stat': b'anon ?\n'}
        def read(path):
            if str(path).startswith('/sys/fs/cgroup/system.slice/wo-caretaker.service/'):
                raise PermissionError(path)
            try:
                return files[str(path)]
            except KeyError:
                raise FileNotFoundError(path) from None
        result = d.service_memory(read)
        self.assertEqual(result['work-ops-workspace.service'], {
            'state': 'observed', 'peak_bytes': 300000000, 'anon_bytes': 90000000, 'file_bytes': 150000000,
            'events': {'high': 0, 'max': 2, 'oom': 1, 'oom_kill': 1}})
        self.assertEqual(result['wo-email.service'], {'state': 'observed', 'peak_bytes': None,
                                                      'anon_bytes': None, 'file_bytes': None, 'events': {}})
        self.assertEqual(result['wo-scan.service'], {'state': 'absent'})
        self.assertEqual(result['wo-caretaker.service'], {'state': 'unavailable'})
        self.assertFalse(any(unit.endswith('.timer') for unit in result))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(d.status(run=lambda argv, **k: {'state': 'unavailable'}, read=read)['service_memory'], result)

    def test_timer_dates_and_private_unit_fields_are_projected(self):
        d = self.module()
        result = d.units_projection('Id=wo-email.timer\nActiveState=active\n'
            'NextElapseUSecRealtime=Sat 2026-10-24 18:00:27 UTC\nLastTriggerUSec=Sat 2026-10-24 17:45:27 UTC\n'
            'Environment=SECRET\nExecStart=SECRET\n\n')
        self.assertEqual(result['wo-email.timer']['NextElapseUSecRealtime'], '2026-10-24T18:00:27+00:00')
        self.assertNotIn('SECRET', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
