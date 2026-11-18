"""Read-only bot gateway: synthetic requests; the document check uses a throwaway database."""
import base64
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
VPS = ROOT / 'ops' / 'vps'


def load(name, path=None):
    spec = importlib.util.spec_from_file_location(name, path or VPS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway = load('wo_agent_gateway')
read = load('wo_read_gateway')

# Pinned independently of the module: widening the read key must be a deliberate, reviewed change.
EXPECTED_READ = {'brief', 'find', 'show', 'approach', 'quality', 'import-report', 'fetch-log',
                 'decision list', 'decision show', 'question list', 'question show'}


def request(*args):
    return shlex.join(['wo', '--actor', 'agent', *args])


def minimal(name):
    """The shortest request the record gateway itself accepts for one RULES entry."""
    count, values, _ = gateway.RULES[name]
    upload = ['--stdin', 'synthetic.json'] if '--stdin' in values else []
    return request(*name.split(), *['x'] * count, *upload)


class ReadGatewayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.lock = str(Path(self.folder.name) / 'read.lock')
        self.calls, self.notes = [], []

    def tearDown(self):
        self.folder.cleanup()

    def fake_run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return types.SimpleNamespace(returncode=0)

    def call(self, original, argv=('--caller', 'hermes'), run=None, module=read):
        return module.main(list(argv), original, run=run or self.fake_run, lock=self.lock, wait=0,
                           note=self.notes.append)

    def test_read_set_is_pinned_and_inside_the_record_gateway(self):
        self.assertEqual(set(read.READ), EXPECTED_READ)
        self.assertLessEqual(set(read.READ), set(gateway.RULES))
        self.assertNotIn('state', read.READ)

    def test_no_read_accepts_input(self):
        for name in sorted(read.READ):
            with self.subTest(name=name):
                self.assertFalse(gateway.RULES[name][1] & {'--json', '--text-file', '--stdin'})

    def test_each_read_runs_once_as_agent_on_the_live_database_without_input(self):
        for name in sorted(EXPECTED_READ):
            with self.subTest(name=name):
                self.calls.clear()
                self.assertEqual(self.call(minimal(name)), 0)
                [(command, kwargs)] = self.calls
                self.assertEqual(command[:7], [gateway.PYTHON, '-I', gateway.CLI, '--db',
                                               gateway.DATABASE, '--actor', 'agent'])
                self.assertEqual(command[7:], shlex.split(minimal(name))[3:])
                self.assertEqual(kwargs['input'], b'')
                self.assertNotIn('shell', kwargs)

    def test_every_other_gateway_command_is_refused_before_execution(self):
        for name in sorted(set(gateway.RULES) - EXPECTED_READ):
            with self.subTest(name=name):
                gateway.validate(minimal(name))  # the record gateway would run it
                self.assertEqual(self.call(minimal(name)), 64)
        for original in ('', 'id', '/bin/sh', 'sftp', 'wo --actor owner state', 'wo --actor agent state',
                         'wo --actor agent show CP-x; id', 'wo --actor agent fetch https://example.com',
                         'wo --actor agent decision answer 1 approve', 'wo --actor agent show CP-x --db=/tmp/x'):
            with self.subTest(original=original):
                self.assertEqual(self.call(original), 64)
        self.assertEqual(self.calls, [])

    def test_the_key_must_name_exactly_one_valid_caller(self):
        for argv in ([], ['--caller'], ['--caller', 'Hermes'], ['--caller', 'her mes'], ['--caller', 'a;id'],
                     ['--caller', '-x'], ['--caller', 'x' * 33], ['--caller', 'hermes', '--caller', 'codex'],
                     ['--profile', 'read']):
            with self.subTest(argv=argv):
                self.assertEqual(self.call(request('brief'), argv=argv), 64)
        self.assertEqual(self.calls, [])
        self.assertTrue(all(note.startswith('caller=unknown command=refused status=64 ') for note in self.notes))

    def test_a_held_lock_is_busy_and_runs_nothing_until_released(self):
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.call(request('brief')), 75)
            self.assertEqual(self.calls, [])
        finally:
            os.close(fd)
        self.assertEqual(self.call(request('brief')), 0)
        self.assertEqual(len(self.calls), 1)

    def test_an_unsafe_lock_is_refused(self):
        target = Path(self.folder.name) / 'elsewhere'
        target.write_text('')
        os.symlink(target, self.lock)
        self.assertEqual(self.call(request('brief')), 64)
        os.unlink(self.lock)
        Path(self.lock).write_text('')
        os.chmod(self.lock, 0o644)
        self.assertEqual(self.call(request('brief')), 64)
        self.assertEqual(self.calls, [])

    def test_journal_records_caller_command_and_status_never_arguments(self):
        self.call(request('find', 'Secret Person', '--type', 'person'))
        self.call(request('decision', 'show', '12'))
        self.call(request('edit', 'CP-secret', '--json', '{"summary":"x"}'))
        self.assertEqual([note.rsplit(' ms=', 1)[0] for note in self.notes],
                         ['caller=hermes command=find status=0',
                          'caller=hermes command=decision-show status=0',
                          'caller=hermes command=edit status=64'])

    def test_a_timeout_is_refused(self):
        def slow(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 180)
        self.assertEqual(self.call(request('approach'), run=slow), 64)

    def test_an_older_record_gateway_fails_closed(self):
        older = types.ModuleType('wo_agent_gateway')
        older.Refused, older.validate, older.execute = gateway.Refused, gateway.validate, gateway.execute
        with patch.dict(sys.modules, {'wo_agent_gateway': older}):
            stale = load('wo_read_gateway_stale', VPS / 'wo_read_gateway.py')
        self.assertFalse(stale.COMPATIBLE)
        self.assertEqual(self.call(request('brief'), module=stale), 64)
        self.assertEqual(self.calls, [])


