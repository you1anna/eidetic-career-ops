#!/usr/bin/env bash
#
# Open the VPS-hosted work-ops workspace from this Mac through an SSH port-forward.
# Nothing is exposed publicly: the server keeps listening on its own loopback only, and the
# forward binds 127.0.0.1:8769 here. The local port must match the server port because the
# workspace validates the Host header against its own port.
#
# Usage: ops/vps/open.sh            # start the forward if needed, then open the browser
#        ops/vps/open.sh --stop     # close the forward
#
set -uo pipefail

SERVER_HOST="${SERVER_HOST:-100.64.0.10}"
SSH_USER="${SSH_USER:-ops}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/work_ops_vps}"
PORT="${PORT:-8769}"
URL="http://127.0.0.1:$PORT"
FORWARD="$PORT:127.0.0.1:$PORT"

forward_pids() { pgrep -f "ssh .*-L $FORWARD" || true; }
healthy() { /usr/bin/curl --silent --fail --max-time 3 "$URL/" >/dev/null 2>&1; }

if [[ "${1:-}" == "--stop" ]]; then
  pids=$(forward_pids)
  if [[ -n "$pids" ]]; then kill $pids && echo "forward closed"; else echo "no forward running"; fi
  exit 0
fi

if [[ -n "$(forward_pids)" ]] && healthy; then
  open "$URL"; exit 0
fi

if healthy; then
  echo "Port $PORT is already served locally (probably a development server)." >&2
  echo "Stop it first, then re-run this script." >&2
  exit 1
fi

ssh -f -N -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L "$FORWARD" "$SSH_USER@$SERVER_HOST" || { echo "SSH forward failed (server down, key missing, or port busy)" >&2; exit 1; }

for _ in $(seq 1 30); do
  healthy && { open "$URL"; echo "workspace: $URL (forwarded from $SERVER_HOST)"; exit 0; }
  sleep 0.2
done
echo "Forward is up but the workspace did not answer on $URL. Run ops/vps/check.sh." >&2
exit 1
