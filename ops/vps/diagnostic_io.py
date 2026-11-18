"""Bounded observation I/O. No shell, inherited secrets, symlinks or special files."""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time

ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
       'HOME': '/nonexistent', 'SYSTEMD_PAGER': '', 'SYSTEMD_COLORS': '0', 'TZ': 'UTC'}


def read_file(path, limit=65536):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute fixed path required')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('not a bounded regular file')
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(fd, min(8192, limit + 1 - len(data)))
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
            raise ValueError('file exceeds limit')
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def command(argv, timeout=5, limit=262144):
    """Never return partial stdout or stderr after a failed/bounded-out command."""
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=ENV, cwd='/', start_new_session=True)
        deadline = time.monotonic() + timeout
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {'state': 'timeout'}
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), min(8192, limit + 1 - len(data)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        data.extend(chunk)
                        if len(data) > limit:
                            return {'state': 'output-limit'}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {'state': 'timeout'}
            if process.wait(timeout=remaining):
                return {'state': 'unavailable'}
        return {'state': 'observed', 'output': data.decode('utf-8', errors='replace')}
    except subprocess.TimeoutExpired:
        return {'state': 'timeout'}
    except OSError:
        return {'state': 'unavailable'}
    finally:
        if process is not None:
            # Kill descendants too, including when the parent exited before its pipe closed.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
