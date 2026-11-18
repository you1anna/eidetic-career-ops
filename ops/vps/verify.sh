#!/usr/bin/env bash
# Test committed code on the VPS, in scratch storage with synthetic data.
# No deployment, package installation, production database access or service changes.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."

usage() {
  echo 'Usage: ops/vps/verify.sh [--list | --help]'
  echo 'Verify HEAD on the VPS; --list prints the committed transfer contents without SSH.'
  echo 'Overrides: SERVER_HOST, SSH_USER, SSH_KEY (same defaults as ops/vps/check.sh).'
  echo 'Uncommitted changes are excluded. Run ops/check.sh --strict locally for browser coverage.'
}
mode=${1:-run}
[[ $# -le 1 ]] || { usage >&2; exit 2; }
case "$mode" in run|--list) ;; --help|-h) usage; exit 0 ;; *) usage >&2; exit 2 ;; esac

revision=$(git rev-parse --verify HEAD)
# Deliberately exclude records, evidence, provenance, exports, keys and Git history.
paths=(.gitignore .python-version requirements.txt workspace tools config tests/ui
  'ops/*.sh' 'ops/local/*.py' 'ops/vps/*.sh' 'ops/vps/*.service'
  'ops/vps/*.timer' 'ops/vps/*.conf' ops/vps/wo ops/vps/wo-offsite-fetch
  ops/backup-recipient.txt)
if [[ $mode == --list ]]; then
  git archive --format=tar "$revision" -- "${paths[@]}" | tar -tf -
  exit 0
fi
echo "Verifying committed source $revision on ${SERVER_HOST:-100.64.0.10} (synthetic data only)"
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  echo 'NOTE: local uncommitted changes are excluded from this verification.'
fi
# The remote program is fixed; no shell interpolation of user-provided paths or host settings.
remote='set -euo pipefail
umask 077
cd /tmp
verify_dir=$(mktemp -d /srv/work-ops/run/verify-candidate.XXXXXX)
trap '\''rm -rf -- "$verify_dir"'\'' EXIT
tar -x -f - -C "$verify_dir"
git -C "$verify_dir" init -q --initial-branch=main
ln -s /srv/work-ops/venv "$verify_dir/.venv"
cd "$verify_dir"
unset WO_DB WO_CONTRACT_DB WO_UI_DB
export PYTHONDONTWRITEBYTECODE=1
ops/check.sh --strict unit contract
for script_file in ops/*.sh ops/vps/*.sh ops/vps/wo ops/vps/wo-offsite-fetch; do
  bash -n "$script_file"
done
echo "ok    shell syntax"
'
remote_quoted=${remote//\'/\'\\\'\'}
remote_command="sudo -n -u workops bash -c '$remote_quoted'"
git archive --format=tar "$revision" -- "${paths[@]}" |
  ssh -i "${SSH_KEY:-$HOME/.ssh/work_ops_vps}" -o IdentitiesOnly=yes \
    -o BatchMode=yes -o ConnectTimeout=15 "${SSH_USER:-ops}@${SERVER_HOST:-100.64.0.10}" \
    "$remote_command"
echo "Verified $revision; temporary files removed. Production was not deployed."
