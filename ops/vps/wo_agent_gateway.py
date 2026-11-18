#!/usr/bin/python3 -I
"""Root-owned forced SSH command. Run as workops, never from a writable checkout.

Install /usr/local/lib/work-ops/wo_agent_gateway.py, owned root:root (0644), and bind
the dedicated key to sudo -u workops python3 -I <this file>, preserving only
SSH_ORIGINAL_COMMAND. No client text is ever executed by a shell.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = '/srv/work-ops'
PYTHON = BASE + '/venv/bin/python'
CLI = BASE + '/work-ops/tools/wo.py'
DATABASE = BASE + '/data/work-ops.db'
STAGING = BASE + '/run'
MAX_INPUT = 10 * 1024 * 1024


class Refused(ValueError):
    pass


# Exact option spellings only: never pass argparse abbreviations or global flags.
# Each entry declares the positional count, value options and switch options.
def rule(count, values='', switches=''):
    return count, set(values.split()), set(switches.split()) | {'--help'}


RULES = {
    'show': rule(1), 'find': rule(1, '--type'), 'state': rule(0),
    'brief': rule(0, switches='--md'), 'quality': rule(0, switches='--strict --json'),
    'import-report': rule(0), 'approach': rule(0, '--limit'),
    'research-review': rule(0, '--date'),
    'collection-policy': rule(0, switches='--json'),
    # Public web fetches run as codex itself. A live-record key must not turn the
    # privileged service account into a network proxy to loopback/metadata services.
    'fetch-log': rule(0, '--limit', '--failures --json'),
    'create': rule(0, '--json'), 'edit': rule(1, '--json'),
    'incoming-role': rule(0, '--json', '--preview'),
    'followup': rule(1, '--action --owner --status --reason --waiting-on --notes'),
    'transition': rule(3, '--note'),
    'interaction': rule(1, '--channel --direction --at --summary --evidence'),
    'draft': rule(1, '--title --text-file --purpose --reason', '--clear'),
    'link': rule(3, '--notes'), 'archive': rule(1, '--reason'),
    'merge': rule(2, '--reason'), 'repair': rule(0, switches='--dry-run'),
    'review': rule(2, '--target'), 'retype': rule(2, '--reason'),
    'company-number': rule(2, '--evidence'),
    'outcome': rule(2, '--note --draft-version --action-id --revision --company'),
    'correct': rule(1, '--revision --note --outcome --field'),
    'week include': rule(1),
    'decision list': rule(0, '--status'), 'decision show': rule(1),
    'decision add': rule(1, '--kind --question --context --recommend --reason --evidence'),
    'decision note': rule(2, '--stage --owner --next-step'),
    'question list': rule(0, switches='--all'), 'question show': rule(1),
    'question answer': rule(1, '--text-file'),
    'import-research': rule(0, '--stdin'),
    'import-list': rule(0, '--stdin', '--dry-run'),
}
for action in ('claim', 'block', 'done', 'reply', 'withdraw'):
    RULES['decision ' + action] = rule(2, '--revision')


def command_name(args: list[str]) -> str:
    """The RULES entry a request names: decision, question and week take a second word."""
    return ' '.join(args[:2 if args[0] in {'decision', 'question', 'week'} else 1])


def validate(original: str) -> tuple[list[str], str | None]:
    if len(original) > 128 * 1024 or '\x00' in original:
        raise Refused('invalid request size or encoding')
    try:
        tokens = shlex.split(original)
    except ValueError as exc:
        raise Refused('invalid quoting') from exc
    if tokens[:3] != ['wo', '--actor', 'agent'] or len(tokens) < 4:
        raise Refused('only wo --actor agent requests are accepted')
    args = tokens[3:]
    command = command_name(args)
    if command not in RULES:
        raise Refused('command is outside the record gateway allowlist')
    count, values, switches = RULES[command]
    positional, seen, options = [], set(), {}
    index = len(command.split())
    while index < len(args):
        token = args[index]
        if not token.startswith('-'):
            positional.append(token)
            index += 1
            continue
        flag, equals, inline = token.partition('=')
        if flag in seen or flag not in values | switches:
            raise Refused('unknown, repeated or forbidden option: ' + flag)
        seen.add(flag)
        if flag in switches:
            if equals:
                raise Refused('switch cannot have a value')
            index += 1
            continue
        if equals:
            value = inline
            index += 1
        else:
            if index + 1 == len(args):
                raise Refused('option needs a value')
            value = args[index + 1]
            index += 2
        if flag == '--text-file' and value != '-':
            raise Refused('text must arrive on stdin, never a server file')
        options[flag] = value
    if len(positional) != count and '--help' not in seen:
        raise Refused('wrong number of record arguments')
    upload = options.get('--stdin')
    if command in {'import-research', 'import-list'}:
        if not upload or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}\.json', upload):
            raise Refused('upload requires --stdin and a plain JSON basename')
        # Translate our transport flag to a private service-owned file below.
        args = [command] + (['--dry-run'] if '--dry-run' in seen else [])
    return args, upload


def read_input(stream, limit=MAX_INPUT):
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise Refused('stdin exceeds the 10 MiB gateway limit')
    return data


def execute(original, stream, run=subprocess.run):
    args, upload = validate(original)
    env = {'PATH': '/usr/bin:/bin', 'HOME': BASE, 'LANG': 'C.UTF-8',
           'TMPDIR': STAGING, 'TZ': 'Europe/London'}

    def invoke(arguments, data=None):
        return run([PYTHON, '-I', CLI, '--db', DATABASE, '--actor', 'agent', *arguments],
                   input=data if data is not None else b'', env=env,
                   cwd=BASE + '/work-ops', timeout=180).returncode

    if upload:
        data = read_input(stream)
        with tempfile.TemporaryDirectory(prefix='agent-upload.', dir=STAGING) as folder:
            target = Path(folder) / upload
            target.write_bytes(data)
            target.chmod(0o600)
            return invoke([args[0], str(target), *args[1:]])
    # Only these flags consume stdin. All other requests get an empty stream.
    uses_stdin = any(token in {'--json', '--text-file'} and args[i + 1:i + 2] == ['-']
                     or token in {'--json=-', '--text-file=-'}
                     for i, token in enumerate(args))
    return invoke(args, read_input(stream) if uses_stdin else None)


def main():
    try:
        return execute(os.environ.get('SSH_ORIGINAL_COMMAND', ''), sys.stdin.buffer)
    except (Refused, subprocess.TimeoutExpired) as exc:
        print('wo-agent: refused: ' + str(exc), file=sys.stderr)
        return 64


if __name__ == '__main__':
    sys.exit(main())
