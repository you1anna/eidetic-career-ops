#!/usr/bin/python3 -I
"""Root-owned read-only forced SSH command for a bot acting for the owner (for example Hermes).

Install /usr/local/lib/work-ops/wo_read_gateway.py beside wo_agent_gateway.py, owned root:root
(0644), and bind one dedicated key to

    sudo -n -u workops --preserve-env=SSH_ORIGINAL_COMMAND \\
        /usr/bin/python3 -I /usr/local/lib/work-ops/wo_read_gateway.py --caller <name>

Requests use the record gateway's own validation and execution, limited to READ and given an
empty stdin. This is a separate file so that a rollback which replaces the record gateway cannot
widen the key: an older gateway without `command_name` makes every request fail closed.
"""
from __future__ import annotations

import fcntl
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

# Deployment installs both helpers in this trusted directory. Never import the VPS checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from wo_agent_gateway import Refused, command_name, execute, validate
    COMPATIBLE = True
except ImportError:
    COMPATIBLE = False

    class Refused(ValueError):
        pass

# Reads that change no record and take no input. `state` (the whole database) is left out.
READ = frozenset({
    'brief', 'find', 'show', 'approach', 'quality', 'import-report', 'fetch-log',
    'decision list', 'decision show', 'question list', 'question show',
})
CALLER = re.compile(r'[a-z][a-z0-9-]{0,31}')
LOCK = '/srv/work-ops/run/agent-read.lock'
WAIT_SECONDS = 20


class Busy(Exception):
    pass


def caller_of(argv: list[str]) -> str:
    if len(argv) != 2 or argv[0] != '--caller' or not CALLER.fullmatch(argv[1]):
        raise Refused('the read key must name exactly one caller')
    return argv[1]


def acquire(path: str, wait: float) -> int:
    """One bot read at a time, so a polling bot cannot stack whole-state builds beside the site."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise Refused('unsafe read lock')
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy() from None
                time.sleep(0.1)
    except BaseException:
        os.close(fd)
        raise


def journal(message: str) -> None:
    import syslog
    syslog.openlog('wo-read', 0, syslog.LOG_USER)
    syslog.syslog(syslog.LOG_INFO, message)


def main(argv=None, original=None, *, run=subprocess.run, lock=LOCK, wait=WAIT_SECONDS, note=journal) -> int:
    argv = sys.argv[1:] if argv is None else argv
    original = os.environ.get('SSH_ORIGINAL_COMMAND', '') if original is None else original
    started = time.monotonic()
    caller, command, status, held = 'unknown', 'refused', 64, None
    try:
        if not COMPATIBLE:
            raise Refused('the installed record gateway does not support read-only access')
        caller = caller_of(argv)
        args, upload = validate(original)
        # Only names from the gateway's own RULES reach the journal, never request arguments.
        command = command_name(args)
        if upload is not None or command not in READ:
            raise Refused('command is outside the read-only allowlist')
        held = acquire(lock, wait)
        # No read takes input: the client's stdin is never read.
        status = execute(original, io.BytesIO(b''), run=run)
    except Busy:
        status = 75
        print('wo-read: busy: another read is running; retry shortly', file=sys.stderr)
    except (Refused, subprocess.TimeoutExpired, OSError) as exc:
        status = 64
        print('wo-read: refused: ' + (str(exc) if isinstance(exc, Refused) else 'unavailable'),
              file=sys.stderr)
    finally:
        if held is not None:
            os.close(held)
        note(f'caller={caller} command={command.replace(" ", "-")} status={status} '
             f'ms={round((time.monotonic() - started) * 1000)}')
    return status


if __name__ == '__main__':
    sys.exit(main())
