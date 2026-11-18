#!/usr/bin/python3 -I
"""Explicit, service-scoped decryption verification; not a diagnostics capability."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SERVICES = {'wo-scan.service', 'wo-email.service'}
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'SYSTEMD_PAGER': ''}


def check(service, run=subprocess.run, exists=lambda path: path.is_file() and not path.is_symlink()):
    if service not in SERVICES:
        raise ValueError('only scan/email credential checks are permitted')
    # systemctl show renders this property as [unprintable] on the deployed systemd.
    # cat includes drop-ins in load order. Interpret only Service credential declarations.
    result = run(['/usr/bin/systemctl', 'cat', service, '--no-pager'],
                 capture_output=True, text=True, timeout=5, check=True, env=ENV)
    names, section = [], ''
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        # Fail closed rather than interpreting a continuation as a new declaration.
        if line.endswith('\\'):
            raise ValueError('continued unit syntax needs attended inspection')
        if line.startswith('[') and line.endswith(']'):
            section = line
        key, equals, value = line.partition('=')
        if section == '[Service]' and equals and key.strip() == 'LoadCredentialEncrypted':
            value = value.strip()
            if not value:
                names = []
            else:
                names.append(value)
    if len(names) > 32 or any(not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', name) for name in names):
        raise ValueError('unexpected credential declaration; inspect before testing')
    names = sorted(set(name for name in names if exists(Path('/etc/credstore.encrypted') / name)))
    if not names:
        raise ValueError('no declared encrypted credentials installed; decryption unverified')
    args = ['/usr/bin/systemd-run', '--wait', '--quiet', '--collect', '--pipe']
    for prop in ('User=workops', 'NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes',
                 'PrivateNetwork=yes', 'PrivateTmp=yes', 'PrivateDevices=yes',
                 'InaccessiblePaths=/srv/work-ops /home /root', 'CapabilityBoundingSet=',
                 'RuntimeMaxSec=10', 'MemoryMax=64M', 'TasksMax=8',
                 *('LoadCredentialEncrypted=' + name for name in names)):
        args += ['-p', prop]
    # No credential content is printed or placed in arguments/environment: only file sizes counted.
    args += ['--', '/usr/bin/python3', '-I', '-c',
             'import os,pathlib; p=pathlib.Path(os.environ["CREDENTIALS_DIRECTORY"]); '
             'print(sum(f.is_file() and f.stat().st_size>0 for f in p.iterdir()))']
    result = run(args, capture_output=True, text=True, timeout=20, check=True, env=ENV)
    count = int(result.stdout.strip())
    if count != len(names):
        raise ValueError('not every declared stored credential decrypted')
    return {'service': service, 'stored': len(names), 'decrypted': count}


def main():
    try:
        if os.geteuid() != 0 or len(sys.argv) != 2:
            raise ValueError('attended root invocation requires one service')
        print(json.dumps(check(sys.argv[1])))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError):
        print('credential verification failed or unavailable; no credential values reported', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
