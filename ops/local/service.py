#!/usr/bin/env python3
"""The Mac's one LaunchAgent: the daily encrypted off-host backup (com.macmini.workops-offsite).

Since the cutover (27 September) the workspace, its database and the daily ingest run on the VPS. The
Mac's server and ingest agents and the phone relay were retired then; git history keeps them.
"""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
USER_HOME = Path.home()
LOGS = USER_HOME / 'Library/Logs/work-ops'
DOMAIN = f'gui/{os.getuid()}'
OFFSITE_LABEL = 'com.macmini.workops-offsite'
OFFSITE_PLIST = USER_HOME / 'Library/LaunchAgents' / (OFFSITE_LABEL + '.plist')
OFFSITE_TARGET = DOMAIN + '/' + OFFSITE_LABEL


def build_offsite_plist(root: Path, user_home: Path) -> dict:
    """Daily at 07:30, after the VPS backup (02:15 UTC) and scan (about 05:30 UTC); the owner chose daily
    on 27 Sep (#23, replacing weekly #21). launchd runs a missed time when the Mac wakes.
    `ops/offsite-weekly.sh` keeps a copy of the VPS's newest encrypted backup and runs the restore drill."""
    logs = user_home / 'Library/Logs/work-ops'
    return {
        'Label': OFFSITE_LABEL,
        'ProgramArguments': ['/bin/bash', str(root / 'ops/offsite-weekly.sh')],
        'WorkingDirectory': str(root),
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
        'StartCalendarInterval': {'Hour': 7, 'Minute': 30},
        'RunAtLoad': False,
        'ProcessType': 'Background',
        'StandardOutPath': str(logs / 'offsite.log'),
        'StandardErrorPath': str(logs / 'offsite-error.log'),
    }


def launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, check=check)


def offsite_install() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    OFFSITE_PLIST.write_bytes(plistlib.dumps(build_offsite_plist(ROOT, USER_HOME)))
    subprocess.run(['/usr/bin/plutil', '-lint', str(OFFSITE_PLIST)], check=True)
    if launchctl('print', OFFSITE_TARGET, check=False).returncode == 0:
        launchctl('bootout', OFFSITE_TARGET)
    launchctl('bootstrap', DOMAIN, str(OFFSITE_PLIST))
    print(f'Daily off-host backup installed: {OFFSITE_LABEL}, 07:30. Logs: {LOGS}/offsite.log')


def offsite_uninstall() -> None:
    if launchctl('print', OFFSITE_TARGET, check=False).returncode == 0:
        launchctl('bootout', OFFSITE_TARGET)
    OFFSITE_PLIST.unlink(missing_ok=True)
    print('Daily off-host backup removed.')


def offsite_status() -> None:
    loaded = launchctl('print', OFFSITE_TARGET, check=False).returncode == 0
    print(f'{OFFSITE_LABEL}: {"loaded" if loaded else "not loaded"}; plist {"present" if OFFSITE_PLIST.exists() else "absent"}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['offsite-install', 'offsite-uninstall', 'offsite-status'])
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This LaunchAgent manager is for macOS.')
    {'offsite-install': offsite_install, 'offsite-uninstall': offsite_uninstall,
     'offsite-status': offsite_status}[args.command]()


if __name__ == '__main__':
    try:
        main()
    except (OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
