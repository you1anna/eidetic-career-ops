#!/usr/bin/env bash
# Deliberate credential-decryption test, distinct from read-only diagnostics. Also a release gate.
set -euo pipefail
case "${1:-}" in wo-scan.service|wo-email.service) ;; *) echo 'Usage: ops/vps/check-credentials.sh wo-scan.service|wo-email.service' >&2; exit 2;; esac
[[ $# == 1 ]] || exit 2
exec ssh -i "${SSH_KEY:-$HOME/.ssh/work_ops_vps}" -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o ControlPath=none -o ForwardAgent=no -o ClearAllForwardings=yes \
  -o ConnectTimeout=15 "${SSH_USER:-ops}@${SERVER_HOST:-100.64.0.10}" \
  "sudo -n /usr/bin/python3 -I /usr/local/lib/work-ops/credential_check.py $1"
