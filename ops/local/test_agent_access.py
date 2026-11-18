"""Synthetic provisioning: preserve other keys and never switch before verification."""
import base64
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

VPS = Path(__file__).resolve().parents[1] / 'vps'
sys.path.insert(0, str(VPS))


def public_key(byte):
    # Real Ed25519 wire format; independent synthetic 32-byte key data.
    blob = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes([byte]) * 32
    return 'ssh-ed25519 ' + base64.b64encode(blob).decode()


def packet():
    return {'device': 'test-air', 'source': '100.64.0.21',
            'records': public_key(1), 'diagnostics': public_key(2)}


class ProvisionTests(unittest.TestCase):
    def module(self):
        self.assertTrue((VPS / 'agent_access.py').exists(), 'server provisioning not implemented')
        return importlib.import_module('agent_access')

    def test_add_is_idempotent_and_revoke_preserves_other_keys_exactly(self):
        a = self.module()
        original = b'# existing admin\nssh-ed25519 unrelated admin\n\n'
        added = a.edit_keys(original, packet(), 'add')
        self.assertTrue(added.startswith(original))
        self.assertIn(b'from="100.64.0.21",restrict,command=', added)
        self.assertIn(b'wo_agent_gateway.py', added)
        self.assertIn(b'wo_diagnostics.py', added)
        self.assertEqual(a.edit_keys(added, packet(), 'add'), added)
        self.assertEqual(a.edit_keys(added, packet(), 'remove'), original)

    def test_conflicting_existing_key_is_refused_without_changes(self):
        a = self.module()
        original = (public_key(1) + ' admin\n').encode()
        with self.assertRaises(a.Refused):
            a.edit_keys(original, packet(), 'add')
        with self.assertRaises(a.Refused):
            a.edit_keys(original, packet(), 'remove')

    def test_no_key_options_injection_broad_source_or_duplicate_identity(self):
        a = self.module()
        for key, value in (('source', '100.64.0.0/10'), ('source', '127.0.0.1'),
                           ('source', '192.168.1.5'), ('device', 'x"\ncommand="id'),
                           ('records', 'command="id" ' + public_key(1)),
                           ('diagnostics', public_key(1)), ('records', 'ssh-ed25519 AAAA')):
            request = packet()
            request[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(a.Refused):
                a.edit_keys(b'', request, 'add')

    def test_atomic_edit_rejects_symlink_and_preserves_mode(self):
        a = self.module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / 'authorized_keys'
            path.write_bytes(b'original\n')
            path.chmod(0o600)
            a.update_file(path, packet(), 'add', owner=os.getuid(), group=os.getgid())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            a.update_file(path, packet(), 'remove', owner=os.getuid(), group=os.getgid())
            self.assertEqual(path.read_bytes(), b'original\n')

            link = path.with_name('link')
            link.symlink_to(path)
            with self.assertRaises((a.Refused, OSError)):
                a.update_file(link, packet(), 'add', owner=os.getuid(), group=os.getgid())
            self.assertEqual(path.read_bytes(), b'original\n')

    def test_failed_validation_does_not_modify_authorized_keys(self):
        a = self.module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / 'authorized_keys'
            path.write_bytes(b'original\n')
            path.chmod(0o600)
            invalid = packet(); invalid['source'] = '*'
            with self.assertRaises(a.Refused):
                a.update_file(path, invalid, 'add', owner=os.getuid(), group=os.getgid())
            self.assertEqual(path.read_bytes(), b'original\n')

    def test_concurrent_authorizations_do_not_lose_other_devices(self):
        a = self.module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / 'authorized_keys'
            path.write_bytes(b'original\n'); path.chmod(0o600)
            requests = [dict(device='device-' + str(i), source='100.64.1.' + str(i),
                             records=public_key(i * 2), diagnostics=public_key(i * 2 + 1)) for i in range(1, 5)]
            def add(request):
                return a.update_file(path, request, 'add', owner=os.getuid(), group=os.getgid())
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertTrue(all(pool.map(add, requests)))
            content = path.read_bytes()
            self.assertTrue(content.startswith(b'original\n'))
            for request in requests:
                self.assertEqual(a.edit_keys(content, request, 'add'), content)
            self.assertEqual(len(content.splitlines()), 9)


class MacActivationTests(unittest.TestCase):
    def module(self):
        self.assertTrue((VPS.parent / 'local/provision_mac.py').exists(), 'Mac activation not implemented')
        return importlib.import_module('provision_mac')

    def test_failed_capability_check_does_not_publish_routes(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            calls = []
            def remote(action, request):
                calls.append(action)
            def verify(routes):
                raise m.Refused('verification failed')
            with self.assertRaises(m.Refused):
                m.activate(home, 'test-air', '100.64.0.21', remote=remote, verify=verify)
            self.assertEqual(calls, ['add'])
            self.assertFalse((home / '.config/work-ops/live-gateway.json').exists())
            self.assertFalse((home / '.config/work-ops/diagnostics-gateway.json').exists())

    def test_success_and_retry_preserve_keys_and_publish_private_routes(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            seen = []
            def verify(routes):
                seen.append(routes)
                self.assertFalse((home / '.config/work-ops/live-gateway.json').exists())
            m.activate(home, 'test-air', '100.64.0.21', remote=lambda *a: None, verify=verify)
            key = (home / '.ssh/workops_live').read_bytes()
            m.activate(home, 'test-air', '100.64.0.21', remote=lambda *a: None, verify=lambda r: None)
            self.assertEqual(key, (home / '.ssh/workops_live').read_bytes())
            for name in ('live-gateway.json', 'diagnostics-gateway.json'):
                path = home / '.config/work-ops' / name
                self.assertTrue(json.loads(path.read_text())['restricted'])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(seen), 1)

    def test_revocation_does_not_remove_route_and_enable_admin_fallback(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            m.activate(home, 'test-air', '100.64.0.21', remote=lambda *a: None, verify=lambda r: None)
            config = home / '.config/work-ops/live-gateway.json'
            before = config.read_bytes()
            actions = []
            m.revoke(home, 'test-air', '100.64.0.21', remote=lambda action, request: actions.append(action))
            self.assertEqual(actions, ['remove'])
            self.assertEqual(config.read_bytes(), before)

    def test_conflicting_route_is_refused_before_authorization(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            config = home / '.config/work-ops/live-gateway.json'
            config.parent.mkdir(parents=True)
            config.write_text('{}')
            config.chmod(0o600)
            calls = []
            with self.assertRaises(m.Refused):
                m.activate(home, 'test-air', '100.64.0.21', remote=lambda *a: calls.append(a), verify=lambda r: None)
            self.assertEqual(calls, [])
            self.assertEqual(config.read_text(), '{}')

    def test_partial_route_publication_is_rolled_back(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            real = m.publish
            def fail_second(path, data):
                if path.name == 'diagnostics-gateway.json':
                    raise OSError('synthetic disk failure')
                real(path, data)
            with patch.object(m, 'publish', side_effect=fail_second), self.assertRaises(OSError):
                m.activate(home, 'test-air', '100.64.0.21', remote=lambda *a: None, verify=lambda r: None)
            self.assertFalse((home / '.config/work-ops/live-gateway.json').exists())
            self.assertFalse((home / '.config/work-ops/diagnostics-gateway.json').exists())

    def test_dangling_public_key_symlink_never_creates_its_target(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            (home / '.ssh').mkdir(mode=0o700)
            target = home / 'must-not-be-created'
            (home / '.ssh/workops_live.pub').symlink_to(target)
            with self.assertRaises(m.Refused):
                m.activate(home, 'test-air', '100.64.0.21', remote=lambda *a: None, verify=lambda r: None)
            self.assertFalse(target.exists())
            self.assertFalse((home / '.ssh/workops_live').exists())


if __name__ == '__main__':
    unittest.main()
