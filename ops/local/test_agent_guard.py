import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import agent_guard

GUARD = Path(__file__).with_name('agent_guard.py')


def hook(payload, env=None):
    """Run the guard as Claude Code does: JSON on stdin, decision in the exit code."""
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(GUARD)], input=data, text=True, capture_output=True,
                          env=env or os.environ, timeout=10)


def bash(command):
    return {'tool_name': 'Bash', 'tool_input': {'command': command}}


class GuardTests(unittest.TestCase):
    def test_raw_live_routes_are_refused(self):
        for command in (
            'ssh ops@203.0.113.10 wo show CO-itoflow',
            "ssh -i ~/.ssh/work_ops_vps -o BatchMode=yes ops@203.0.113.10 "
            "'wo --actor agent import-research /tmp/x.json && rm /tmp/x.json'",
            'scp records/research/2026-10-04-approach.json ops@203.0.113.10:/tmp/',
            'rsync -a ops@203.0.113.10:/srv/work-ops/data/ ./copy',
            'cd ops && ssh ops@203.0.113.10 sqlite3 /srv/work-ops/data/work-ops.db .tables',
            'ssh ops@203.0.113.10 cat /srv/work-ops/data/work-ops.db > live.db',
            'ssh ops@203.0.113.10 sudo -u workops /usr/local/bin/wo state',
            'ls\nssh ops@203.0.113.10 wo fetch-log --failures',
            'bash -c "ssh ops@203.0.113.10 wo state"',
            'FOO=1 ssh ops@203.0.113.10 wo find Acme',
            # The same server under its ssh-config aliases and on the tailnet.
            'ssh work-ops-vps wo state',
            'ssh codex-vps sqlite3 /srv/work-ops/data/work-ops.db .tables',
            'ssh ops@100.64.0.10 wo show CO-itoflow',
            'ssh ops@work-ops-vps.tailnet-example.ts.net wo state',
            'scp rec.json work-ops-vps:/tmp/',
            # A hop through the other Mac.
            "ssh mini 'ssh work-ops-vps wo state'",
        ):
            with self.subTest(command=command):
                self.assertTrue(agent_guard.refused(command))

    def test_the_gateway_and_other_ssh_use_are_allowed(self):
        for command in (
            'ops/wo-live show CO-itoflow',
            'ops/wo-live import-research records/research/2026-10-04-approach.json',
            'ops/wo-live create --json - < rec.json',
            'ssh ops@203.0.113.10 systemctl status wo-scan.timer',
            'ssh ops@203.0.113.10 journalctl -u wo-backup --since today',
            'ops/vps/deploy.sh --local-approved --dry-run',
            'ops/pull-backup.sh data/work-ops-2026-10-04.db',
            '.venv/bin/python tools/wo.py --db data/work-ops.db show CO-itoflow',
            'git commit -m "docs: never use ssh ops@203.0.113.10 wo show"',
            "grep -n 'ssh ops@203.0.113.10 wo' CLAUDE.md",
            'ssh github.com',
            "ssh mini 'cd ~/Projects/work-ops && git pull'",
            'echo "unbalanced',
        ):
            with self.subTest(command=command):
                self.assertFalse(agent_guard.refused(command))

    def test_hook_blocks_with_exit_2_and_tells_the_agent_the_route(self):
        result = hook(bash('ssh ops@203.0.113.10 wo state'))
        self.assertEqual(result.returncode, 2)
        self.assertIn('ops/wo-live', result.stderr)
        self.assertIn('! <command>', result.stderr)

    def test_hook_allows_everything_it_cannot_judge(self):
        for payload in ('not json', '{}', json.dumps({'tool_name': 'Bash', 'tool_input': {}}),
                        json.dumps({'tool_name': 'Edit', 'tool_input': {'command': 'ssh ops@203.0.113.10 wo state'}}),
                        bash('ops/wo-live decision list')):
            with self.subTest(payload=payload):
                result = hook(payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')

    def test_the_host_is_the_gateway_host(self):
        env = dict(os.environ, WO_LIVE_HOST='ops@203.0.113.7')
        self.assertEqual(hook(bash('ssh ops@203.0.113.7 wo state'), env).returncode, 2)
        self.assertEqual(hook(bash('ssh ops@198.51.100.9 wo state'), env).returncode, 0)


if __name__ == '__main__':
    unittest.main()
