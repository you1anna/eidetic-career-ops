"""Protected Codex maintenance: preparation keeps old packages; activation is attended and reversible."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import codex_maintenance as installer

# Synthetic stand-in for the VPS's reviewed 0.161.0 sandbox profile (the real one is host state).
TEMPLATE = b"""# Exact root-owned official Codex 0.161.0 sandbox binary; Ubuntu child restrictions retained.
profile codex_bwrap /usr/local/lib/codex-packages/0.161.0/codex-resources/bwrap flags=(attach_disconnected) {
  allow pix /** -> &codex_bwrap//&codex_unpriv_bwrap,
  include if exists <local/codex-bwrap-userns-restrict>
}
profile codex_unpriv_bwrap flags=(attach_disconnected) {
  allow pix /** -> &codex_unpriv_bwrap,
  audit deny capability,
  include if exists <local/codex_unpriv_bwrap>
}
"""


class ProtectedInstall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'source'
        self.source.mkdir()
        (self.source / 'binary').write_text('new package')
        self.dest = root / '0.164.0'
        self.profile = root / 'new-profile'
        self.old_package = root / 'old-package'
        self.old_package.write_text('existing package')
        self.old_profile = root / 'old-profile'
        self.old_profile.write_text('existing profile')
        for name, value in [('DEST', self.dest), ('NEW_PROFILE', self.profile)]:
            context = patch.object(installer, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.output = io.StringIO()
        context = redirect_stdout(self.output)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def assert_old_untouched(self):
        self.assertEqual(self.old_package.read_text(), 'existing package')
        self.assertEqual(self.old_profile.read_text(), 'existing profile')

    def test_success_preserves_existing_installation(self):
        with patch.object(installer.subprocess, 'run') as run, patch.object(installer, 'verify_sandbox') as verify:
            installer.publish_and_verify(self.source, 'new profile')
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(self.profile.read_text(), 'new profile')
        verify.assert_called_once()
        self.assertEqual(run.call_count, 1)
        self.assertIn('-a', run.call_args.args[0])
        self.assert_old_untouched()

    def test_failed_sandbox_removes_only_new_installation(self):
        with patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, \
             patch.object(installer, 'verify_sandbox', side_effect=RuntimeError('sandbox failed')):
            with self.assertRaisesRegex(RuntimeError, 'sandbox failed'):
                installer.publish_and_verify(self.source, 'new profile')
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.profile.exists())
        self.assertIn('-R', run.call_args.args[0])
        self.assert_old_untouched()

    def test_failed_unload_preserves_files_and_reports_it(self):
        with patch.object(installer.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 0),
                         subprocess.TimeoutExpired('parser', 60)]), \
             patch.object(installer, 'verify_sandbox', side_effect=RuntimeError('sandbox failed')):
            with self.assertRaisesRegex(RuntimeError, 'sandbox failed'):
                installer.publish_and_verify(self.source, 'new profile')
        self.assertTrue(self.dest.exists())
        self.assertTrue(self.profile.exists())
        self.assertIn('ROLLBACK INCOMPLETE', self.output.getvalue())
        self.assert_old_untouched()

    def test_existing_profile_is_never_overwritten_or_deleted(self):
        self.profile.write_text('another installation')
        with patch.object(installer.subprocess, 'run') as run:
            with self.assertRaises(FileExistsError):
                installer.publish_and_verify(self.source, 'new profile')
        run.assert_not_called()
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.profile.read_text(), 'another installation')
        self.assert_old_untouched()


class ReleaseMaintenance(unittest.TestCase):
    def test_rejects_unstable_versions_and_paths(self):
        for value in ('latest', '../0.164.0', '0.164.0-beta', '0.164', '0.164.0;reboot'):
            with self.assertRaises(ValueError):
                installer.stable_version(value)

    def test_profile_names_and_paths_follow_selected_version(self):
        old = installer.VERSION
        try:
            installer.select_version('0.200.1')
            with patch.object(installer, 'PROFILE_HASH', hashlib.sha256(TEMPLATE).hexdigest()):
                text = installer.profile_text(TEMPLATE)
            self.assertIn('/0.200.1/codex-resources/bwrap', text)
            self.assertIn('profile codex_bwrap_0_200_1 ', text)
            self.assertIn('&codex_bwrap_0_200_1//&codex_unpriv_bwrap_0_200_1', text)
            self.assertIn('include if exists <local/codex_unpriv_bwrap>', text)
            self.assertIn('audit deny capability,', text)
            self.assertNotIn('/0.161.0/', text)
        finally:
            installer.select_version(old)

    def test_latest_resolves_once_to_exact_version_and_digest(self):
        package = {'name': '@openai/codex', 'version': '0.164.0-linux-x64',
                   'dist': {'tarball': 'https://registry.npmjs.org/@openai/codex/-/codex-0.164.0-linux-x64.tgz',
                            'integrity': 'sha512-' + installer.INTEGRITY}}
        with patch.object(installer, 'metadata', side_effect=[{'name': '@openai/codex', 'version': '0.164.0'}, package]) as fetch:
            self.assertEqual(installer.resolve_release('latest'), ('0.164.0', installer.INTEGRITY))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args[0], '/@openai%2fcodex/0.164.0-linux-x64')

    def test_rejects_unexpected_download_source(self):
        package = {'name': '@openai/codex', 'version': '0.164.0-linux-x64',
                   'dist': {'tarball': 'https://unrelated.example/package.tgz',
                            'integrity': 'sha512-' + installer.INTEGRITY}}
        with patch.object(installer, 'metadata', return_value=package):
            with self.assertRaisesRegex(RuntimeError, 'Unexpected package URL'):
                installer.resolve_release('0.164.0')

    def test_template_changes_require_review(self):
        with self.assertRaisesRegex(RuntimeError, 'profile changed'):
            installer.profile_text(b'altered template')


UNIT_TEXT = """[Service]
Type=oneshot
WorkingDirectory=/home/codex/workspaces
ExecStart=/usr/local/lib/codex-packages/0.161.0/bin/codex app-server daemon start
ExecStop=/usr/local/lib/codex-packages/0.161.0/bin/codex app-server daemon stop
"""


class Activation(unittest.TestCase):
    """activate/rollback switch the CLI links, daemon package link and boot service together."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'codex-packages'
        for version in ('0.161.0', '0.164.0'):
            (self.base / version / 'bin').mkdir(parents=True)
        self.old = self.base / '0.161.0'
        self.new = self.base / '0.164.0'
        self.state = {'links': {path: str(self.old / suffix) if suffix else str(self.old)
                                for path, suffix in installer.LINKS.items()},
                      'unit': UNIT_TEXT.replace('/usr/local/lib/codex-packages', str(self.base)),
                      'dropins': []}
        self.running = '0.161.0'
        self.calls = []
        old_version = installer.VERSION
        self.addCleanup(installer.select_version, old_version)
        patches = [patch.object(installer, 'BASE', self.base),
                   patch.object(installer, 'ACTIVATIONS', self.base / 'activations'),
                   patch.object(installer, 'PACKAGE_BIN', installer.package_bin_pattern(self.base)),
                   patch.object(installer, 'read_state', side_effect=lambda: json.loads(json.dumps(self.state))),
                   patch.object(installer, 'apply_state', side_effect=self.fake_apply),
                   patch.object(installer, 'user_systemctl', side_effect=lambda *a: self.calls.append(('systemctl',) + a)),
                   patch.object(installer, 'daemon_version', side_effect=self.fake_version),
                   patch.object(installer, 'research_busy', return_value=''),
                   patch.object(installer, 'verify_prepared'),
                   patch.object(installer, 'protected_tree'),
                   redirect_stdout(io.StringIO())]
        for context in patches:
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        installer.select_version('0.164.0')

    def fake_apply(self, links, unit):
        self.calls.append(('apply',))
        self.state['links'] = dict(links)
        self.state['unit'] = unit
        self.running = Path(links[installer.DAEMON_LINK]).name

    def fake_version(self):
        package = Path(self.state['links'][installer.DAEMON_LINK])
        return {'cliVersion': Path(self.state['links'][installer.CLI_LINK]).parent.parent.name,
                'appServerVersion': self.running, 'managedCodexVersion': self.running,
                'managedCodexPath': str(package / 'bin/codex')}

    def records(self):
        return sorted((self.base / 'activations').glob('*.json'))

    def test_activate_switches_everything_in_order_and_records_previous_state(self):
        installer.activate()
        self.assertEqual(self.calls, [('systemctl', 'stop', installer.UNIT_NAME), ('apply',),
                                      ('systemctl', 'daemon-reload'), ('systemctl', 'start', installer.UNIT_NAME)])
        for path, target in self.state['links'].items():
            self.assertTrue(target.startswith(str(self.new)), path)
        self.assertIn(f'ExecStart={self.new}/bin/codex app-server daemon start', self.state['unit'])
        self.assertIn(f'ExecStop={self.new}/bin/codex app-server daemon stop', self.state['unit'])
        [record] = self.records()
        saved = json.loads(record.read_text())
        self.assertEqual((saved['from_version'], saved['to_version']), ('0.161.0', '0.164.0'))
        self.assertEqual(saved['links'][installer.DAEMON_LINK], str(self.old))
        self.assertIn('0.161.0/bin/codex app-server daemon start', saved['unit'])
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)

    def test_activate_refuses_during_a_research_run_and_changes_nothing(self):
        with patch.object(installer, 'research_busy', return_value='wo-research.service active'):
            with self.assertRaisesRegex(RuntimeError, 'research run is active'):
                installer.activate()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.records(), [])

    def test_activate_refuses_a_daemon_on_a_self_updating_package(self):
        self.state['links'][installer.DAEMON_LINK] = '/home/codex/.codex/packages/app-server-daemon/releases/0.161.0-x86_64-unknown-linux-musl'
        with self.assertRaisesRegex(RuntimeError, 'not on a protected package'):
            installer.activate()
        self.assertEqual(self.calls, [])

    def test_activate_refuses_boot_service_overrides(self):
        self.state['dropins'] = ['/home/codex/.config/systemd/user/codex-start.service.d/override.conf']
        with self.assertRaisesRegex(RuntimeError, 'overrides'):
            installer.activate()
        self.assertEqual(self.calls, [])

    def test_failed_activation_restores_the_previous_version(self):
        def broken_start(*args):
            self.calls.append(('systemctl',) + args)
            if args == ('start', installer.UNIT_NAME) and self.running == '0.164.0':
                self.running = 'none'
        with patch.object(installer, 'user_systemctl', side_effect=broken_start):
            with self.assertRaisesRegex(RuntimeError, 'disagree'):
                installer.activate()
        for target in self.state['links'].values():
            self.assertTrue(target.startswith(str(self.old)))
        self.assertIn(f'ExecStart={self.old}/bin/codex', self.state['unit'])
        self.assertEqual(self.running, '0.161.0')

    def test_rollback_restores_the_last_activation_and_ignores_rollback_records(self):
        installer.activate()
        installer.rollback()
        installer.rollback()
        for target in self.state['links'].values():
            self.assertTrue(target.startswith(str(self.old)))
        self.assertIn(f'ExecStart={self.old}/bin/codex', self.state['unit'])
        names = [record.name for record in self.records()]
        self.assertEqual(sum(name.endswith('-rollback.json') for name in names), 1)

    def test_rollback_without_an_activation_refuses(self):
        with self.assertRaisesRegex(RuntimeError, 'No recorded activation'):
            installer.rollback()
        self.assertEqual(self.calls, [])

    def test_confirmation_requires_every_version_and_the_protected_path(self):
        self.fake_apply({path: target.replace('0.161.0', '0.164.0') for path, target in self.state['links'].items()},
                        self.state['unit'])
        installer.confirm_running('0.164.0', self.new)
        self.running = '0.161.0'
        with self.assertRaisesRegex(RuntimeError, 'disagree'):
            installer.confirm_running('0.164.0', self.new)
        self.running = '0.164.0'
        with patch.object(installer, 'daemon_version', return_value={
                'cliVersion': '0.164.0', 'appServerVersion': '0.164.0', 'managedCodexVersion': '0.164.0',
                'managedCodexPath': '/home/codex/.codex/packages/app-server-daemon/releases/x/bin/codex'}):
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                installer.confirm_running('0.164.0', self.new)

    def test_unit_rewrite_requires_exactly_the_start_and_stop_commands(self):
        with self.assertRaisesRegex(RuntimeError, 'boot service layout'):
            installer.rewrite_unit('[Service]\nExecStart=/usr/bin/true\n', self.new)


class CodexSideHelper(unittest.TestCase):
    """The helper that runs as codex swaps links in place and never writes through a regular file."""

    def run_helper(self, request):
        return subprocess.run(['python3', '-I', '-c', installer.CODEX_SIDE], input=json.dumps(request),
                              text=True, capture_output=True)

    def test_read_then_apply_replaces_links_and_unit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            link = root / 'codex'
            link.symlink_to('/usr/local/lib/codex-packages/0.161.0/bin/codex')
            unit = root / 'codex-start.service'
            unit.write_text('old unit\n')
            unit.chmod(0o640)
            read = self.run_helper({'op': 'read', 'links': [str(link), str(root / 'absent')], 'unit': str(unit)})
            self.assertEqual(read.returncode, 0, read.stderr)
            state = json.loads(read.stdout)
            self.assertEqual(state['links'], {str(link): '/usr/local/lib/codex-packages/0.161.0/bin/codex',
                                              str(root / 'absent'): None})
            self.assertEqual((state['unit'], state['dropins']), ('old unit\n', []))
            applied = self.run_helper({'op': 'apply', 'unit': str(unit), 'unit_text': 'new unit\n',
                                       'links': {str(link): '/usr/local/lib/codex-packages/0.164.0/bin/codex'}})
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(str(link.readlink()), '/usr/local/lib/codex-packages/0.164.0/bin/codex')
            self.assertEqual(unit.read_text(), 'new unit\n')
            self.assertEqual(unit.stat().st_mode & 0o777, 0o640)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ['codex', 'codex-start.service'])

    def test_apply_refuses_to_replace_a_regular_file(self):
        with tempfile.TemporaryDirectory() as temp:
            regular = Path(temp) / 'codex'
            regular.write_text('not a link')
            unit = Path(temp) / 'unit'
            unit.write_text('unit\n')
            result = self.run_helper({'op': 'apply', 'unit': str(unit), 'unit_text': 'changed\n',
                                      'links': {str(regular): '/elsewhere'}})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(regular.read_text(), 'not a link')
            self.assertEqual(unit.read_text(), 'unit\n')


class Entry(unittest.TestCase):
    def test_deploy_installs_the_released_commit_not_the_working_tree(self):
        deploy = (Path(__file__).parent.parent / 'ops/vps/deploy.sh').read_text()
        self.assertIn('"tools/research_schemas.py", "tools/codex_maintenance.py"]', deploy)
        self.assertIn('install -m 755 -o root -g root "$TRUSTED/tools/codex_maintenance.py" /usr/local/sbin/codex-maintenance', deploy)
        self.assertNotIn('codex-maintenance/install-protected-codex.py', deploy)

    def test_every_command_requires_an_administrator(self):
        for argv in (['check'], ['status'], ['activate', '0.164.0'], ['rollback']):
            with patch.object(installer.os, 'geteuid', return_value=1000), \
                 patch.object(installer.sys, 'argv', ['codex-maintenance', *argv]):
                with self.assertRaisesRegex(SystemExit, 'sudo'):
                    installer.main()


if __name__ == '__main__':
    unittest.main()
