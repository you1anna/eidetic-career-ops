#!/usr/bin/python3 -I
"""Forced SSH diagnostics capability: bounded metadata, no raw logs or record reads."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import sys

# Deployment installs all three modules in this trusted directory. Never import the VPS checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnostic_io import command, read_file
from ops_policy import DIAGNOSTIC_UNITS, LOG_UNITS, diagnostic_request

DATA = Path('/srv/work-ops/data')
ARTIFACTS = {
    'records': '/usr/local/lib/work-ops/wo_agent_gateway.py',
    'read': '/usr/local/lib/work-ops/wo_read_gateway.py',
    'diagnostics': '/usr/local/lib/work-ops/wo_diagnostics.py',
    'diagnostics_io': '/usr/local/lib/work-ops/diagnostic_io.py',
    'policy': '/usr/local/lib/work-ops/ops_policy.py',
    'ssh': '/etc/ssh/sshd_config.d/20-work-ops.conf',
    'loopback': '/etc/work-ops/loopback.nft',
    'research': '/etc/systemd/system/wo-research-codex@.service',
}
NUMBERS = ('ExecMainStatus', 'NRestarts', 'MemoryCurrent', 'MemoryPeak', 'MemoryMax',
           'TasksCurrent')
TIMES = ('NextElapseUSecRealtime', 'LastTriggerUSec', 'ExecMainStartTimestamp', 'ExecMainExitTimestamp')
ENUMS = {
    'ActiveState': {'active', 'inactive', 'failed', 'activating', 'deactivating', 'reloading'},
    'SubState': {'running', 'dead', 'exited', 'waiting', 'listening', 'failed', 'start', 'stop'},
    'Result': {'success', 'exit-code', 'signal', 'core-dump', 'timeout', 'oom-kill',
               'resources', 'start-limit-hit', 'protocol', 'watchdog', 'condition'},
    'LoadState': {'loaded', 'not-found', 'error', 'masked', 'bad-setting'},
    'UnitFileState': {'enabled', 'disabled', 'static', 'masked', 'indirect', 'generated'},
}
ERRORS = {'OperationalError', 'IntegrityError', 'DatabaseError', 'RuntimeError', 'ValueError',
          'TypeError', 'KeyError', 'OSError', 'PermissionError', 'TimeoutError', 'MemoryError'}


class Refused(ValueError):
    pass


def validate(original):
    if not isinstance(original, str) or len(original) > 256 or '\x00' in original:
        raise Refused('invalid diagnostics request')
    try:
        tokens = shlex.split(original)
    except ValueError:
        raise Refused('invalid diagnostics request') from None
    if tokens[:1] == ['wo-diagnostics']:
        try:
            return diagnostic_request(tokens[1:])
        except ValueError:
            pass
    raise Refused('only status or logs for an allowlisted service is permitted')


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value if parsed.tzinfo else None
    except ValueError:
        return None


def project_backup(value):
    return {'at': timestamp(value.get('at')), 'result': value.get('result')
            if value.get('result') in {'passed', 'defects', 'failed', 'ok'} else None}


def project_crash(value):
    error = str(value.get('error', '')).split(':', 1)[0]
    recent = value.get('recent', [])
    return {'at': timestamp(value.get('at')), 'error_type': error if error in ERRORS else 'other',
            'recent': [v for v in recent[-10:] if timestamp(v)] if isinstance(recent, list) else []}


def project_journal(text):
    events, unclassified = [], 0
    for line in text.splitlines()[-50:]:
        try:
            row = json.loads(line)
            raw_at = row.get('__REALTIME_TIMESTAMP', '')
            if not isinstance(raw_at, str) or not re.fullmatch(r'\d{16}', raw_at):
                raise ValueError()
            at = dt.datetime.fromtimestamp(int(raw_at) / 1e6, dt.timezone.utc).isoformat()
            message = row.get('MESSAGE')
            if not isinstance(message, str):
                raise ValueError()
            # Classify; never copy a journal message or arbitrary captured text into the output.
            match = re.fullmatch(r"[a-z0-9@_.-]+: Failed with result '([a-z-]+)'\.", message)
            if match and match[1] in ENUMS['Result']:
                events.append({'at': at, 'event': 'failed', 'result': match[1]})
            elif re.fullmatch(r'[a-z0-9@_.-]+: Deactivated successfully\.', message):
                events.append({'at': at, 'event': 'completed'})
            else:
                unclassified += 1
        except (ValueError, TypeError, AttributeError, OverflowError):
            unclassified += 1
    return {'events': events, 'unclassified': unclassified,
            'limit': 50, 'window_hours': 24, 'raw_messages_omitted': True}


def projected_file(path, project, read=read_file):
    try:
        value = json.loads(read(path))
        if not isinstance(value, dict):
            raise ValueError()
        return {'state': 'observed', **project(value)}
    except (OSError, ValueError, TypeError):
        return {'state': 'unavailable'}


def units_projection(text):
    result = {}
    for block in text.split('\n\n'):
        row = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        unit = row.get('Id')
        if unit not in DIAGNOSTIC_UNITS:
            continue
        clean = {k: row.get(k) if row.get(k) in allowed else None for k, allowed in ENUMS.items()}
        clean.update({k: int(row[k]) if re.fullmatch(r'\d{1,20}', row.get(k, '')) else None
                      for k in NUMBERS})
        for key in TIMES:
            try:
                clean[key] = dt.datetime.strptime(row.get(key, ''), '%a %Y-%m-%d %H:%M:%S UTC').replace(tzinfo=dt.timezone.utc).isoformat()
            except ValueError:
                clean[key] = None
        result[unit] = clean
    return {unit: result.get(unit, {'state': 'unavailable'}) for unit in DIAGNOSTIC_UNITS}


MEMORY_EVENTS = ('high', 'max', 'oom', 'oom_kill')


def service_memory(read=read_file):
    """Each running service's cgroup now: its peak counts reclaimable file cache, so anon/file split
    the current use and events count limit pressure and OOM kills since it started. Finished oneshot
    runs have no cgroup ('absent'); their own receipts record the same figures. Mirrors the run-side
    reader in tools/ingest.py, because this installed helper never imports the checkout."""
    def number(raw):
        text = raw.decode('ascii', 'replace').strip()
        return int(text) if re.fullmatch(r'\d{1,20}', text) else None

    def pairs(raw):
        return {k: number(v.encode()) for k, v in (line.split(' ', 1) for line in
                raw.decode('ascii', 'replace').splitlines() if ' ' in line)}

    def optional(path):
        try:
            return read(path)
        except (OSError, ValueError):
            return b''

    result = {}
    for unit in LOG_UNITS:
        group = Path('/sys/fs/cgroup/system.slice') / unit
        try:
            stat = pairs(read(group / 'memory.stat'))
        except FileNotFoundError:
            result[unit] = {'state': 'absent'}
            continue
        except (OSError, ValueError):
            result[unit] = {'state': 'unavailable'}
            continue
        events = pairs(optional(group / 'memory.events'))
        result[unit] = {'state': 'observed', 'peak_bytes': number(optional(group / 'memory.peak')),
                        'anon_bytes': stat.get('anon'), 'file_bytes': stat.get('file'),
                        'events': {k: events[k] for k in MEMORY_EVENTS if events.get(k) is not None}}
    return result


def status(run=command, read=read_file):
    report = {}
    properties = ','.join(('Id', *ENUMS, *NUMBERS, *TIMES))
    observed = run(['/usr/bin/systemctl', 'show', *DIAGNOSTIC_UNITS, '--property=' + properties])
    report['units'] = {'state': observed['state']}
    if observed['state'] == 'observed':
        report['units']['values'] = units_projection(observed['output'])
    observed = run(['/usr/bin/curl', '--noproxy', '*', '--max-time', '3', '--fail', '--silent',
                    'http://127.0.0.1:8769/api/health'])
    report['origin'] = {'state': observed['state']}
    if observed['state'] == 'observed':
        try:
            value = json.loads(observed['output'])
            revision = value.get('revision')
            report['origin'].update(healthy=value.get('status') == 'ok' and value.get('mode') == 'database',
                revision=revision if isinstance(revision, str) and re.fullmatch(r'[a-f0-9]{40}', revision) else None)
        except (ValueError, AttributeError):
            report['origin'] = {'state': 'invalid'}
    try:
        usage = os.statvfs('/srv')
        report['disk'] = {'state': 'observed', 'available_bytes': usage.f_bavail * usage.f_frsize,
                          'total_bytes': usage.f_blocks * usage.f_frsize, 'free_inodes': usage.f_favail}
    except OSError:
        report['disk'] = {'state': 'unavailable'}
    try:
        mem = read(Path('/proc/meminfo')).decode()
        report['memory_kib'] = {key: int(value) for key, value in
            re.findall(r'^(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d+) kB$', mem, re.M)}
    except (OSError, ValueError):
        report['memory_kib'] = {'state': 'unavailable'}
    report['service_memory'] = service_memory(read)
    report['backup'] = projected_file(DATA / 'backups/last-backup.json', project_backup, read)
    report['crashes'] = {unit: projected_file(DATA / 'crashes' / (unit + '.json'), project_crash, read)
                         for unit in LOG_UNITS}
    report['offhost_restore'] = {'state': 'unknown', 'reason': 'evidence remains on the Mac'}
    report['artifacts'] = {}
    for name, path in ARTIFACTS.items():
        try:
            report['artifacts'][name] = {'state': 'observed', 'sha256': hashlib.sha256(read(Path(path))).hexdigest()}
        except (OSError, ValueError):
            report['artifacts'][name] = {'state': 'unavailable'}
    observed = run(['/usr/sbin/sshd', '-T'])
    report['ssh'] = {'state': observed['state']}
    if observed['state'] == 'observed':
        settings = dict(line.split(' ', 1) for line in observed['output'].splitlines() if ' ' in line)
        for key in ('permitrootlogin', 'passwordauthentication', 'kbdinteractiveauthentication',
                    'allowagentforwarding', 'x11forwarding', 'allowtcpforwarding'):
            report['ssh'][key] = settings.get(key) if settings.get(key) in {'yes', 'no', 'local', 'remote'} else None
    observed = run(['/usr/sbin/ufw', 'status'])
    report['firewall'] = {'state': observed['state']}
    if observed['state'] == 'observed':
        lines = observed['output'].splitlines()
        allows = [line for line in lines if 'ALLOW' in line]
        report['firewall']['active'] = 'Status: active' in lines
        report['firewall']['ssh_tailnet_only'] = bool(allows) and all(
            re.fullmatch(r'OpenSSH(?: \(v6\))? on tailscale0\s+ALLOW\s+Anywhere(?: \(v6\))?\s*', line)
            for line in allows)
    observed = run(['/usr/sbin/nft', '-j', 'list', 'table', 'inet', 'work-ops-loopback'])
    report['loopback_rules'] = {'state': observed['state']}
    if observed['state'] == 'observed':
        report['loopback_rules']['sha256'] = hashlib.sha256(observed['output'].encode()).hexdigest()
    return report


def collect(action, unit=None):
    if action == 'status':
        return status()
    observed = command(['/usr/bin/journalctl', '--quiet', '--no-pager', '--output=json',
        '--output-fields=__REALTIME_TIMESTAMP,PRIORITY,MESSAGE', '--since=-24h', '--lines=50', '--unit=' + unit])
    return {'state': observed['state'], 'unit': unit,
            **(project_journal(observed['output']) if observed['state'] == 'observed' else {})}


def main():
    lock = None
    try:
        action, unit = validate(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        if os.geteuid() != 0 or Path(__file__).resolve().parent != Path('/usr/local/lib/work-ops'):
            raise Refused('use the installed diagnostics capability')
        os.umask(0o077)
        lock = os.open('/run/work-ops-diagnostics.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise Refused('unsafe diagnostics lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def expired(*_):
            raise Refused('diagnostics deadline exceeded')
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(30)
        report = collect(action, unit)
        signal.alarm(0)
        print(json.dumps({'version': 1, 'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                          'action': action, 'report': report}, sort_keys=True))
        return 0
    except BlockingIOError:
        print('{"version":1,"error":"busy"}')
        return 75
    except (Refused, OSError):
        print('{"version":1,"error":"refused-or-unavailable"}')
        return 64
    finally:
        signal.alarm(0)
        if lock is not None:
            os.close(lock)


if __name__ == '__main__':
    sys.exit(main())
