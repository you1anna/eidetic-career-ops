import importlib
from pathlib import Path
import subprocess
import sys
import unittest

VPS = Path(__file__).resolve().parents[1] / 'vps'
sys.path.insert(0, str(VPS))


class CredentialChecks(unittest.TestCase):
    def module(self):
        self.assertTrue((VPS / 'credential_check.py').exists(), 'scoped credential check not implemented')
        return importlib.import_module('credential_check')

    def test_only_declared_installed_credentials_enter_isolated_test(self):
        c = self.module()
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[0] == '/usr/bin/systemctl':
                return subprocess.CompletedProcess(args, 0, '[Service]\nLoadCredentialEncrypted=SCAN_KEY\nLoadCredentialEncrypted=OPTIONAL_KEY\n', '')
            return subprocess.CompletedProcess(args, 0, '1\n', '')
        result = c.check('wo-scan.service', run=run, exists=lambda p: p.name == 'SCAN_KEY')
        self.assertEqual(result, {'service': 'wo-scan.service', 'stored': 1, 'decrypted': 1})
        args = calls[1]
        self.assertIn('LoadCredentialEncrypted=SCAN_KEY', args)
        self.assertNotIn('LoadCredentialEncrypted=OPTIONAL_KEY', args)
        self.assertIn('PrivateNetwork=yes', args)
        self.assertIn('InaccessiblePaths=/srv/work-ops /home /root', args)

    def test_undeclared_paths_and_unapproved_services_never_start_unit(self):
        c = self.module()
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '[Service]\nLoadCredentialEncrypted=KEY:/etc/shadow\n', '')
        with self.assertRaises(ValueError):
            c.check('ssh.service', run=run)
        self.assertEqual(calls, [])
        with self.assertRaises(ValueError):
            c.check('wo-email.service', run=run)
        self.assertEqual(len(calls), 1)

    def test_whitespace_reset_removes_old_credential_before_decryption(self):
        c = self.module()
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            output = ('[Service]\nLoadCredentialEncrypted=OLD_KEY\n'
                      '[Service]\nLoadCredentialEncrypted = \nLoadCredentialEncrypted = NEW_KEY\n')
            return subprocess.CompletedProcess(args, 0, output if len(calls) == 1 else '1\n', '')
        result = c.check('wo-email.service', run=run, exists=lambda p: True)
        self.assertEqual(result['decrypted'], 1)
        self.assertIn('LoadCredentialEncrypted=NEW_KEY', calls[1])
        self.assertNotIn('LoadCredentialEncrypted=OLD_KEY', calls[1])

    def test_continuation_is_refused_before_a_false_credential_declaration(self):
        c = self.module()
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            output = '[Service]\nEnvironment=EXAMPLE=one ' + chr(92) + '\nLoadCredentialEncrypted=UNRELATED_KEY\n'
            return subprocess.CompletedProcess(args, 0, output if len(calls) == 1 else '1\n', '')
        with self.assertRaises(ValueError):
            c.check('wo-email.service', run=run, exists=lambda p: True)
        self.assertEqual(len(calls), 1, 'unsupported syntax started a credential unit')


if __name__ == '__main__':
    unittest.main()
