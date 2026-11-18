"""Explicit attended activation. No files or server changes in plan mode."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

from ssh_route import Refused, ssh_command

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ops/vps'))
from agent_access import validate as validate_request, Refused as ServerRefused

HOST = 'ops@100.64.0.10'
ALIAS = '100.64.0.10'
CAPABILITIES = {'records': ('workops_live', 'live-gateway.json'),
                'diagnostics': ('workops_diagnostics', 'diagnostics-gateway.json')}


def private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise Refused('expected an owned private regular file: ' + path.name)


def directory(path):
    if path.is_symlink():
        raise Refused('refusing a symlink directory')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Refused('unsafe local directory')


@contextlib.contextmanager
def locked(home):
    for path in (home / '.ssh', home / '.config', home / '.config/work-ops'):
        directory(path)
    fd = os.open(home / '.config/work-ops/access.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise Refused('unsafe local activation lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('another activation is running') from None
        yield
    finally:
        os.close(fd)


def make_request(home, device, source, *, create):
    request = {'device': device, 'source': source}
    routes = {}
    for capability, (key_name, _) in CAPABILITIES.items():
        key = home / '.ssh' / key_name
        if not key.exists() and not key.is_symlink():
            if not create:
                raise Refused('dedicated key missing; nothing revoked')
            if key.with_suffix('.pub').exists() or key.with_suffix('.pub').is_symlink():
                raise Refused('orphaned public key; inspect before generating')
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                            'work-ops-' + device + '-' + capability, '-f', str(key)], check=True,
                           stdin=subprocess.DEVNULL, timeout=15)
        private_file(key)
        result = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', str(key)], check=True,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        request[capability] = ' '.join(result.stdout.split()[:2])
        routes[capability] = dict(host=HOST, key=str(key), host_key_alias=ALIAS, restricted=True)
    try:
        validate_request(request)
    except ServerRefused as exc:
        raise Refused(str(exc)) from None
    return request, routes


def remote_update(action, request):
    # Admin authority is explicit here; no code upload or shell interpolation of request values.
    admin = {'host': HOST, 'key': str(Path.home() / '.ssh/work_ops_vps'), 'restricted': False}
    remote = ('sudo -n --preserve-env=SSH_CONNECTION /usr/bin/python3 -I '
              '/usr/local/lib/work-ops/agent_access.py ' + action)
    cmd = ssh_command(admin, remote)
    cmd[1:1] = ['-o', 'ControlPath=none', '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes']
    result = subprocess.run(cmd, input=json.dumps(request).encode(), capture_output=True, timeout=30)
    if result.returncode:
        raise Refused('server authorization failed; inspect using administrator access, then retry or revoke')
    try:
        receipt = json.loads(result.stdout)
        if receipt.get('version') != 1 or receipt.get('action') != action:
            raise ValueError()
    except (ValueError, AttributeError):
        raise Refused('authorization result unknown; inspect before proceeding') from None


def verify_routes(routes, run=subprocess.run):
    def invoke(capability, request):
        return run(ssh_command(routes[capability], request), stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=45)
    if invoke('records', 'wo --actor agent show --help').returncode != 0:
        raise Refused('record gateway positive check failed')
    result = invoke('diagnostics', 'wo-diagnostics status')
    if result.returncode:
        raise Refused('diagnostics positive check failed')
    try:
        value = json.loads(result.stdout)
        if value.get('version') != 1 or value.get('action') != 'status':
            raise ValueError()
        artifacts = value['report']['artifacts']
        for name, path in {'records': 'ops/vps/wo_agent_gateway.py', 'diagnostics': 'ops/vps/wo_diagnostics.py',
                           'diagnostics_io': 'ops/vps/diagnostic_io.py', 'policy': 'tools/ops_policy.py'}.items():
            if artifacts[name]['sha256'] != hashlib.sha256((ROOT / path).read_bytes()).hexdigest():
                raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Refused('deployed capabilities do not match this reviewed checkout') from None
    for capability in CAPABILITIES:
        other = 'wo-diagnostics status' if capability == 'records' else 'wo --actor agent show --help'
        for request in ('id', 'wo --actor owner state', other):
            if invoke(capability, request).returncode != 64:
                raise Refused('negative capability check failed: ' + capability)
        # A forced command alone does not prohibit TCP forwarding; the key also needs restrict.
        cmd = ssh_command(routes[capability], '')[:-1]
        cmd[1:1] = ['-W', '127.0.0.1:8769']
        forwarded = run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        if forwarded.returncode != 255 or b'administratively prohibited' not in forwarded.stderr:
            raise Refused('forwarding restriction could not be verified: ' + capability)


def publish(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def activate(home, device, source, *, remote=remote_update, verify=verify_routes):
    with locked(home):
        request, routes = make_request(home, device, source, create=True)
        pending = []
        for capability, (_, filename) in CAPABILITIES.items():
            path = home / '.config/work-ops' / filename
            if path.exists() or path.is_symlink():
                private_file(path)
                try:
                    if json.loads(path.read_bytes()) != routes[capability]:
                        raise ValueError()
                except ValueError:
                    raise Refused('existing route differs; refusing to replace ' + filename) from None
            else:
                pending.append((path, (json.dumps(routes[capability], indent=2) + '\n').encode()))
        remote('add', request)
        verify(routes)
        published = []
        try:
            for path, data in pending:
                publish(path, data)
                published.append(path)
        except BaseException:
            for path in published:
                path.unlink()
            raise
        return request


def revoke(home, device, source, *, remote=remote_update):
    with locked(home):
        request, _ = make_request(home, device, source, create=False)
        remote('remove', request)
        # Keep routes: revocation must not silently restore the legacy administrator fallback.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'activate', 'verify', 'revoke'])
    parser.add_argument('--device', required=True, help='stable device label, e.g. air or mini')
    parser.add_argument('--source', required=True, help="this Mac's exact Tailscale IPv4 address")
    parser.add_argument('--approved', action='store_true', help='explicit authority for activation or revocation')
    args = parser.parse_args(argv)
    try:
        # Validate non-secret inputs without creating files or keys in plan mode.
        import ipaddress
        import re
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', args.device) or ipaddress.IPv4Address(args.source) not in ipaddress.IPv4Network('100.64.0.0/10'):
            raise Refused('use a device label and exact tailnet IPv4 address')
        if args.action == 'plan':
            print('After approved deployment: authorize two dedicated, source-limited forced-command keys;')
            print('verify record/diagnostics access, shell denial, cross-capability denial, forwarding denial')
            print('and installed hashes; then publish private local routes. Existing administrator, offsite')
            print('and VPS Codex keys are preserved. No files, keys or server settings changed by this plan.')
            return 0
        if args.action in {'activate', 'revoke'} and not args.approved:
            raise Refused('this security change requires explicit authority and --approved')
        home = Path.home()
        if args.action == 'activate':
            activate(home, args.device, args.source)
            print('Restricted record and diagnostics routes verified and active on this Mac.')
        elif args.action == 'revoke':
            revoke(home, args.device, args.source)
            print('Only this device’s managed keys revoked. Routes retained to fail closed; admin access preserved.')
        else:
            _, routes = make_request(home, args.device, args.source, create=False)
            verify_routes(routes)
            print('Capabilities and installed hashes verified; no configuration changed.')
        return 0
    except (ValueError, OSError, subprocess.SubprocessError):
        print('agent-access: incomplete; local routes are not switched before successful verification. '
              'Keys may have been prepared/authorized. Inspect with admin access; retry the same '
              'activation or explicitly revoke. No automatic administrative fallback was enabled.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
