#!/usr/bin/env bash
# SessionStart hook (.claude/settings.json), local and cloud. Idempotent and quick:
#  1. makes .venv with ops/make-venv.sh (Python 3.12 when available) if it is missing;
#  2. prints the owner's answered decisions, his unanswered card questions and what the site serves (and any undeployed code) from the
#     hosted database, or says once that live data is not
#     reachable from this session, so the session works on code only.
# Never fails the session: every step reports and carries on.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 0
ops/make-venv.sh 2>&1
# The Mac's daily off-host copy (ops/offsite-weekly.sh) fails silently when the Mac is offline at wake:
# say so here when the newest copy is more than two days old, or when its last restore drill failed.
if [[ -d data/offsite ]]; then
  newest=$(ls -1t data/offsite/work-ops-*.db.age 2>/dev/null | head -1)
  if [[ -z "$newest" || -n "$(find "$newest" -mtime +2 2>/dev/null)" ]]; then
    echo "work-ops off-host copy: STALE (newest: ${newest##*/}). Run ops/offsite-weekly.sh and read ~/Library/Logs/work-ops/offsite-error.log."
  elif grep -q '"result": "failed"' data/offsite/last-run.json 2>/dev/null; then
    echo "work-ops off-host copy: the last restore drill FAILED. Read ~/Library/Logs/work-ops/offsite-error.log, then rerun ops/offsite-weekly.sh."
  elif grep -q '"result": "defects"' data/offsite/last-run.json 2>/dev/null; then
    echo "work-ops data: the last off-host copy restores, but wo quality --strict found defects. Run ops/wo-live quality."
  fi
fi
KEY="${SSH_KEY:-$HOME/.ssh/work_ops_vps}"
VPS="ops@${SERVER_HOST:-100.64.0.10}"  # the VPS on the tailnet; public SSH is closed
if out=$(ops/wo-live decision list 2>/dev/null); then
  echo "work-ops live decisions (reconcile current scope; preserve other owners' claims): ${out}"
  approach=$(ops/wo-live approach --limit 0 2>/dev/null | head -1) && [[ -n "$approach" ]] && echo "work-ops ${approach} (/approach to work the queue)"
  # The owner's "Ask Claude…" questions from the cards: answer them in this session (CLAUDE.md).
  asked=$(ops/wo-live question list 2>/dev/null) && [[ -n "$asked" && "$asked" != No\ * ]] \
    && echo "work-ops questions from the owner's cards (answer each with ops/wo-live question answer <id> --text-file -): ${asked}"
  # What the site runs, and whether code or config on main has not reached it yet (only deploy.sh ships it).
  if [[ -e "$HOME/.config/work-ops/live-gateway.json" || -L "$HOME/.config/work-ops/live-gateway.json" ]]; then
    rev=$(ops/wo-diagnostics status 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("report",{}).get("origin",{}).get("revision") or "")' 2>/dev/null)
  else
    # Preserve pre-activation operator health visibility; never use this after a route is configured.
    rev=$(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=6 "$VPS" \
          curl -fsS http://127.0.0.1:8769/api/health 2>/dev/null | sed -n 's/.*"revision": *"\([0-9a-f]*\)".*/\1/p')
  fi
  if [[ -n "$rev" ]] && n=$(git rev-list --count "$rev..main" -- tools workspace config ops tests requirements.txt 2>/dev/null); then
    echo "work-ops site: serving ${rev:0:7}; ${n} commit(s) on main change code or config and are not deployed."
  fi
else
  if [[ -e "$HOME/.config/work-ops/live-gateway.json" || -L "$HOME/.config/work-ops/live-gateway.json" ]]; then
    echo 'work-ops: restricted record access unavailable. Inspect ops/agent-access verify; no administrator fallback attempted.'
    exit 0
  fi
  key_status=0
  ops/vps-unlock --check || key_status=$?
  if [[ "$key_status" == 3 ]]; then
    echo "work-ops: VPS authentication needs attention. Ask the owner to run ops/vps-unlock in a terminal on this Mac."
    echo "Never ask for, store or type the passphrase."
  else
    echo "work-ops: live SSH failed; key readiness status $key_status (0 ready, 2 missing, 4 inconclusive)."
    echo "Check the diagnostic above and the session's agent/network permissions; do not assume the key is locked."
  fi
  echo "Use the normal permission layer for scoped SSH access; never bypass a denial or weaken sandbox settings."
  echo "Until reachable, work on code with synthetic fixtures; do not claim live validation or deployment."
fi
exit 0
