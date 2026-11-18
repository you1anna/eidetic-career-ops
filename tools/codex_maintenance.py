#!/usr/bin/python3 -I
"""Attended Codex maintenance on the VPS: check, prepare, verify, status, activation-plan, activate, rollback.

deploy.sh installs this file from the released commit as /usr/local/sbin/codex-maintenance (root-owned);
the runbook is ops/OPERATIONS.md "Codex updates". Only an administrator runs it. Nothing runs it on a
schedule. prepare/verify never touch the active CLI or daemon; activate and rollback switch the CLI
links, the daemon's package link and its boot service together, as the codex user, and restart the
daemon. The daemon's package link stays on a root-owned package, which keeps Codex's own updater off.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse

VERSION = '0.164.0'
INTEGRITY = 'XLAoaWsGOT2khzl+P4FYDPTkzAr9W0VFXFaVkqlFdkM0soa1gH/4S0wSD57Ek3HHwkirAEGz2/Mwehseh+l5Mg=='
PROFILE_HASH = '7258984b3ae93315d676f9c16ace8875b8723a1912579ba22078e070b4de504c'
BASE = Path('/usr/local/lib/codex-packages')
DEST = BASE / VERSION
OLD_PROFILE = Path('/etc/apparmor.d/codex-bwrap-userns-restrict')
NEW_PROFILE = Path('/etc/apparmor.d/codex-bwrap-0.164.0')
WORKSPACE = '/home/codex/workspaces'
REGISTRY = 'https://registry.npmjs.org'
PROFILE_NAMES = ('codex_bwrap_0_164_0', 'codex_unpriv_bwrap_0_164_0')

CODEX_USER = 'codex'
CODEX_HOME = Path('/home/codex')
CLI_LINK = str(CODEX_HOME / '.local/bin/codex')
DAEMON_LINK = str(CODEX_HOME / '.codex/packages/app-server-daemon/current')
# Each link and the package path it points to; the daemon link names the package directory itself.
LINKS = {CLI_LINK: 'bin/codex', str(CODEX_HOME / '.local/bin/codex-code-mode-host'): 'bin/codex-code-mode-host',
         DAEMON_LINK: ''}
UNIT_NAME = 'codex-start.service'
UNIT = CODEX_HOME / '.config/systemd/user' / UNIT_NAME
ACTIVATIONS = BASE / 'activations'


def package_bin_pattern(base):
    return re.compile(re.escape(str(base)) + r'/\d+\.\d+\.\d+/bin/codex(?=\s|$)')


PACKAGE_BIN = package_bin_pattern(BASE)

# Runs as codex: reads or replaces its own links and boot unit, so root never writes in codex's home.
CODEX_SIDE = r'''
import json, os, sys
from pathlib import Path
request = json.load(sys.stdin)
unit = Path(request['unit'])
if request['op'] == 'read':
    links = {path: os.readlink(path) if os.path.islink(path) else None for path in request['links']}
    dropins = sorted(str(path) for path in unit.with_name(unit.name + '.d').glob('*.conf'))
    print(json.dumps({'links': links, 'unit': unit.read_text(), 'dropins': dropins}))
    sys.exit(0)
links = request['links']
for path in links:
    if os.path.lexists(path) and not os.path.islink(path):
        sys.exit(path + ' is not a symlink; refusing to replace it')
mode = unit.stat().st_mode & 0o777
for path, target in links.items():
    temporary = path + '.codex-maintenance'
    if os.path.lexists(temporary):
        os.unlink(temporary)
    if target is None:
        if os.path.lexists(path):
            os.unlink(path)
        continue
    os.symlink(target, temporary)
    os.replace(temporary, path)
temporary = unit.with_name(unit.name + '.codex-maintenance')
temporary.unlink(missing_ok=True)
with open(temporary, 'x') as output:
    output.write(request['unit_text'])
os.chmod(temporary, mode)
os.replace(temporary, unit)
'''


def stable_version(value):
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('Use a stable numeric version such as 0.164.0')
    return value


def metadata(path):
    with urllib.request.urlopen(REGISTRY + path, timeout=30) as response:
        if urllib.parse.urlparse(response.url).hostname != 'registry.npmjs.org':
            raise RuntimeError('Unexpected registry redirect')
        data = response.read(2 * 1024**2 + 1)
        if len(data) > 2 * 1024**2:
            raise RuntimeError('Release metadata too large')
        return json.loads(data)


def select_version(value):
    global VERSION, DEST, NEW_PROFILE, PROFILE_NAMES
    VERSION = stable_version(value)
    DEST = BASE / VERSION
    NEW_PROFILE = Path('/etc/apparmor.d') / ('codex-bwrap-' + VERSION)
    suffix = VERSION.replace('.', '_')
    PROFILE_NAMES = ('codex_bwrap_' + suffix, 'codex_unpriv_bwrap_' + suffix)


def resolve_release(requested):
    if requested == 'latest':
        latest = metadata('/@openai%2fcodex/latest')
        if latest.get('name') != '@openai/codex':
            raise RuntimeError('Unexpected package name')
        requested = stable_version(latest['version'])
    else:
        stable_version(requested)
    package = metadata('/@openai%2fcodex/' + requested + '-linux-x64')
    if package.get('name') != '@openai/codex' or package.get('version') != requested + '-linux-x64':
        raise RuntimeError('Unexpected platform package')
    expected_url = f'{REGISTRY}/@openai/codex/-/codex-{requested}-linux-x64.tgz'
    integrity = package['dist']['integrity']
    if package['dist']['tarball'] != expected_url or not integrity.startswith('sha512-'):
        raise RuntimeError('Unexpected package URL or integrity algorithm')
    digest = integrity.removeprefix('sha512-')
    if len(base64.b64decode(digest, validate=True)) != 64:
        raise RuntimeError('Invalid SHA-512 digest')
    return requested, digest


def protected_tree(path):
    for entry in [path, *path.rglob('*')]:
        info = entry.lstat()
        if entry.is_symlink() or info.st_uid != 0 or info.st_mode & 0o7022:
            raise RuntimeError(f'Unprotected package entry: {entry}')


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def record_installation():
    record = {'version': VERSION, 'verified_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'integrity': 'sha512-' + INTEGRITY, 'profile_sha256': file_hash(NEW_PROFILE),
              'files': {str(path.relative_to(DEST)): file_hash(path)
                        for path in DEST.rglob('*') if path.is_file()}}
    with (DEST / 'maintenance-manifest.json').open('x') as out:
        json.dump(record, out, indent=2)
        out.write('\n')
    (DEST / 'maintenance-manifest.json').chmod(0o644)


def verify_prepared():
    protected_tree(DEST)
    profile_info = NEW_PROFILE.lstat()
    if NEW_PROFILE.is_symlink() or profile_info.st_uid != 0 or profile_info.st_mode & 0o022:
        raise RuntimeError('Profile is not protected')
    record = json.loads((DEST / 'maintenance-manifest.json').read_text())
    actual = {str(path.relative_to(DEST)): file_hash(path) for path in DEST.rglob('*')
              if path.is_file() and path != DEST / 'maintenance-manifest.json'}
    if record['version'] != VERSION or record['files'] != actual or record['profile_sha256'] != file_hash(NEW_PROFILE):
        raise RuntimeError('Prepared installation changed; refusing to reuse it')
    loaded = Path('/sys/kernel/security/apparmor/profiles').read_text()
    if not all(name + ' (enforce)' in loaded for name in PROFILE_NAMES):
        raise RuntimeError('Prepared sandbox profiles are not loaded in enforce mode')
    verify_sandbox()


def profile_text(original):
    if hashlib.sha256(original).hexdigest() != PROFILE_HASH:
        raise RuntimeError('Existing sandbox profile changed; review before proceeding')
    text = original.decode().replace('/0.161.0/', '/' + VERSION + '/')
    text = text.replace('official Codex 0.161.0', 'official Codex ' + VERSION)
    # Keep the existing optional local includes; rename only profile names and transitions.
    lines = []
    for line in text.splitlines():
        if 'include if exists' not in line:
            line = re.sub(r'\bcodex_bwrap\b', PROFILE_NAMES[0], line)
            line = re.sub(r'\bcodex_unpriv_bwrap\b', PROFILE_NAMES[1], line)
        lines.append(line)
    return '\n'.join(lines) + '\n'


def prepare():
    for path in [BASE, *BASE.parents, OLD_PROFILE, OLD_PROFILE.parent]:
        info = path.lstat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f'Expected a protected root-owned path: {path}')
    if DEST.exists() or DEST.is_symlink() or NEW_PROFILE.exists() or NEW_PROFILE.is_symlink():
        verify_prepared()
        print(f'Codex {VERSION} was already prepared; integrity and sandbox reverified.')
        return
    loaded = Path('/sys/kernel/security/apparmor/profiles').read_text()
    if any(name in loaded for name in PROFILE_NAMES):
        raise RuntimeError('New profile names are already loaded; refusing to modify them')
    profile = profile_text(OLD_PROFILE.read_bytes())
    with tempfile.TemporaryDirectory(prefix='.codex-upgrade-', dir=BASE) as directory:
        stage = Path(directory)
        candidate = stage / 'profile'
        candidate.write_text(profile)
        subprocess.run(['/usr/sbin/apparmor_parser', '-Q', '-K', '-j', '1', str(candidate)], check=True, timeout=60)
        archive = stage / 'package.tgz'
        if shutil.disk_usage(BASE).free < 3 * 1024**3:
            raise RuntimeError('Less than 3 GiB free; no package installation attempted')
        url = f'https://registry.npmjs.org/@openai/codex/-/codex-{VERSION}-linux-x64.tgz'
        subprocess.run(['/usr/bin/curl', '--disable', '--fail', '--silent', '--show-error',
                        '--proto', '=https', '--max-time', '300', '--max-filesize', '536870912',
                        '--output', str(archive), url], check=True, timeout=330)
        with archive.open('rb') as stream:
            digest = base64.b64encode(hashlib.file_digest(stream, 'sha512').digest()).decode()
        if digest != INTEGRITY:
            raise RuntimeError('Package integrity mismatch')
        with tarfile.open(archive) as contents:
            members = contents.getmembers()
            if len(members) > 10000 or sum(m.size for m in members) > 2 * 1024**3:
                raise RuntimeError('Unexpected package size')
            if any(not (m.isfile() or m.isdir()) for m in members):
                raise RuntimeError('Package contains links or special files')
            contents.extractall(stage / 'extracted', filter='data')
        source = stage / 'extracted/package/vendor/x86_64-unknown-linux-musl'
        manifest = json.loads((source / 'codex-package.json').read_text())
        expected = {'layoutVersion': 1, 'version': VERSION, 'target': 'x86_64-unknown-linux-musl',
                    'entrypoint': 'bin/codex', 'resourcesDir': 'codex-resources', 'pathDir': 'codex-path'}
        if any(manifest.get(k) != v for k, v in expected.items()):
            raise RuntimeError('Unexpected package layout')
        for path in [source, *source.rglob('*')]:
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
            os.chown(path, 0, 0)
        publish_and_verify(source, profile)
    print(f'Protected Codex {VERSION} installed and sandbox verified.')
    print('CLI links and running daemon are unchanged. Switch to it with: sudo codex-maintenance activate ' + VERSION)


def publish_and_verify(source, profile):
    installed = False
    profile_created = False
    load_attempted = False
    try:
        source.rename(DEST)
        installed = True
        with NEW_PROFILE.open('x') as output:
            profile_created = True
            output.write(profile)
        NEW_PROFILE.chmod(0o644)
        load_attempted = True
        subprocess.run(['/usr/sbin/apparmor_parser', '-a', '-K', '-j', '1', str(NEW_PROFILE)], check=True, timeout=60)
        verify_sandbox()
        record_installation()
    except BaseException:
        # Never remove existing profiles or packages. If kernel cleanup fails,
        # preserve the new protected files and report the exact remaining state.
        can_remove = True
        if load_attempted:
            try:
                result = subprocess.run(['/usr/sbin/apparmor_parser', '-R', '-K', '-j', '1', str(NEW_PROFILE)], timeout=60)
                if result.returncode:
                    loaded = Path('/sys/kernel/security/apparmor/profiles').read_text()
                    can_remove = not any(name in loaded for name in PROFILE_NAMES)
            except BaseException:
                can_remove = False
        if can_remove:
            if profile_created:
                NEW_PROFILE.unlink()
            if installed:
                shutil.rmtree(DEST)
            print('Failed installation rolled back; active CLI and daemon were not changed.')
        else:
            print(f'ROLLBACK INCOMPLETE: retained {DEST} and {NEW_PROFILE}; administrator inspection required.')
        raise


def verify_sandbox():
    command = ['/usr/sbin/runuser', '-u', 'codex', '--', str(DEST / 'bin/codex')]
    result = subprocess.run(command + ['--version'], check=True, text=True, capture_output=True, timeout=30)
    if result.stdout.strip() != 'codex-cli ' + VERSION:
        raise RuntimeError('Installed CLI version mismatch')
    print(result.stdout.strip())
    probe = """from pathlib import Path
