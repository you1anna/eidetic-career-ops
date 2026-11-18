import shlex
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import wo_live


class Ran:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, stdin=None):
        self.calls.append((cmd, stdin.read() if stdin else None))
        return type('Done', (), {'returncode': 0})()


class GatewayTests(unittest.TestCase):
    def setUp(self):
        # Route discovery must not read the developer's configured live gateway. Tests below
        # exercise both Mac and restricted routes explicitly, with SSH replaced by Ran.
        home = tempfile.TemporaryDirectory(prefix='wo-gateway-test-')
        self.addCleanup(home.cleanup)
        fake_home = patch.object(wo_live.Path, 'home', return_value=Path(home.name))
        fake_home.start()
        self.addCleanup(fake_home.stop)

    def remote(self, *args):
        return wo_live.remote_command(list(args))

    def test_reads_and_agent_edits_run_as_agent(self):
        self.assertEqual(self.remote('decision', 'list'), 'wo --actor agent decision list')
        self.assertEqual(self.remote('show', 'CO-itoflow'), 'wo --actor agent show CO-itoflow')
        self.assertEqual(self.remote('week', 'include', 'CP-1'), 'wo --actor agent week include CP-1')
        self.assertEqual(shlex.split(self.remote('merge', 'CO-a', 'AG-b', '--reason', 'Recruiter')),
                         ['wo', '--actor', 'agent', 'merge', 'CO-a', 'AG-b', '--reason', 'Recruiter'])

    def test_incoming_process_uses_stdin_and_agent_attribution(self):
        self.assertEqual(self.remote('incoming-role','--json','-','--preview'),
                         'wo --actor agent incoming-role --json - --preview')
        with self.assertRaises(wo_live.Refused):self.remote('incoming-role','--actor','owner','--json','-')
        with self.assertRaises(wo_live.Refused):self.remote('incoming-role','--db','other','--json','-')

    def test_card_questions_are_listed_shown_and_answered_as_agent(self):
        self.assertEqual(self.remote('question', 'list'), 'wo --actor agent question list')
        self.assertEqual(self.remote('question', 'show', '3'), 'wo --actor agent question show 3')
        self.assertEqual(self.remote('question', 'answer', '3', '--text-file', '-'),
                         'wo --actor agent question answer 3 --text-file -')
        for bad in ([], ['ask', '3'], ['delete', '3']):
            with self.assertRaises(wo_live.Refused):
                self.remote('question', *bad)

    def test_collection_policy_is_a_read_only_gateway_command(self):
        self.assertEqual(self.remote('collection-policy','--json'),'wo --actor agent collection-policy --json')
        with self.assertRaises(wo_live.Refused):self.remote('collection-policy','--db','other')

    def test_acceptance_report_read_stays_on_the_gateway(self):
        self.assertEqual(self.remote('research-review','--date','2026-10-19'),
                         'wo --actor agent research-review --date 2026-10-19')
        with self.assertRaises(wo_live.Refused):self.remote('research-review','--db','other')

    def test_whole_database_and_schedule_commands_are_refused(self):
        for cmd in ('import-json', 'export-json', 'backup', 'prune-backups', 'migrate', 'ingest',
                    'import-scans', 'import-linkedin', 'watch', 'rm'):
            with self.assertRaises(wo_live.Refused, msg=cmd):
                self.remote(cmd, 'x')
        with self.assertRaises(wo_live.Refused):
            self.remote('decision', 'answer', '3')
        with self.assertRaises(wo_live.Refused):
            self.remote()

    def test_revision_safe_decisions_and_outcomes_preserve_agent_attribution(self):
        for command in (['decision', 'claim', '42', '--revision', '3'],
                        ['decision', 'block', '42', 'Need evidence', '--revision', '4'],
                        ['outcome', 'CP-example', 'replied', '--revision', '8', '--note', 'The owner confirmed the reply'],
                        ['correct', 'CP-example', '--outcome', '3', '--revision', '13', '--note', 'The owner: tapped by mistake']):
            self.assertEqual(shlex.split(self.remote(*command)), ['wo', '--actor', 'agent', *command])

    def test_actor_and_database_cannot_be_chosen(self):
        for bad in (['--actor', 'owner', 'show', 'x'], ['show', 'x', '--actor=owner'], ['--db', '/tmp/x.db', 'state'],
                    ['state', '--db=/tmp/x.db']):
            with self.assertRaises(wo_live.Refused, msg=bad):
                self.remote(*bad)

    def test_shell_characters_stay_inside_one_argument(self):
        for text in ('x; rm -rf /', "it's", '$(touch /tmp/p)', '`id`', 'a && b', 'line\nbreak'):
            remote = self.remote('edit', 'CO-a', '--json', text)
            self.assertEqual(shlex.split(remote)[-1], text)

    def test_import_research_uploads_under_its_own_name_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / '2026-09-29-approach.json'
            path.write_text('[]')
            run = Ran()
            self.assertEqual(wo_live.main(['import-research', str(path)], run=run), 0)
            cmd, sent = run.calls[0]
            self.assertEqual(sent, b'[]')
            self.assertIn("import-research \"$d\"/2026-09-29-approach.json", cmd[-1])
            self.assertIn('rm -rf "$d"', cmd[-1])
            # Staged as the service user, where the import can read it and no other user can.
            self.assertIn('d=$(sudo -n -u workops mktemp -d -p /srv/work-ops/run)', cmd[-1])
            self.assertIn('sudo -n -u workops tee "$d"/2026-09-29-approach.json >/dev/null', cmd[-1])
            self.assertIn('BatchMode=yes', cmd)
            # Names the VPS key, so the agent's other keys (GitHub) are never offered to the VPS.
            self.assertEqual(cmd[1:4], ['-i', wo_live.KEY, '-o'])
            self.assertIn('IdentitiesOnly=yes', cmd)
        for bad in ('a b.json', 'x;y.json', 'notes.txt'):
            with self.assertRaises(wo_live.Refused):
                self.remote('import-research', bad)

    def test_import_list_stages_one_file_and_passes_only_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / '2026-10-20-list.json'
            path.write_text('{"rows": []}')
            for flags, tail in (([], 'import-list "$d"/2026-10-20-list.json;'),
                                (['--dry-run'], 'import-list "$d"/2026-10-20-list.json --dry-run;')):
                run = Ran()
                self.assertEqual(wo_live.main(['import-list', str(path), *flags], run=run), 0)
                cmd, sent = run.calls[0]
                self.assertEqual(sent, b'{"rows": []}')
                self.assertIn('wo --actor agent ' + tail, cmd[-1])
                self.assertIn('d=$(sudo -n -u workops mktemp -d -p /srv/work-ops/run)', cmd[-1])
                self.assertIn('rm -rf "$d"', cmd[-1])
        for bad in (['import-list'], ['import-list', 'list.json', '--force'], ['import-list', 'list.json', '--dry-run', '--dry-run'],
                    ['import-list', 'list.json', '--dry-run=yes'], ['import-list', 'a b.json'], ['import-list', 'list.csv'],
                    ['import-list', 'list.json', '--actor', 'owner'], ['import-research', 'x.json', '--dry-run'],
                    ['import-linkedin-export', 'snapshot.json', '--tranche', 'all']):
            with self.assertRaises(wo_live.Refused, msg=bad):
                self.remote(*bad)

    def test_a_refusal_exits_without_contacting_the_server(self):
        run = Ran()
        self.assertEqual(wo_live.main(['import-json', 'x'], run=run), 64)
        self.assertEqual(run.calls, [])

    def test_restricted_transport_sends_upload_without_shell_and_pins_host(self):
        route = dict(host='ops@127.0.0.1', key='/home/synthetic/.ssh/workops_live',
                     host_key_alias='203.0.113.10', restricted=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'synthetic.json'
            path.write_text('{"rows":[]}')
            run = Ran()
            self.assertEqual(wo_live.main(['import-list', str(path), '--dry-run'], run=run, route=route), 0)
        command, sent = run.calls[0]
        self.assertEqual(sent, b'{"rows":[]}')
        self.assertEqual(shlex.split(command[-1]),
                         ['wo', '--actor', 'agent', 'import-list', '--stdin', 'synthetic.json', '--dry-run'])
        for option in ('StrictHostKeyChecking=yes', 'HostKeyAlias=203.0.113.10',
                       'ForwardAgent=no', 'ClearAllForwardings=yes', 'ControlPath=none',
                       'ControlMaster=no', 'ControlPersist=no', 'IdentityAgent=none'):
            self.assertIn(option, command)
        self.assertEqual(command[2], route['key'])

    def test_bad_route_fails_closed_without_ssh(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(wo_live.Path, 'home', return_value=Path(folder)):
            config = Path(folder) / '.config/work-ops/live-gateway.json'
            config.parent.mkdir(parents=True)
            config.write_text('{"restricted":false}')
            run = Ran()
            self.assertEqual(wo_live.main(['state'], run=run), 64)
            self.assertEqual(run.calls, [])


if __name__ == '__main__':
    unittest.main()
