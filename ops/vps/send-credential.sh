#!/usr/bin/env bash
#
# Copy one key from the Mac's .env to the VPS's encrypted credential store, without printing it, writing
# it to a file or putting it on a command line. Only a key whose source the owner approved belongs there:
# Companies House (#29); Adzuna and Reed (#39); the explicitly requested dedicated alerts inbox.
#
#   ops/vps/send-credential.sh COMPANIES_HOUSE_API_KEY
set -euo pipefail
cd "$(dirname "$0")/../.."
[[ $# -eq 1 ]] || { echo 'usage: send-credential.sh NAME' >&2; exit 2; }
name=$1
# Validate before reading .env or interpolating the name into SSH's remote shell command.
case "$name" in
  COMPANIES_HOUSE_API_KEY|ADZUNA_APP_ID|ADZUNA_APP_KEY|REED_API_KEY|WO_ALERTS_IMAP_HOST|WO_ALERTS_IMAP_USER|WO_ALERTS_IMAP_PASSWORD) ;;
  *) echo 'send-credential: choose an approved credential name' >&2; exit 2 ;;
esac
SERVER_HOST=${SERVER_HOST:-100.64.0.10}
SSH_USER=${SSH_USER:-ops}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/work_ops_vps}
.venv/bin/python - "$name" <<'PY' | ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 "$SSH_USER@$SERVER_HOST" "sudo -n /usr/local/bin/put-credential $name"
import sys
sys.path[:0] = ["tools"]
import ingest
value = ingest.load_env(".env").get(sys.argv[1], "")
if not value:
    sys.exit(f"send-credential: {sys.argv[1]} is not in .env")
sys.stdout.write(value)
PY
