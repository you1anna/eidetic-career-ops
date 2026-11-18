"""SSH transport for explicit restricted capabilities; no administrative fallback."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path


class Refused(ValueError):
    pass


def validate_route(route):
    if (not isinstance(route, dict) or route.get('restricted') is not True
            or set(route) != {'host', 'key', 'host_key_alias', 'restricted'}
            or any(not isinstance(route[k], str) or not route[k]
                   for k in ('host', 'key', 'host_key_alias'))
            or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*', route['host'])
            or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]*', route['host_key_alias'])
            or not Path(route['key']).is_absolute() or '\x00' in route['key']):
        raise Refused('invalid restricted route')
    return route


def load_route(name, *, legacy=None):
    path = Path.home() / '.config/work-ops' / name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if legacy is not None and not path.is_symlink():
            return legacy
        raise Refused(f'{name} is not configured; activate restricted access first')
    except OSError:
        raise Refused(f'cannot safely open {name}') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 4096):
            raise Refused(f'{name} must be a private, owned regular file under 4 KiB')
        return validate_route(json.loads(os.read(fd, 4097)))
    except (ValueError, OSError):
        raise Refused(f'invalid {name}; refusing administrative fallback') from None
    finally:
        os.close(fd)


def ssh_command(route, remote):
    args = ['ssh', '-i', route['key'], '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15']
    if route['restricted']:
        validate_route(route)
        # A different IdentityFile does NOT change an already authenticated ControlMaster.
        # Ignore user config as well: it may add admin identities, proxies or local commands.
        args += ['-F', '/dev/null', '-o', 'ControlPath=none', '-o', 'ControlMaster=no',
                 '-o', 'ControlPersist=no', '-o', 'IdentityAgent=none', '-o', 'IdentityFile=none',
                 '-o', 'HostKeyAlias=' + route['host_key_alias'], '-o', 'ForwardAgent=no',
                 '-o', 'ClearAllForwardings=yes', '-o', 'ServerAliveInterval=15',
                 '-o', 'ServerAliveCountMax=2', '-T']
    return [*args, route['host'], remote]
