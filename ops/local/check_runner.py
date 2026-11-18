"""Local check diagnostics; never changes permissions or contacts the hosted service."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time


_printing = threading.Lock()


def stop_group(child):
    """Reap the leader and stop surviving descendants even if the leader exits first."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    deadline = time.monotonic() + 5
    while True:
        child.poll()
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            # macOS can report EPERM while a group is exiting. Reap and probe again;
            # only ESRCH proves it is gone, and a persistent denial must still fail.
            if time.monotonic() >= deadline:
                raise
            time.sleep(.02)
            continue
        if time.monotonic() >= deadline:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        time.sleep(.02)
    child.wait()


def preflight():
    try:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
    except OSError as error:
        print(f'FAIL  loopback preflight: {error}', flush=True)
        print('No suites ran. Rerun with local-server permissions; do not loosen global sandbox settings.', flush=True)
        return 1
    print('ok    loopback preflight (temporary local port closed)', flush=True)
    return 0


def run(name, log, command, *, interval=30, cancelled=None):
    if cancelled is not None and cancelled.is_set():
        return 130
    started = time.monotonic()
    heartbeat = started + interval
    print(f'RUN   {name} — log: {log}', flush=True)
    # Logs can contain development-record details. Keep them private even with a permissive umask.
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        try:
            child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            output.write(f'{error}\n')
            code = 1
        else:
            try:
                while True:
                    if cancelled is not None and cancelled.is_set():
                        code = 130
                        break
                    try:
                        code = child.wait(timeout=min(interval, .1) if cancelled is not None else interval)
                        if code < 0:
                            code = 128 - code
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= heartbeat:
                            print(f'WAIT  {name} ({time.monotonic() - started:.0f}s) — log: {log}', flush=True)
                            heartbeat = time.monotonic() + interval
            finally:
                if (child.poll() is None or sys.exc_info()[0] is not None
                        or (cancelled is not None and cancelled.is_set()) or code in (130, 143)):
                    stop_group(child)
    with _printing:
        print(f'{"ok   " if code == 0 else "FAIL "} {name} ({time.monotonic() - started:.1f}s)', flush=True)
        if code:
            with Path(log).open(errors='replace') as output:
                print(''.join(deque(output, maxlen=40)), end='', flush=True)
            print(f'Full log: {log}', flush=True)
    return code


def batch(stages, *, jobs):
    """Bounded independent stages; each retains its own process group, log and exit status."""
    if jobs not in (1, 2):
        raise ValueError('browser jobs must be 1 or 2')
    cancelled = threading.Event()
    pool = ThreadPoolExecutor(max_workers=jobs)
    futures = []
    codes = []
    try:
        for stage in stages:
            futures.append(pool.submit(run, stage['name'], stage['log'], stage['command'], cancelled=cancelled))
        for future in as_completed(futures):
            code = future.result()
            codes.append(code)
            if code in (130, 143):
                cancelled.set()
                return code
        return int(any(codes))
    finally:
        # Includes Ctrl-C, SIGTERM and a failure to start/log a stage. No queued stage starts
        # once cancelled; running children terminate their whole group before the batch exits.
        cancelled.set()
        pool.shutdown(wait=True, cancel_futures=True)


def node_dependencies(project):
    """Check installed versions against the committed graph, including transitive packages."""
    project = Path(project)
    try:
        packages = json.loads((project / 'package-lock.json').read_text())['packages']
        problems = []
        for name, expected in packages.items():
            if not name.startswith('node_modules/'):
                continue
            package = project / name / 'package.json'
            if not package.is_file():
                if not expected.get('optional'):
                    problems.append(f'{name}: missing')
                continue
            actual = json.loads(package.read_text()).get('version')
            if actual != expected.get('version'):
                problems.append(f'{name}: installed {actual}, locked {expected.get("version")}')
    except (OSError, ValueError, KeyError) as error:
        problems = [f'cannot read dependency metadata: {error}']
    if problems:
        print(f'FAIL  {project} dependencies differ from package-lock.json: ' + '; '.join(problems[:8]), flush=True)
        print(f'      Run npm ci --prefix {project}; no dependency upgrade is required.', flush=True)
        return 1
    return 0


def browser_jobs(profile):
    if profile == 'vps':
        return 1
    try:
        memory = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    except (ValueError, OSError, AttributeError):
        memory = None
        if sys.platform == 'darwin':
            try:
                memory = int(subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.memsize']))
            except (OSError, ValueError, subprocess.CalledProcessError):
                pass
    return 1 if memory is None or memory < 8 * 1024**3 or (os.cpu_count() or 1) < 2 else 2


def interrupted(signum, frame):
    raise SystemExit(128 + signum)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    if sys.argv[1:] == ['preflight']:
        sys.exit(preflight())
    if len(sys.argv) == 3 and sys.argv[1] == 'node-deps':
        sys.exit(node_dependencies(sys.argv[2]))
    if len(sys.argv) == 3 and sys.argv[1] == 'browser-jobs':
        print(browser_jobs(sys.argv[2]))
        sys.exit(0)
    if len(sys.argv) >= 6 and sys.argv[1] == 'queue':
        descriptor = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, 'a') as output:
            output.write(json.dumps({'name': sys.argv[3], 'log': sys.argv[4], 'command': sys.argv[5:]}) + '\n')
        sys.exit(0)
    if len(sys.argv) == 4 and sys.argv[1] == 'batch':
        content = Path(sys.argv[3]).read_text()
        stages = json.loads(content) if content.lstrip().startswith('[') else [json.loads(line) for line in content.splitlines()]
        sys.exit(batch(stages, jobs=int(sys.argv[2])))
    if len(sys.argv) < 5 or sys.argv[1] != 'run':
        sys.exit('Usage: check_runner.py preflight | node-deps PROJECT | run NAME LOG COMMAND [ARG ...] | queue MANIFEST NAME LOG COMMAND [ARG ...] | batch JOBS MANIFEST')
    sys.exit(run(sys.argv[2], Path(sys.argv[3]), sys.argv[4:]))