import uuid
name = '.codex-boundary-' + uuid.uuid4().hex
allowed = Path.cwd() / name
denied = Path('/home/codex') / name
try:
    allowed.write_text('workspace write test')
    try:
        denied.write_text('home write test')
    except OSError as e:
        assert e.errno in (1, 13, 30), e
    else:
        raise RuntimeError('Sandbox unexpectedly allowed a home write')
finally:
    allowed.unlink(missing_ok=True)
    if denied.exists():
        denied.unlink()
print('Sandbox boundary checks passed')
"""
    subprocess.run(command + ['sandbox', '-P', ':workspace', '-C', WORKSPACE, '--',
                               '/usr/bin/python3', '-c', probe], check=True, timeout=60)


def run_as_codex(args, stdin=None, timeout=120):
    uid = pwd.getpwnam(CODEX_USER).pw_uid
    command = ['/usr/sbin/runuser', '-u', CODEX_USER, '--', '/usr/bin/env', '-i', f'HOME={CODEX_HOME}',
               f'USER={CODEX_USER}', f'XDG_RUNTIME_DIR=/run/user/{uid}', 'PATH=/usr/local/bin:/usr/bin:/bin', *args]
    result = subprocess.run(command, input=stdin, text=True, capture_output=True, timeout=timeout, cwd='/')
    if result.returncode:
        raise RuntimeError(f'{" ".join(args[:3])} failed as codex: {result.stderr.strip() or result.stdout.strip()}')
    return result


def read_state():
    request = {'op': 'read', 'links': list(LINKS), 'unit': str(UNIT)}
    return json.loads(run_as_codex(['/usr/bin/python3', '-I', '-c', CODEX_SIDE], json.dumps(request)).stdout)


def apply_state(links, unit_text):
    request = {'op': 'apply', 'links': links, 'unit': str(UNIT), 'unit_text': unit_text}
    run_as_codex(['/usr/bin/python3', '-I', '-c', CODEX_SIDE], json.dumps(request))


def user_systemctl(*args):
    run_as_codex(['/usr/bin/systemctl', '--user', *args], timeout=180)


def daemon_version():
    return json.loads(run_as_codex([CLI_LINK, 'app-server', 'daemon', 'version'], timeout=60).stdout)


def research_busy():
    result = subprocess.run(['/usr/bin/systemctl', 'list-units', '--plain', '--no-legend',
                             '--state=active,activating,deactivating', 'wo-research*.service'],
                            text=True, capture_output=True, check=True, timeout=30)
    return result.stdout.strip()


def package_version(target):
    """The version a link selects, only when it is a root-owned package directly under BASE."""
    path = Path(target or '')
    if path.parent != BASE or not re.fullmatch(r'\d+\.\d+\.\d+', path.name):
        raise RuntimeError(f'The Codex daemon is not on a protected package ({target}); review before switching')
    return path.name


def rewrite_unit(text, target):
    lines, changed = [], 0
    for line in text.splitlines():
        if line.startswith(('ExecStart=', 'ExecStop=')):
            line, count = PACKAGE_BIN.subn(str(target / 'bin/codex'), line)
            changed += count
        lines.append(line)
    if changed != 2:
        raise RuntimeError('Unexpected boot service layout; manual review required')
    return '\n'.join(lines) + '\n'


def planned_links(target):
    return {path: str(target / suffix) if suffix else str(target) for path, suffix in LINKS.items()}


def switch(links, unit_text):
    user_systemctl('stop', UNIT_NAME)
    apply_state(links, unit_text)
    user_systemctl('daemon-reload')
    user_systemctl('start', UNIT_NAME)


def confirm_running(version, target):
    info = daemon_version()
    seen = {key: info.get(key) for key in ('cliVersion', 'appServerVersion', 'managedCodexVersion')}
    if set(seen.values()) != {version}:
        raise RuntimeError(f'Codex versions disagree (expected {version}): {seen}')
    if not os.path.realpath(info.get('managedCodexPath') or '/').startswith(os.path.realpath(target) + '/'):
        raise RuntimeError(f'The daemon runs from outside {target}: {info.get("managedCodexPath")}')


def save_record(state, from_version, to_version, kind=''):
    ACTIVATIONS.mkdir(mode=0o700, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + f'{time.time_ns() % 10**9:09d}'
    path = ACTIVATIONS / f'{stamp}-{from_version}-to-{to_version}{kind}.json'
    record = {'from_version': from_version, 'to_version': to_version, 'at_utc': stamp[:16],
              'links': state['links'], 'unit': state['unit']}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as out:
        json.dump(record, out, indent=2)
        out.write('\n')


def ready_state():
    busy = research_busy()
    if busy:
        raise RuntimeError('A research run is active; try again when it finishes:\n' + busy)
    state = read_state()
    if state['dropins']:
        raise RuntimeError('Boot service has overrides; review effective service configuration first')
    return state


def activate():
    verify_prepared()
    before = ready_state()
    previous = package_version(before['links'][DAEMON_LINK])
    links, unit_text = planned_links(DEST), rewrite_unit(before['unit'], DEST)
    if before['links'] == links and before['unit'] == unit_text:
        confirm_running(VERSION, DEST)
        print(f'Codex {VERSION} is already active.')
        return
    save_record(before, previous, VERSION)
    print(f'Switching Codex {previous} -> {VERSION}. Open Codex sessions on this server are interrupted.')
    try:
        switch(links, unit_text)
        confirm_running(VERSION, DEST)
    except BaseException as error:
        print(f'Activation failed: {error}\nRestoring Codex {previous}.')
        switch(before['links'], before['unit'])
        confirm_running(previous, BASE / previous)
        print(f'Codex {previous} restored.')
        raise
    print(f'Codex {VERSION} is active: CLI, daemon and boot service agree. Undo: sudo codex-maintenance rollback')


def rollback():
    records = sorted(path for path in ACTIVATIONS.glob('*.json') if not path.name.endswith('-rollback.json')) \
        if ACTIVATIONS.is_dir() else []
    if not records:
        raise RuntimeError('No recorded activation to roll back')
    record = json.loads(records[-1].read_text())
    previous = stable_version(record['from_version'])
    target = BASE / previous
    if package_version(record['links'][DAEMON_LINK]) != previous:
        raise RuntimeError('Activation record is inconsistent; review ' + str(records[-1]))
    protected_tree(target)
    current = ready_state()
    if current['links'] == record['links'] and current['unit'] == record['unit']:
        confirm_running(previous, target)
        print(f'Codex {previous} is already active.')
        return
    save_record(current, Path(current['links'][DAEMON_LINK] or 'unknown').name, previous, '-rollback')
    print(f'Restoring Codex {previous} from {records[-1].name}. Open Codex sessions on this server are interrupted.')
    switch(record['links'], record['unit'])
    confirm_running(previous, target)
    print(f'Codex {previous} is active again.')


def status():
    state = read_state()
    try:
        running = daemon_version()
    except (RuntimeError, ValueError) as error:
        running = {'error': str(error)}
    activations = sorted(path.name for path in ACTIVATIONS.glob('*.json')) if ACTIVATIONS.is_dir() else []
    print(json.dumps({'links': state['links'],
                      'boot_service_commands': [line for line in state['unit'].splitlines()
                                                if line.startswith(('ExecStart=', 'ExecStop='))],
                      'service_drop_ins': state['dropins'],
                      'prepared_versions': sorted(path.parent.name for path in BASE.glob('*/maintenance-manifest.json')),
                      'running': running, 'recent_activations': activations[-5:]}, indent=2))


def activation_plan():
    state = read_state()
    if state['dropins']:
        raise RuntimeError('Boot service has overrides; review effective service configuration first')
    unit_text = rewrite_unit(state['unit'], DEST)
    print(json.dumps({'target_version': VERSION, 'actions_performed': False,
                      'current_daemon_package': state['links'][DAEMON_LINK],
                      'link_changes': {path: {'before': state['links'][path], 'after': target}
                                       for path, target in planned_links(DEST).items()},
                      'service_command_changes': [line for line in unit_text.splitlines()
                                                  if line.startswith(('ExecStart=', 'ExecStop='))],
                      'apply_with': f'sudo codex-maintenance activate {VERSION}'}, indent=2))


def main():
    global INTEGRITY, WORKSPACE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='check',
                        choices=['check', 'status', 'prepare', 'verify', 'activation-plan', 'activate', 'rollback'])
    parser.add_argument('version', nargs='?')
    parser.add_argument('--workspace', default=WORKSPACE, help='Existing codex-writable directory for sandbox tests')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo: codex-maintenance needs administrator rights (the codex account has none).')
    if args.command == 'status':
        status()
        return
    if args.command == 'check':
        version, _ = resolve_release('latest')
        print('Latest stable Codex: ' + version)
        status()
        return
    if args.command in ('verify', 'activation-plan', 'activate') and not args.version:
        parser.error('Specify the exact prepared version')
    if args.command == 'activation-plan':
        select_version(args.version)
        activation_plan()
        return
    if args.command == 'prepare':
        selected, INTEGRITY = resolve_release(args.version or 'latest')
        select_version(selected)
    elif args.command != 'rollback':
        select_version(args.version)
    WORKSPACE = str(Path(args.workspace).resolve(strict=True))
    # Lock the protected directory itself, without creating a persistent lock file.
    descriptor = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            prepare()
        elif args.command == 'activate':
            activate()
        elif args.command == 'rollback':
            rollback()
        else:
            verify_prepared()
            print('Prepared package integrity and sandbox verified: ' + VERSION)
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    main()
