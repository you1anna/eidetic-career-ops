#!/usr/bin/env bash
#
# Prove an off-host backup restores: decrypt it into a scratch folder, check integrity and records, bring
# a schema that is behind this checkout forward with the code's migrations (ops/local/restore_check.py),
# then run `wo quality --strict`. Read-only for the live database. Run on the Mac, where the private key is.
# Exit 0: restores, no defects. Exit 3: restores, but the data has quality defects (a data problem, not a
# failed backup). Any other exit: the restore failed.
#
#   ops/restore-drill.sh                   pull the newest encrypted backup from the VPS
#   ops/restore-drill.sh <file.db.age>     use an encrypted copy already on this Mac (the daily job's)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
AGE_KEY="${AGE_KEY:-$HOME/.config/work-ops/backup.agekey}"
tmp="$(mktemp -d)"; chmod 700 "$tmp"; trap 'rm -rf "$tmp"' EXIT
if [[ $# -gt 0 ]]; then
  ( umask 077; age -d -i "$AGE_KEY" -o "$tmp/restored.db" "$1" )
  echo "decrypted $(basename "$1")"
else
  "$ROOT/ops/pull-backup.sh" "$tmp/restored.db"
fi
"$PY" "$ROOT/ops/local/restore_check.py" "$tmp/restored.db"
# No `| head` here: under pipefail, a long report would end the writer with SIGPIPE and fail a passing drill.
quality="$("$PY" "$ROOT/tools/wo.py" --db "$tmp/restored.db" quality --strict)" || defects=1
summary="${quality%%$'\n'*}"
# A crash also exits 1; only a report ("N active records: D defects, G gaps") means the data was checked.
if [[ ! $summary =~ ^[0-9]+\ active\ records:\ [0-9]+\ defects ]]; then
  sed -n '1,20p' <<<"$quality" >&2
  echo "restore FAILED: wo quality did not run on the restored copy" >&2
  exit 1
fi
echo "$summary"
if [[ -n "${defects:-}" ]]; then
  sed -n '2,20p' <<<"$quality" >&2
  echo "the copy restores, but wo quality --strict found defects: run ops/wo-live quality on the site's data" >&2
  exit 3
fi
echo "drill passed"
