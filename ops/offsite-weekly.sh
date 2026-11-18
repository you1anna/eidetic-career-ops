#!/usr/bin/env bash
#
# The daily off-host backup (LaunchAgent com.macmini.workops-offsite, 07:30; the owner chose daily on
# 27 September, #23). The file keeps its old name so the installed agent's path stays valid.
# The VPS writes an encrypted backup nightly; this keeps a copy of the newest on the Mac (still
# encrypted, 8 kept in data/offsite/), then the restore drill proves that copy restores. Each run leaves
# data/offsite/last-run.json (passed, defects or failed), which ops/vps/check.sh reports.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "== $(date '+%Y-%m-%d %H:%M')"
KEY="${SSH_KEY:-$HOME/.ssh/workops_offsite}"; [[ -r "$KEY" ]] || KEY="$HOME/.ssh/work_ops_vps"
# ControlPath=none: never share a connection with the main key (~/.ssh/config multiplexes the VPS), or the
# restricted key's forced command and the main key's sessions would mix.
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ControlPath=none -o ConnectTimeout=15 "${SSH_USER:-ops}@${SERVER_HOST:-100.64.0.10}")
# launchd runs a missed 07:30 the moment the Mac wakes, often before the network is up: on 30 September
# and 1 October 2026 the one attempt failed with "Network is unreachable" and no copy was kept. Wait for
# the VPS (12 tries, 30 s apart) before giving up, and say so in the log when it fails.
OFFSITE="$ROOT/data/offsite"; mkdir -p "$OFFSITE"; chmod 700 "$OFFSITE"; latest=""
receipt() {
  printf '{"at": "%s", "result": "%s", "backup": "%s"}\n' "$(date -u +%FT%TZ)" "$1" "${latest##*/}" > "$OFFSITE/last-run.json.part"
  mv "$OFFSITE/last-run.json.part" "$OFFSITE/last-run.json"
}
trap 'echo "FAILED: the off-host copy or its restore drill did not complete (see offsite-error.log)"; receipt failed' ERR
# An interrupted download leaves a .part file that retention (below) never matches.
find "$OFFSITE" -maxdepth 1 -name 'work-ops-*.db.age.part' -mmin +60 -delete
tries="${OFFSITE_TRIES:-12}"; pause="${OFFSITE_PAUSE:-30}"
for ((i = 1; i <= tries; i++)); do
  if latest="$("${SSH[@]}" wo-offsite-fetch latest)"; then break; fi
  if ((i == tries)); then echo "VPS not reachable after $tries tries" >&2; false; fi
  sleep "$pause"
done
[[ -n "$latest" ]] || { echo "no encrypted backup on the VPS" >&2; false; }
kept="$OFFSITE/$(basename "$latest")"
"${SSH[@]}" wo-offsite-fetch get "$latest" > "$kept.part"
mv "$kept.part" "$kept"
ls -1t "$OFFSITE"/work-ops-*.db.age | tail -n +9 | xargs -r rm -f
echo "kept $(basename "$latest") on this Mac"
drill=0; "$ROOT/ops/restore-drill.sh" "$kept" || drill=$?
case $drill in
  0) receipt passed ;;
  3) receipt defects
     echo "RESTORED, with data defects: the copy restores; wo quality --strict found defects (see offsite-error.log)"
     exit 3 ;;
  *) false ;;  # the ERR trap records the failure
esac
