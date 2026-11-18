#!/usr/bin/env python3
"""/deploy (.claude/commands/deploy.md): the owner's routine deploy from any local Claude Code session.

Claude Code runs this when the owner types /deploy, before Claude reads anything; Claude cannot start it
(`disable-model-invocation`). It refuses at once where a deploy cannot work: no VPS key or no SSH route
(a cloud session, or a machine without the key), or a deploy still running. Otherwise it starts
`ops/vps/deploy.sh --local-approved` in a session of its own, so the 2-minute command timeout can
never stop it half-way, waits up to WAIT seconds and prints the log. A finished log ends with
"deploy exit status: N". It takes no arguments: --migrate, --rev and the timer switches stay explicit
`! ops/vps/deploy.sh --local-approved …` commands. Runs on the Macs' system Python (3.9).
"""
from __future__ import annotations

import os
import fcntl
from pathlib import Path
import shlex
import subprocess
import sys
import time
import tempfile

REPO = Path(__file__).resolve().parents[2]
COMMAND = ['ops/vps/deploy.sh', '--local-approved']
HOST = 'ops@100.64.0.10'
WAIT = 100  # under the 120 s that Claude Code gives the command before moving it to the background
DONE = 'deploy exit status: '


def ssh_reachable(key: str) -> bool:
    return subprocess.run(['ssh', '-i', key, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                           HOST, 'true'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def running(lock: Path) -> str:
    """The log of a deploy still running, or ''."""
    try:
        pid, log = lock.read_text().split(maxsplit=1)
        os.kill(int(pid), 0)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return ''
    return log.strip()


def run(command=COMMAND, key: str | None = None, logs: Path = REPO / 'data/logs', wait: int = WAIT,
        reachable=ssh_reachable) -> tuple[int, str]:
    key = key or os.environ.get('SSH_KEY') or str(Path.home() / '.ssh/work_ops_vps')
    if not os.access(key, os.R_OK):
        return 2, (f'deploy refused: no VPS key at {key} on this machine, so nothing ran. Deploy from a local '
                   'session on a Mac that has the key; cloud sessions never deploy.')
    if not reachable(key):
        return 2, (f'deploy refused: {HOST} did not answer over SSH with {key}, so nothing ran. '
                   'Run ops/vps-unlock --check for key/session diagnostics; check Tailscale and SSH access. '
                   'A failed connection alone does not establish that the key is locked.')
    logs.mkdir(parents=True, exist_ok=True)
    lock = logs / 'deploy.pid'  # human-readable metadata; flock, not a PID lookup, owns exclusion
    # Never unlink the flock file: another starter must open the same inode. The child inherits
    # this descriptor, so exclusion survives the caller returning or being interrupted.
    descriptor = os.open(logs / 'deploy.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as claim:
        try:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            other = running(lock)
            return 2, ('deploy refused: a deploy is already running; ' +
                       (f'its log is {other}.' if other else f'its log is being recorded in {logs}.'))
        # Retain compatibility with a still-running deployment started by the older PID-only runner.
        other = running(lock)
        if other:
            return 2, f'deploy refused: a deploy is already running; its log is {other}.'
        fd, name = tempfile.mkstemp(prefix=time.strftime('deploy-%Y%m%dT%H%M%SZ-', time.gmtime()),
                                    suffix='.log', dir=logs)
        log = Path(name)
        # Publish the log before spawning. The child writes its own PID before running the command;
        # the parent must not recreate metadata after a very short child has already removed it.
        lock.write_text(f'{os.getpid()} {log}\n')
        remove = shlex.quote('rm -f -- ' + shlex.quote(str(lock)))
        script = (f'trap {remove} EXIT; '
                  f'printf \'%s %s\\n\' "$$" {shlex.quote(str(log))} > {shlex.quote(str(lock))}; '
                  f'{shlex.join(command)}; status=$?; echo "{DONE}$status"; exit $status')
        try:
            with os.fdopen(fd, 'w') as out:
                proc = subprocess.Popen(['/bin/sh', '-c', script], cwd=REPO, stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                        pass_fds=(claim.fileno(),))
        except OSError:
            lock.unlink(missing_ok=True)
            raise
    deadline = time.monotonic() + wait
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    text = log.read_text()
    if proc.poll() is None:
        return 0, (f'{text}\nstill running (pid {proc.pid}) in its own session; its log is {log} and ends with '
                   f'"{DONE}<code>" when it finishes.')
    return proc.returncode, text


def main(argv: list[str]) -> int:
    if argv:
        print('deploy refused: /deploy takes no arguments, so nothing ran. For --migrate, --rev or the timers, '
              'The owner runs ! ops/vps/deploy.sh --local-approved <option> himself.')
        return 0
    code, text = run()
    print(text.rstrip())
    print(f'(deploy_session exit {code})')
    return 0  # Claude reads the text; a failing exit would hide it


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
