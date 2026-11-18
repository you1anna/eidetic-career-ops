#!/usr/bin/env python3
"""PreToolUse guard (.claude/settings.json): agents reach live data only through `ops/wo-live`.

Claude Code passes each Bash call as JSON on stdin. A command that runs `wo` or `sqlite3` on the VPS
over raw ssh, reads the database file there, or copies files to or from the VPS with scp, rsync or
sftp is refused: exit 2, with the reason on stderr for the agent. Those routes skip the gateway's
allowlist and its fixed `--actor agent`, and an upload to `/tmp` there is readable by every user on
the server. Other ssh use (systemctl, journalctl, `ops/vps/*` scripts, deploys) is untouched, and so
is any command that only mentions the route inside an argument, such as a commit message.

The guard narrows one route; it must never stop a session. Input it cannot read is allowed. The owner's
own `!` commands are not tool calls, so they never reach it.
Runs on the Macs' system Python (3.9) and in cloud sessions.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wo_live import HOST  # noqa: E402  (one place names the VPS)

# The gateway's host, plus every name the same server answers to: its public address, the ssh-config
# aliases and its tailnet address (OPERATIONS.md, "Tailscale"). The MagicDNS name contains `work-ops-vps`.
VPS_NAMES = (HOST.rsplit('@', 1)[-1], '203.0.113.10', '100.64.0.10', 'work-ops-vps', 'codex-vps')
COPIERS = {'scp', 'rsync', 'sftp'}
SHELLS = {'bash', 'sh', 'zsh'}
WRAPPERS = {'sudo', 'env', 'command', 'exec', 'time', 'nohup'}
SEPARATORS = set(';&|()\n')
LIVE_WORD = re.compile(r'^(wo|sqlite3)$|work-ops\.db')
REASON = ('work-ops guard: live data goes through ops/wo-live (allowlisted, always --actor agent); '
          'raw ssh or file copies to the VPS skip it. Use ops/wo-live <command>, '
          'ops/wo-live import-research <file> for research, or ops/pull-backup.sh for a local copy. '
          'If the owner needs this exact command, give it to him to run himself as `! <command>`.')


def names_vps(word: str) -> bool:
    return any(name in word for name in VPS_NAMES)


def segments(command: str):
    """Each simple command as a list of words, split at ; & | ( ) and newlines outside quotes."""
    lex = shlex.shlex(command, posix=True, punctuation_chars='();<>|&\n')
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    words: list[str] = []
    try:
        for token in lex:
            if token and set(token) <= SEPARATORS:
                if words:
                    yield words
                words = []
            else:
                words.append(token)
    except ValueError:  # unbalanced quotes: judge the raw lines instead
        for line in command.splitlines():
            if line.split():
                yield line.split()
        return
    if words:
        yield words


def program(words: list[str]) -> tuple[str, list[str]]:
    """The program a simple command runs, past variable assignments and wrappers, with its arguments."""
    i = 0
    while i < len(words) and ('=' in words[i].split('/')[0] or os.path.basename(words[i]) in WRAPPERS):
        i += 1
    if i == len(words):
        return '', []
    return os.path.basename(words[i]), words[i + 1:]


def refused(command: str, depth: int = 0) -> bool:
    for words in segments(command):
        name, args = program(words)
        if name in SHELLS and '-c' in args and depth < 3:
            script = args[args.index('-c') + 1:args.index('-c') + 2]
            if script and refused(script[0], depth + 1):
                return True
        elif name in COPIERS and any(names_vps(a) for a in args):
            return True
        elif name == 'ssh':
            at = next((i for i, a in enumerate(args) if names_vps(a) and ' ' not in a), None)
            if at is not None:
                remote = re.split(r'[\s;&|()]+', ' '.join(args[at + 1:]))
                if any(LIVE_WORD.search(os.path.basename(w)) for w in remote if w):
                    return True
            # A remote command run on another host (`ssh mini '…'`) is judged as a command too.
            elif depth < 3 and any(' ' in a and refused(a, depth + 1) for a in args):
                return True
    return False


def main() -> int:
    try:
        call = json.load(sys.stdin)
        command = call['tool_input']['command']
    except (ValueError, KeyError, TypeError):
        return 0
    if call.get('tool_name', 'Bash') != 'Bash' or not isinstance(command, str):
        return 0
    if refused(command):
        print(REASON, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
