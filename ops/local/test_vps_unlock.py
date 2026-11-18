"""No real keys, Keychain or network: exercise credential/permission distinctions."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'vps-unlock'


class VpsUnlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = self.root / 'key'
        self.key.write_text('synthetic encrypted key')
        self.env = dict(os.environ, SSH_KEY=str(self.key),
                        PATH=f'{self.root}:/usr/bin:/bin', AGENT_STATE='loaded',
                        SSH_RESULT='denied', KEY_PLAIN='0', KEYCHAIN='0',
                        KEYCHAIN_MARKER=str(self.root / 'keychain-loaded'),
                        SSH_ARGS=str(self.root / 'ssh-args'))
        self.fake('uname', 'echo Darwin')
        self.fake('ssh-keygen', '''if [ "$1" = -lf ]; then
  echo '256 SHA256:test synthetic'; exit 0
fi
[ "$KEY_PLAIN" = 1 ]''')
        self.fake('ssh-add', '''if [ "$1" != -l ]; then
  [ "$KEYCHAIN" = 1 ] && touch "$KEYCHAIN_MARKER" && exit 0
  exit 1
fi
if [ -e "$KEYCHAIN_MARKER" ]; then echo '256 SHA256:test synthetic'; exit 0; fi
case "$AGENT_STATE" in
  loaded) echo '256 SHA256:test synthetic';;
  empty) echo 'The agent has no identities.'; exit 1;;
  other) echo '256 SHA256:another other';;
  denied) echo 'Error connecting to agent: Operation not permitted' >&2; exit 2;;
esac''')
        self.fake('ssh', '''printf '%s\n' "$@" > "$SSH_ARGS"
case "$SSH_RESULT" in
  ok) exit 0;;
  denied) echo 'ops@host: Permission denied (publickey).' >&2;;
  timeout) echo 'ssh: connect to host: Operation timed out' >&2;;
  noroute) echo 'ssh: connect to host: No route to host' >&2;;
  hostkey) echo 'Host key verification failed.' >&2;;
esac
exit 255''')

    def fake(self, name, body):
        path = self.root / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def check(self, **values):
        return subprocess.run(['/bin/bash', str(SCRIPT), '--check'],
                              env=dict(self.env, **values), capture_output=True, text=True)

    def test_loaded_key_and_plain_key_remain_fast_local_checks(self):
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(self.check(AGENT_STATE='denied', KEY_PLAIN='1').returncode, 0)
        self.assertFalse((self.root / 'ssh-args').exists())

    def test_inaccessible_agent_is_not_a_locked_key(self):
        result = self.check(AGENT_STATE='denied')
        self.assertEqual(result.returncode, 4)
        self.assertIn('agent', result.stderr)
        self.assertIn('permission', result.stderr.lower())
        self.assertNotIn('key is locked', result.stderr)

    def test_keychain_ssh_can_work_without_an_accessible_agent(self):
        self.assertEqual(self.check(AGENT_STATE='denied', SSH_RESULT='ok').returncode, 0)

    def test_empty_or_other_agent_with_rejected_auth_needs_unlock(self):
        for state in ('empty', 'other'):
            with self.subTest(state=state):
                result = self.check(AGENT_STATE=state)
                self.assertEqual(result.returncode, 3)
                self.assertIn('ops/vps-unlock', result.stderr)

    def test_network_failure_is_not_reported_as_a_locked_key(self):
        result = self.check(AGENT_STATE='empty', SSH_RESULT='timeout')
        self.assertEqual(result.returncode, 4)
        self.assertIn('timed out', result.stderr)

    def test_unreachable_tailnet_address_suggests_checking_tailscale(self):
        # 3 November: a stopped Tailscale client surfaced only as an inconclusive timeout.
        for ssh_result in ('timeout', 'noroute'):
            with self.subTest(ssh_result=ssh_result):
                result = self.check(AGENT_STATE='empty', SSH_RESULT=ssh_result)
                self.assertEqual(result.returncode, 4)
                self.assertIn('tailscale status', result.stderr)
        other = self.check(AGENT_STATE='empty', SSH_RESULT='hostkey')
        self.assertEqual(other.returncode, 4)
        self.assertNotIn('tailscale', other.stderr.lower())

    def test_keychain_reload_preserves_local_readiness(self):
        self.assertEqual(self.check(AGENT_STATE='empty', KEYCHAIN='1').returncode, 0)
        self.assertTrue((self.root / 'keychain-loaded').exists())
        self.assertFalse((self.root / 'ssh-args').exists())

    def test_fallback_is_bounded_noninteractive_and_uses_selected_key(self):
        self.check(AGENT_STATE='empty')
        args = (self.root / 'ssh-args').read_text().splitlines()
        self.assertIn('BatchMode=yes', args)
        self.assertIn('IdentitiesOnly=yes', args)
        self.assertIn('ConnectTimeout=10', args)
        self.assertEqual(args[args.index('-i') + 1], str(self.key))
        self.assertEqual(args[-1], 'true')

    def test_session_start_only_requests_unlock_for_status_three(self):
        ops = self.root / 'ops'
        ops.mkdir()
        hook = ops / 'session-start.sh'
        hook.write_text((SCRIPT.parent / 'session-start.sh').read_text())
        for name, body in [('make-venv.sh', 'exit 0'),
                           ('vps-unlock', 'echo "synthetic key diagnostic" >&2; exit "$CHECK_STATUS"')]:
            stub = ops / name
            stub.write_text('#!/bin/bash\n' + body + '\n')
            stub.chmod(0o755)
        for status in (0, 2, 3, 4):
            with self.subTest(status=status):
                out = subprocess.run(['/bin/bash', str(hook)], capture_output=True, text=True,
                                     env=dict(self.env, HOME=str(self.root), CHECK_STATUS=str(status)))
                self.assertEqual(out.returncode, 0)
                self.assertEqual('Ask the owner to run' in out.stdout, status == 3)
                self.assertIn('synthetic key diagnostic', out.stderr)
                if status != 3:
                    self.assertIn(f'key readiness status {status}', out.stdout)

    def test_noninteractive_unlock_keeps_failure_status_and_never_claims_success(self):
        for state, expected in [('denied', 4), ('empty', 3), ('loaded', 255)]:
            # An available key still needs an actual SSH success for the interactive command.
            with self.subTest(state=state):
                out = subprocess.run(['/bin/bash', str(SCRIPT)], stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True,
                                     env=dict(self.env, AGENT_STATE=state))
                self.assertEqual(out.returncode, expected)
                self.assertNotIn('answers', out.stdout)
        out = subprocess.run(['/bin/bash', str(SCRIPT)], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True,
                             env=dict(self.env, AGENT_STATE='denied', SSH_RESULT='ok'))
        self.assertEqual(out.returncode, 0)
        self.assertIn('answers', out.stdout)

    def test_session_start_uses_gateways_when_restricted_route_exists(self):
        ops = self.root / 'ops'
        ops.mkdir()
        hook = ops / 'session-start.sh'
        hook.write_text((SCRIPT.parent / 'session-start.sh').read_text())
        config = self.root / '.config/work-ops/live-gateway.json'
        config.parent.mkdir(parents=True)
        config.write_text('{}')
        for name, body in [('make-venv.sh', 'exit 0'),
                           ('wo-live', 'echo "gateway $*" >> "$TEST_ROOT/gateway-calls"; echo "No pending work"'),
                           ('wo-diagnostics', 'echo "diagnostics" >> "$TEST_ROOT/gateway-calls"; echo \'{"report":{"origin":{"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}}\'')]:
            stub = ops / name
            stub.write_text('#!/bin/bash\n' + body + '\n')
            stub.chmod(0o755)
        out = subprocess.run(['/bin/bash', str(hook)], capture_output=True, text=True,
                             env=dict(self.env, HOME=str(self.root), TEST_ROOT=str(self.root)))
        self.assertEqual(out.returncode, 0)
        self.assertTrue((self.root / 'gateway-calls').exists(), 'session bypassed the gateways')
        self.assertIn('gateway decision list', (self.root / 'gateway-calls').read_text())
        self.assertIn('diagnostics', (self.root / 'gateway-calls').read_text())
        self.assertFalse((self.root / 'ssh-args').exists(), 'session used direct administrator SSH')

    def test_missing_key_has_its_own_exit_status(self):
        self.assertEqual(self.check(SSH_KEY=str(self.root / 'missing')).returncode, 2)


if __name__ == '__main__':
    unittest.main()
