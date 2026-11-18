#!/usr/bin/env bash
#
# Legacy pre-hosting upload tool (decision #20); not the current hosted backup path.
# Use ops/offsite-weekly.sh to retain the VPS backup locally. This tool backs up WO_DB locally.
# Encrypted off-host backup of the selected local database. A consistent `wo backup` is
# encrypted with age to ops/backup-recipient.txt (the private key stays on the Mac and in the owner's
# password manager), copied to the VPS, checked by sha256, and the newest KEEP copies are kept.
# The plaintext copy never leaves the temporary folder and is deleted at exit.
#
# Usage: ops/backup-offsite.sh            (WO_DB, SERVER_HOST, SSH_KEY, KEEP override the defaults)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${WO_DB:-$ROOT/data/work-ops.db}"
PY="${PY:-$ROOT/.venv/bin/python}"
SERVER_HOST="${SERVER_HOST:-100.64.0.10}"
SSH_USER="${SSH_USER:-ops}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/work_ops_vps}"
DEST="${OFFSITE_DIR:-/srv/offsite/work-ops}"
KEEP="${KEEP:-30}"
SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 "$SSH_USER@$SERVER_HOST")

tmp="$(mktemp -d)"; chmod 700 "$tmp"; trap 'rm -rf "$tmp"' EXIT
name="work-ops-$(date -u +%Y%m%dT%H%M%SZ).db.age"
"$PY" "$ROOT/tools/wo.py" --db "$DB" backup "$tmp/work-ops.db" >/dev/null
age -R "$ROOT/ops/backup-recipient.txt" -o "$tmp/$name" "$tmp/work-ops.db"
rm -f "$tmp/work-ops.db"
sum="$(shasum -a 256 "$tmp/$name" | cut -d' ' -f1)"
"${SSH[@]}" "umask 007; cat > '$DEST/$name.part'" < "$tmp/$name"
"${SSH[@]}" "set -e; cd '$DEST'; [ \"\$(sha256sum '$name.part' | cut -d' ' -f1)\" = '$sum' ] || { rm -f '$name.part'; echo 'checksum mismatch' >&2; exit 1; }
  mv '$name.part' '$name'; ls -1t work-ops-*.db.age | tail -n +$((KEEP + 1)) | xargs -r rm -f
  echo \"offsite ok: $name (\$(du -h '$name' | cut -f1)) on $SERVER_HOST:$DEST, \$(ls -1 work-ops-*.db.age | wc -l) kept\""
