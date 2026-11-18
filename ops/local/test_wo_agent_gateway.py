"""Server-side authorization tests; synthetic requests only."""
import importlib.util
import io
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('wo_agent_gateway',
    Path(__file__).resolve().parents[1] / 'vps' / 'wo_agent_gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class ServerGatewayTests(unittest.TestCase):
    def request(self, *args):
        return gateway.validate(shlex.join(['wo', '--actor', 'agent', *args]))

    def test_record_reads_edits_and_week_are_agent_only(self):
        for args in (['find', 'Synthetic Person'], ['show', 'CP-synthetic'],
                     ['edit', 'CP-synthetic', '--json', '{"summary":"a; $(id)"}'],
                     ['week', 'include', 'CP-synthetic'],
                     ['decision', 'claim', '3', 'Requested work', '--revision', '4'],
                     ['question', 'answer', '4', '--text-file', '-']):
            actual, upload = self.request(*args)
            self.assertEqual(actual, args)
            self.assertIsNone(upload)

    def test_out_of_scope_commands_fail_before_execution(self):
        for command in ('', 'id', '/bin/sh', 'sudo -n id', 'sftp',
                        'wo --actor owner state', 'wo --db /tmp/x state',
                        'wo --actor agent show CP-x; id',
                        'wo --actor agent state && id',
                        'wo --actor agent decision answer 1 approve',
                        'wo --actor agent week refresh',
                        'wo --actor agent import-research /etc/passwd',
                        'wo --actor agent show CP-x --db=/tmp/x',
                        'wo --actor agent state --act=owner',
                        'wo --actor agent state --env-file=/etc/passwd'):
            with self.subTest(command=command), self.assertRaises(gateway.Refused):
                gateway.validate(command)
        for command in ('backup', 'migrate', 'ingest', 'deploy', 'export-json',
                        'import-json', 'prune-backups', 'import-scans', 'fetch'):
            with self.subTest(command=command), self.assertRaises(gateway.Refused):
                self.request(command, '/tmp/x')

    def test_policy_is_a_subset_of_the_client_gateway(self):
        import wo_live
        roots = {name.split()[0] for name in gateway.RULES}
        self.assertEqual(roots, (wo_live.READS | wo_live.EDITS | set(wo_live.STAGED)
                                | {'decision', 'question', 'week'}) - {'fetch'})
        for category, allowed in (('decision', wo_live.DECISION), ('question', wo_live.QUESTION)):
            self.assertEqual({key.split()[1] for key in gateway.RULES if key.startswith(category + ' ')}, allowed)

    def test_text_files_require_stdin_and_option_abbreviations_are_refused(self):
        for command in (['draft', 'CP-x', '--text-file', '/etc/passwd'],
                        ['draft', 'CP-x', '--text-file=/etc/passwd'],
                        ['draft', 'CP-x', '--text-f', '/etc/passwd'],
                        ['question', 'answer', '1', '--text-file', '/proc/self/environ']):
            with self.subTest(command=command), self.assertRaises(gateway.Refused):
                self.request(*command)

    def test_staged_uploads_are_bounded_and_never_use_a_shell(self):
        args, name = self.request('import-list', '--stdin', 'synthetic.json', '--dry-run')
        self.assertEqual(args, ['import-list', '--dry-run'])
        self.assertEqual(name, 'synthetic.json')
        for name in ('../x.json', '/tmp/x.json', 'x;y.json', 'x.json\n', '.json'):
            with self.subTest(name=name), self.assertRaises(gateway.Refused):
                self.request('import-research', '--stdin', name)
        with self.assertRaises(gateway.Refused):
            gateway.read_input(io.BytesIO(b'x' * 5), limit=4)

    def test_execution_has_fixed_database_actor_environment_and_cleans_upload(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            self.assertEqual(command[:6], [gateway.PYTHON, '-I', gateway.CLI,
                             '--db', gateway.DATABASE, '--actor'])
            self.assertEqual(command[6], 'agent')
            self.assertNotIn('shell', kwargs)
            self.assertNotIn('SSH_AUTH_SOCK', kwargs['env'])
            self.assertEqual(Path(command[-2]).read_bytes(), b'{"rows":[]}')
            return type('Done', (), {'returncode': 0})()
        with tempfile.TemporaryDirectory() as folder, patch.object(gateway, 'STAGING', folder):
            status = gateway.execute('wo --actor agent import-list --stdin synthetic.json --dry-run',
                                     io.BytesIO(b'{"rows":[]}'), run=run)
            self.assertEqual(status, 0)
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