class PrivateDocumentTests(unittest.TestCase):
    """The read commands may list a supplied document's name, never its contents."""

    def test_reads_never_return_document_contents(self):
        marker = 'PRIVATE-DOCUMENT-MARKER-7f3c'
        encoded = base64.b64encode(marker.encode()).decode()
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'synthetic.db'

            def wo(*args, data=None):
                return subprocess.run([sys.executable, '-I', str(ROOT / 'tools' / 'wo.py'), '--db', str(database),
                                       '--actor', 'agent', *args], input=data, capture_output=True, text=True,
                                      timeout=120, cwd=folder)

            self.assertEqual(wo('migrate').returncode, 0)
            payload = dict(request_id='read-gateway-1', name='Synthetic Quality Lead', employer='Example Client',
                           agency='Example Search', origin='recruiter', origin_date='2026-10-15',
                           stage='screening_completed', stage_date='2026-10-21',
                           source_ref='Synthetic recruiter note', source_text='Synthetic role.',
                           documents=[dict(filename='synthetic-spec.txt', kind='specification', content=encoded)])
            saved = wo('incoming-role', '--json', '-', data=json.dumps(payload))
            self.assertEqual(saved.returncode, 0, saved.stderr)
            record = json.loads(saved.stdout)['record']['id']
            reads = {'show': ('show', record), 'find': ('find', 'Synthetic'), 'brief': ('brief', '--md'),
                     'approach': ('approach',), 'quality': ('quality', '--json'), 'import-report': ('import-report',),
                     'decision list': ('decision', 'list'), 'question list': ('question', 'list', '--all')}
            outputs = {name: wo(*args) for name, args in reads.items()}
        for name, result in outputs.items():
            with self.subTest(name=name):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(marker, result.stdout)
                self.assertNotIn(encoded, result.stdout)
        # The document is listed by name, so the check above did cover a record holding one.
        self.assertIn('synthetic-spec.txt', outputs['show'].stdout)


if __name__ == '__main__':
    unittest.main()
