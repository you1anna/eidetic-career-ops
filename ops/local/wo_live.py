"""`ops/wo-live`: the agent's gateway to the hosted `wo`.

The VPS `wo` runs any subcommand, including ones that replace or migrate the whole database. This
gateway passes only the reads and record-level agent edits listed here, always as `--actor agent`,
and quotes every argument into one remote command so nothing reaches the remote shell unquoted.

    ops/wo-live decision list
    ops/wo-live question list
    ops/wo-live question answer 3 --text-file - < answer.md
    ops/wo-live show CO-itoflow
    ops/wo-live draft CO-x --title "…" --text-file - < note.md
    ops/wo-live import-research data/research/2026-09-29-approach.json
    ops/wo-live import-list data/research/2026-10-20-list.json --dry-run    (the owner's list; approved 14 Oct 2026)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from ssh_route import Refused, load_route as read_route, ssh_command

HOST = os.environ.get('WO_LIVE_HOST', 'ops@100.64.0.10')
KEY = os.environ.get('SSH_KEY') or str(Path.home() / '.ssh/work_ops_vps')
READS = {'collection-policy', 'show', 'find', 'state', 'brief', 'quality', 'import-report', 'fetch', 'fetch-log', 'approach', 'research-review'}
EDITS = {'incoming-role', 'create', 'edit', 'followup', 'transition', 'interaction', 'draft', 'link', 'archive', 'merge',
         'repair', 'review', 'retype', 'company-number', 'outcome', 'correct'}
DECISION = {'list', 'show', 'add', 'claim', 'block', 'done', 'reply', 'withdraw', 'note'}
# The owner asks on the site ("Ask Claude…"); the agent lists, reads and answers.
QUESTION = {'list', 'show', 'answer'}
RESEARCH_NAME = re.compile(r'^[\w.-]+\.json$')
# Commands that upload one local JSON file to a private staging folder, and the flags each accepts.
STAGED = {'import-research': (), 'import-list': ('--dry-run',)}


def remote_command(args: list[str], *, restricted: bool = False) -> str:
    """The one quoted command line the VPS runs, or Refused."""
    if not args:
        raise Refused('usage: ops/wo-live <command> [args]; see ops/local/wo_live.py for the allowed commands')
    for a in args:
        if a.startswith(('--actor', '--db')):
            raise Refused(f'{a.split("=")[0]} is set by the gateway: every live change is --actor agent on the hosted database')
    cmd, rest = args[0], args[1:]
    if cmd == 'decision':
        if not rest or rest[0] not in DECISION:
            raise Refused(f'decision takes one of: {", ".join(sorted(DECISION))}')
    elif cmd == 'question':
        if not rest or rest[0] not in QUESTION:
            raise Refused(f'question takes one of: {", ".join(sorted(QUESTION))}')
    elif cmd in STAGED:
        flags = rest[1:]
        if not rest or any(f not in STAGED[cmd] for f in flags) or len(set(flags)) != len(flags):
            raise Refused(f'{cmd} takes one local JSON file' + (f' and optionally {", ".join(STAGED[cmd])}' if STAGED[cmd] else ''))
        name = Path(rest[0]).name
        if not RESEARCH_NAME.match(name):
            raise Refused(f'file name {name!r} must be letters, digits, dots, dashes or underscores, ending .json')
        if restricted:
            return shlex.join(['wo', '--actor', 'agent', cmd, '--stdin', name, *flags])
        # The file name is the research source (import_research), so it is kept inside a private temp
        # folder. `wo` runs as workops, so the folder and file are made as workops in the service's own run
        # directory: a folder made by ops (mktemp -d, mode 700) cannot be read by the import (1 Oct 2026),
        # and one in /tmp would be readable by every user on the server.
        target = '"$d"/' + shlex.quote(name)
        as_service = 'sudo -n -u workops '
        return ('d=$(' + as_service + 'mktemp -d -p /srv/work-ops/run) && ' + as_service + 'tee ' + target
                + ' >/dev/null && wo --actor agent ' + cmd + ' ' + target + ''.join(' ' + f for f in flags)
                + '; rc=$?; ' + as_service + 'rm -rf "$d"; exit $rc')
    elif cmd == 'week':
        if len(rest) != 2 or rest[0] != 'include':
            raise Refused('week include <id> puts one ready person at the top of this week')
    elif cmd not in READS | EDITS:
        raise Refused(f'{cmd!r} is not available through the gateway; allowed: '
                      + ', '.join(sorted(READS | EDITS | {'decision', 'question', *STAGED})))
    return ' '.join(shlex.quote(a) for a in ['wo', '--actor', 'agent', *args])


def load_route() -> dict:
    """Optional per-account restricted transport; contains paths, never secret values."""
    return read_route('live-gateway.json', legacy={'host': HOST, 'key': KEY, 'restricted': False})


def main(argv: list[str] | None = None, run=subprocess.run, route=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        route = load_route() if route is None else route
        remote = remote_command(args, restricted=route['restricted'])
    except Refused as e:
        print(f'wo-live: refused: {e}', file=sys.stderr)
        return 64
    ssh = ssh_command(route, remote)
    if args[0] in STAGED:
        with open(args[1], 'rb') as f:
            return run(ssh, stdin=f).returncode
    return run(ssh).returncode


if __name__ == '__main__':
    sys.exit(main())
