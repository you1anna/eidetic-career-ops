"""Credential transport checks use stand-ins only: no keys, .env file or SSH connection."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class SendCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'ops/vps/send-credential.sh'
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'ops/vps/send-credential.sh', self.script)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.trace = self.root / 'trace'
        self.payload = self.root / 'payload'
        self.command(self.root / '.venv/bin/python',
                     'printf "python\\n" >> "$TEST_TRACE"\n'
                     'printf %s synthetic-secret\n')
        self.command(self.bin / 'ssh',
                     'printf "%s\\n" "$@" >> "$TEST_TRACE"\n'
                     'cat > "$TEST_PAYLOAD"\n')

    def command(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def run_script(self, *args):
        return subprocess.run(['/bin/bash', str(self.script), *args], capture_output=True, text=True,
                              timeout=5, env={**os.environ, 'PATH': str(self.bin) + ':/usr/bin:/bin',
                              'TEST_TRACE': str(self.trace), 'TEST_PAYLOAD': str(self.payload),
                              'SERVER_HOST': 'example.test', 'SSH_USER': 'test-ops',
                              'SSH_KEY': '/test/key with spaces'})

    def test_invalid_names_and_extra_arguments_never_read_or_send_credentials(self):
        for args in (('BAD_KEY',), ('REED_API_KEY; id',), ('REED_API_KEY', 'extra'), ()):
            with self.subTest(args=args):
                self.trace.unlink(missing_ok=True)
                self.payload.unlink(missing_ok=True)
                result = self.run_script(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.trace.exists(), result.stdout + result.stderr)
                self.assertFalse(self.payload.exists())

    def test_approved_key_uses_private_stdin_and_configured_ssh(self):
        result = self.run_script('REED_API_KEY')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload.read_text(), 'synthetic-secret')
        arguments = self.trace.read_text().splitlines()
        self.assertIn('/test/key with spaces', arguments)
        self.assertIn('test-ops@example.test', arguments)
        self.assertIn('BatchMode=yes', arguments)
        self.assertIn('sudo -n /usr/local/bin/put-credential REED_API_KEY', arguments)
        self.assertNotIn('synthetic-secret', result.stdout + result.stderr + self.trace.read_text())


if __name__ == '__main__':
    unittest.main()
