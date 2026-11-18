#!/usr/bin/python3 -I
"""Attended, separately approved key authorization. Never invoked by a restricted key."""
from __future__ import annotations

import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import sys
import time


class Refused(ValueError):
    pass


def validate(request):
    if not isinstance(request, dict) or set(request) != {'device', 'source', 'records', 'diagnostics'}:
        raise Refused('invalid request fields')
    if not isinstance(request['device'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', request['device']):
        raise Refused('invalid device name')
    try:
        address = ipaddress.IPv4Address(request['source'])
    except (ValueError, TypeError):
        raise Refused('one tailnet IPv4 address required') from None
    if address not in ipaddress.IPv4Network('100.64.0.0/10'):
        raise Refused('source must be one tailnet device address')
    for capability in ('records', 'diagnostics'):
        key = request[capability]
        if not isinstance(key, str) or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]{68}', key):
            raise Refused('one plain Ed25519 public key required')
        try:
            blob = base64.b64decode(key.split()[1], validate=True)
        except ValueError:
            raise Refused('invalid public key encoding') from None
        if len(blob) != 51 or blob[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
            raise Refused('invalid Ed25519 public key')
    if request['records'] == request['diagnostics']:
        raise Refused('capabilities need different keys')
    return request


def entries(request):
    validate(request)
    result = []
    for capability, helper, as_user in (
            ('records', 'wo_agent_gateway.py', '-u workops '),
            ('diagnostics', 'wo_diagnostics.py', '')):
        forced = ('sudo -n ' + as_user + '--preserve-env=SSH_ORIGINAL_COMMAND '
                  '/usr/bin/python3 -I /usr/local/lib/work-ops/' + helper)
        result.append((f'from="{request["source"]}",restrict,command="{forced}" '
                       f'{request[capability]} work-ops-{request["device"]}-{capability}\n').encode())
    return result


def edit_keys(original, request, action):
    if action not in {'add', 'remove'} or len(original) > 1024 * 1024:
        raise Refused('invalid action or oversized authorization file')
    if original and not original.endswith(b'\n'):
        raise Refused('authorization file must end with a newline; preserve it for operator review')
    wanted = entries(request)
    lines = original.splitlines(keepends=True)
    for line, capability in zip(wanted, ('records', 'diagnostics')):
        blob = request[capability].split()[1].encode()
        existing = [item for item in lines if blob in item.split()]
        if existing and existing != [line]:
            raise Refused('key already has different or duplicate authorization; no change made')
    if action == 'add':
        return original + b''.join(line for line in wanted if line not in lines)
    return b''.join(line for line in lines if line not in wanted)


def open_directory(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Refused('absolute authorization directory required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def update_file(path, request, action, *, owner, group, lock_path=None):
    validate(request)
    lock_path = lock_path or path.with_name('.work-ops-agent-access.lock')
    lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    directory = None
    temporary = '.authorized_keys.' + secrets.token_hex(12)
    try:
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise Refused('unsafe authorization lock')
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Refused('another authorization update is running')
                time.sleep(.05)
        directory = open_directory(path.parent)
        info = os.fstat(directory)
        if info.st_uid not in {0, owner} or info.st_mode & 0o022:
            raise Refused('unsafe authorization directory')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner or before.st_nlink != 1
                    or before.st_mode & 0o077 or before.st_size > 1024 * 1024):
                raise Refused('unsafe authorization file')
            original = stream.read(1024 * 1024 + 1)
        updated = edit_keys(original, request, action)
        if updated == original:
            return False
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), owner, group)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (current.st_ino, current.st_mtime_ns, current.st_size) != (before.st_ino, before.st_mtime_ns, before.st_size):
            raise Refused('authorization file changed concurrently; retry after inspection')
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        return True
    finally:
        if directory is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)
        os.close(lock)


def main():
    try:
        if os.geteuid() != 0 or sys.argv[1:] not in (['add'], ['remove']):
            raise Refused('attended root invocation requires add or remove')
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise Refused('request too large')
        request = validate(json.loads(raw))
        if sys.argv[1] == 'add' and os.environ.get('SSH_CONNECTION', '').split()[:1] != [request['source']]:
            raise Refused('source must match this attended tailnet connection')
        ops = pwd.getpwnam('ops')
        if ops.pw_dir != '/home/ops':
            raise Refused('unexpected administrator home')
        changed = update_file(Path('/home/ops/.ssh/authorized_keys'), request, sys.argv[1],
                              owner=ops.pw_uid, group=ops.pw_gid,
                              lock_path=Path('/run/work-ops-agent-access.lock'))
        print(json.dumps({'version': 1, 'changed': changed, 'action': sys.argv[1]}))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        # Reasons contain fixed policy text, never key material or arbitrary request values.
        print('agent-access: ' + (str(exc) if isinstance(exc, Refused) else 'invalid or unavailable request'), file=sys.stderr)
        return 64


if __name__ == '__main__':
    sys.exit(main())
