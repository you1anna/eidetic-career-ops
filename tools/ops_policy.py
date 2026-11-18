"""Fixed operational inventory; caretaker evaluation remains in caretaker.py.

The diagnostics installer copies this module into its root-owned directory.
"""
CARETAKER_UNITS = (
    'work-ops-workspace.service', 'cloudflared.service', 'wo-scan.timer', 'wo-backup.timer',
    'wo-email.timer', 'wo-research.timer', 'wo-research-codex.socket',
    'wo-email.service', 'wo-research.service', 'wo-scan.service', 'wo-backup.service',
)
DIAGNOSTIC_UNITS = CARETAKER_UNITS + (
    'wo-caretaker.service', 'wo-caretaker.timer', 'work-ops-loopback-guard.service',
)
LOG_UNITS = tuple(unit for unit in DIAGNOSTIC_UNITS if unit.endswith('.service'))


def diagnostic_request(args):
    if args == ['status']:
        return 'status', None
    if len(args) == 2 and args[0] == 'logs' and args[1] in LOG_UNITS:
        return 'logs', args[1]
    raise ValueError('only status or logs for an allowlisted service is permitted')
