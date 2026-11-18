#!/usr/bin/env bash
#
# Fetch the newest encrypted backup from the VPS and decrypt it to a new local file. Needs the private
# key (~/.config/work-ops/backup.agekey, or AGE_KEY). Never overwrites an existing file. Uses the Mac's
# off-host key (~/.ssh/workops_offsite), which the VPS limits to wo-offsite-fetch, so it runs unattended
# even when the main VPS key has a passphrase; falls back to the main key where it is absent.
#
# Usage: ops/pull-backup.sh <dest.db>
set -euo pipefail
dest="${1:?usage: ops/pull-backup.sh <dest.db>}"
[[ ! -e "$dest" ]] || { echo "refusing to overwrite $dest" >&2; exit 2; }
SERVER_HOST="${SERVER_HOST:-100.64.0.10}"
SSH_USER="${SSH_USER:-ops}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/workops_offsite}"
[[ -r "$SSH_KEY" ]] || SSH_KEY="$HOME/.ssh/work_ops_vps"
AGE_KEY="${AGE_KEY:-$HOME/.config/work-ops/backup.agekey}"
# ControlPath=none: never share a connection with the main key (~/.ssh/config multiplexes the VPS), or the
# restricted key's forced command and the main key's sessions would mix.
SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ControlPath=none -o ConnectTimeout=15 "$SSH_USER@$SERVER_HOST")

latest="$("${SSH[@]}" wo-offsite-fetch latest)"
[[ -n "$latest" ]] || { echo "no backup found on $SERVER_HOST" >&2; exit 1; }
tmp="$(mktemp -d)"; chmod 700 "$tmp"; trap 'rm -rf "$tmp"' EXIT
"${SSH[@]}" wo-offsite-fetch get "$latest" > "$tmp/backup.age"
( umask 077; age -d -i "$AGE_KEY" -o "$dest" "$tmp/backup.age" )
echo "pulled $(basename "$latest") -> $dest"
