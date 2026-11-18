#!/usr/bin/env bash
# Ship this checkout's HEAD to the VPS. Code reaches the site only through this script.
#
#   ops/vps/deploy.sh --local-approved                       routine deploy of HEAD (must be pushed)
#   ops/vps/deploy.sh --local-approved --dry-run             VPS preflight; no site restart or migration
#   ops/vps/deploy.sh --local-approved --rev <sha>           deploy (or roll back to) a commit on origin/main
#   ops/vps/deploy.sh --local-approved --migrate             the same, applying new migrations (backs up first)
#   ops/vps/deploy.sh --local-approved --enable-automation   also turn the scan and backup timers on
#   ops/vps/deploy.sh --local-approved --disable-automation  also turn them off
#   ops/vps/deploy.sh --local-approved --cutover --database /abs/path.db   first deploy or a rebuilt VPS
#
# A routine deploy keeps the timers as they are. Roll back with --rev: this script (not the old
# commit's) deploys it. A commit from before this layout (28 Sep) is refused, and so is a rollback past
# a migration (the database would be newer than the code).
#
# Order: run ops/check.sh --strict in a private local archive of REV; on the VPS, check every precondition (commit on origin, clean checkout,
# Python, schema) before switching code; then pin the code checkout to the commit, migrate if asked,
# install the units, restart and confirm /api/health names the commit; then run ops/vps/check.sh.
# The site's code checkout (/srv/work-ops/work-ops) is detached at the deployed commit, and nothing
# but this script moves it. The VPS only reads from GitHub (decision #28: its deploy key is read-only).
# Preflight prepares deployment paths and fetches Git refs; --dry-run does not run local tests.
set -euo pipefail

# Parse the complete release program before running it; another local session may edit this file
# while its revision checks run. The selected code and this controller must both stay fixed.
main() {
SERVER_HOST="${SERVER_HOST:-100.64.0.10}"
SSH_USER="${SSH_USER:-ops}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/work_ops_vps}"
PUBLIC_ORIGIN="${PUBLIC_ORIGIN:-https://work.example.com}"
WORK_REPO="${WORK_REPO:-git@github.com:you1anna/eidetic-career-ops.git}"
GIT_EMAIL="${GIT_EMAIL:-owner@example.com}"

LOCAL_APPROVED=0; CUTOVER=0; AUTOMATION=keep; MIGRATE=0; SKIP_CHECKS=0; DRY_RUN=0; DATABASE=; REV=
LOCAL_ARTIFACT=
cleanup_local() { [[ -z "$LOCAL_ARTIFACT" ]] || rm -f "$LOCAL_ARTIFACT"; }
trap cleanup_local EXIT
while [[ $# -gt 0 ]]; do
  case "$1" in
    --local-approved) LOCAL_APPROVED=1 ;;
    --cutover) CUTOVER=1 ;;
    --enable-automation) AUTOMATION=1 ;;
    --disable-automation) AUTOMATION=0 ;;
    --migrate) MIGRATE=1 ;;
    --skip-checks) SKIP_CHECKS=1 ;;   # emergencies only: skips ops/check.sh before the deploy
    --dry-run) DRY_RUN=1 ;;
    --rev) shift; REV="${1:-}" ;;
    --database) shift; DATABASE="${1:-}" ;;
    *) echo "unknown or incomplete option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ "$LOCAL_APPROVED" != 1 ]]; then
  echo "pass --local-approved to confirm this deploy is intended (see CLAUDE.md, Showing the owner each change)" >&2
  exit 2
fi
if [[ "$CUTOVER" == 1 && -z "$DATABASE" ]]; then
  echo "cutover requires --database /absolute/path/to/the-approved-snapshot.db" >&2
  exit 2
fi
if [[ "$CUTOVER" != 1 && -n "$DATABASE" ]]; then
  echo "--database is accepted only with --cutover; routine deploys never replace hosted data" >&2
  exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$SSH_USER@$SERVER_HOST")
STAGED=/tmp/work-ops-approved-cutover.db

git -C "$REPO_DIR" fetch -q origin main
if [[ -z "$REV" ]]; then
  [[ -z "$(git -C "$REPO_DIR" status --porcelain --untracked-files=no)" ]] || { echo "commit tracked changes first" >&2; exit 1; }
  [[ -z "$(git -C "$REPO_DIR" log origin/main..HEAD --oneline)" ]] || { echo "push local commits first" >&2; exit 1; }
  REV="$(git -C "$REPO_DIR" rev-parse HEAD)"
else
  REV="$(git -C "$REPO_DIR" rev-parse --verify -q "$REV^{commit}")" || { echo "--rev: unknown commit" >&2; exit 2; }
  git -C "$REPO_DIR" merge-base --is-ancestor "$REV" origin/main || { echo "--rev: $REV is not on origin/main" >&2; exit 2; }
fi
# Every deployable commit has this layout: a backup job that never runs git (from 28 Sep).
git -C "$REPO_DIR" show "$REV:ops/vps/wo-backup.sh" 2>/dev/null | grep -q 'decision #28' || {
  echo "refusing $REV: it predates the pinned-checkout layout (28 Sep); its backup job would pull into the site" >&2; exit 2; }
echo "$([[ $DRY_RUN == 1 ]] && echo checking || echo deploying) $(git -C "$REPO_DIR" log -1 --format='%h %s' "$REV")"

if [[ "$SKIP_CHECKS" != 1 && "$DRY_RUN" != 1 ]]; then
  python3 "$REPO_DIR/ops/local/deploy_checks.py" "$REPO_DIR" "$REV" || {
    echo "required checks failed or were skipped; nothing was deployed" >&2; exit 1; }
fi

if [[ "$CUTOVER" == 1 && "$DRY_RUN" == 1 ]]; then
  echo "--dry-run does not stage a cutover database" >&2; exit 2
fi
if [[ "$CUTOVER" == 1 ]]; then
  [[ "$DATABASE" = /* && -s "$DATABASE" ]] || { echo "approved database must be an absolute, non-empty file" >&2; exit 2; }
  LOCAL_ARTIFACT="$(mktemp "${TMPDIR:-/tmp}/work-ops-cutover.XXXXXX.db")"
  chmod 600 "$LOCAL_ARTIFACT"
  "$REPO_DIR/.venv/bin/python" - "$DATABASE" "$LOCAL_ARTIFACT" "$REPO_DIR/workspace/migrations" <<'PY'
import re, sqlite3, sys
from pathlib import Path
db, artifact, folder = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
target = sqlite3.connect(artifact)
try: source.backup(target)
finally:
    target.close(); source.close()
conn = sqlite3.connect(f"file:{artifact}?mode=ro", uri=True)
try:
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise SystemExit("database integrity check failed")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"records","schema_version","review_queue","observations","source_health"}
    if not required <= tables: raise SystemExit("database does not have the current work-ops schema")
    have = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    want = {int(m.group(1)) for p in folder.glob("[0-9][0-9][0-9]_*.sql") if (m:=re.match(r"(\d{3})_",p.name))}
    if have != want: raise SystemExit(f"database schema versions {sorted(have)} do not match code {sorted(want)}")
    if conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0: raise SystemExit("database contains no records")
finally: conn.close()
PY
  "${SSH[@]}" "umask 077; cat >$STAGED" <"$LOCAL_ARTIFACT"
fi

# Hash privileged artifacts on the deploying machine, independently of the service-owned VPS Git
# checkout. The base64 manifest contains paths and hashes only (no credentials or private data).
ARTIFACT_MANIFEST="$("$REPO_DIR/.venv/bin/python" -I - "$REPO_DIR" "$REV" <<'MANIFEST_PY'
import base64, hashlib, json, subprocess, sys
repo, revision = sys.argv[1:]
paths = subprocess.check_output(["git", "-C", repo, "ls-tree", "-r", "--name-only", revision,
    "--", "ops/vps", "tools/ops_policy.py", "tools/research_codex.py", "tools/research_schemas.py", "tools/codex_maintenance.py"]).decode().splitlines()
manifest = {path: hashlib.sha256(subprocess.check_output(
    ["git", "-C", repo, "show", f"{revision}:{path}"])).hexdigest() for path in paths}
print(base64.b64encode(json.dumps(manifest).encode()).decode())
MANIFEST_PY
)"

"${SSH[@]}" "sudo bash -s" "$PUBLIC_ORIGIN" "$CUTOVER" "$WORK_REPO" "$GIT_EMAIL" "$AUTOMATION" "$STAGED" "$REV" "$MIGRATE" "$DRY_RUN" "$ARTIFACT_MANIFEST" <<'REMOTE'
set -euo pipefail
PUBLIC_ORIGIN="$1"; CUTOVER="$2"; WORK_REPO="$3"; GIT_EMAIL="$4"; AUTOMATION="$5"; STAGED="$6"; REV="$7"; MIGRATE="$8"; DRY_RUN="$9"; ARTIFACT_MANIFEST="${10}"
BASE=/srv/work-ops; CODE=$BASE/work-ops; DATA=$BASE/data; DB=$DATA/work-ops.db; PY=$BASE/venv/bin/python
KEY=$BASE/.ssh/workops_deploy
as_workops() { sudo -u workops -H "$@"; }
fail() { echo "deploy refused: $*; the running site was not changed" >&2; exit 1; }
TRUSTED=
cleanup() { rm -f "$STAGED"; [[ -z "$TRUSTED" ]] || rm -rf -- "$TRUSTED"; }
trap cleanup EXIT

# --- preconditions: nothing below changes the running site ---------------------------------------
# These paths live inside workops-owned /srv/work-ops, so workops could plant a symlink there. Root
# must not follow one: `install -d`/`chown` on a symlink act on its target, which would let a
# compromised workops have root create or chown an arbitrary file. Refuse any symlink, then create
# the directories as workops itself (same end state, root never traverses a workops-controlled path).
for p in "$DATA" "$DATA/pending" "$DATA/backups" "$BASE/run" "$BASE/.ssh" \
         "$DATA/.research-worker.lock" "$DATA/.writer.lock"; do
  [[ -L "$p" ]] && fail "$p is a symlink; refusing to follow it as root"
done
as_workops install -d -m 750 "$DATA" "$DATA/pending" "$DATA/backups" "$BASE/run"
as_workops install -d -m 700 "$BASE/.ssh"
[[ -f "$KEY" ]] || as_workops ssh-keygen -q -t ed25519 -N '' -C 'work-ops-vps deploy key' -f "$KEY"
GIT_SSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20"
clone() {
  local dir=$1
  if [[ ! -d "$dir/.git" ]]; then
    as_workops env GIT_SSH_COMMAND="$GIT_SSH" git clone -q "$WORK_REPO" "$dir" || {
      echo "clone failed; add this deploy key (read-only access) to the private work-ops repository, then retry" >&2
      cat "$KEY.pub"; exit 1; }
    as_workops git -C "$dir" config user.name "work-ops VPS"
    as_workops git -C "$dir" config user.email "$GIT_EMAIL"
    as_workops git -C "$dir" config pull.rebase true
  fi
}
clone "$CODE"
as_workops env GIT_SSH_COMMAND="$GIT_SSH" git -C "$CODE" fetch -q origin
as_workops git -C "$CODE" cat-file -e "$REV^{commit}" 2>/dev/null || fail "commit $REV is not on origin"
[[ -z "$(as_workops git -C "$CODE" status --porcelain --untracked-files=no)" ]] || fail "$CODE has local changes"
BEFORE_REV="$(as_workops git -C "$CODE" rev-parse HEAD)"
# Before 28 Sep the nightly job committed views in this checkout; anything unpushed there must reach origin first.
[[ "$(as_workops git -C "$CODE" rev-list --count origin/main..HEAD)" == 0 ]] || fail "$CODE has commits that are not on origin"
as_workops "$PY" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' || fail "$PY is missing or not Python 3.12 (ops/HANDOVER.md)"

if [[ "$CUTOVER" == 1 ]]; then
  [[ ! -e "$DB" ]] || fail "hosted database already exists"
  [[ -s "$STAGED" ]] || fail "staged approved database is missing"
elif [[ ! -s "$DB" ]]; then
  fail "hosted database absent; use --cutover with the approved snapshot"
fi

# Schema: full integrity outside the worker locks; the same version rule runs again under them.
TRUSTED=$(mktemp -d /tmp/work-ops-deploy.XXXXXXXX)
chmod 700 "$TRUSTED"
WANT="$(as_workops git -C "$CODE" ls-tree --name-only "$REV" workspace/migrations/ | sed -n 's#.*/\([0-9][0-9][0-9]\)_.*\.sql$#\1#p' | tr '\n' ' ')"
cat > "$TRUSTED/schema.py" <<'SCHEMA_PY'
import sqlite3, sys
db, migrate, full = sys.argv[1], sys.argv[2] == "1", sys.argv[3] == "full"
want = {int(v) for v in sys.argv[4:]}
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
try:
    if full and conn.execute("pragma integrity_check").fetchone()[0] != "ok":
        print("refuse integrity check failed"); raise SystemExit
    have = {r[0] for r in conn.execute("select version from schema_version")}
finally:
    conn.close()
if have == want: print("same")
elif have < want and migrate: print("migrate " + " ".join(map(str, sorted(want - have))))
elif have < want: print(f"refuse the commit adds migrations {sorted(want - have)}; deploy with --migrate")
else: print(f"refuse the database has migrations {sorted(have - want)} that this commit lacks (a rollback past a migration)")
SCHEMA_PY
schema_check() {
  # Root supplies the private script on stdin; workops cannot open TRUSTED. Never run its venv as root.
  if [[ "$CUTOVER" == 1 ]]; then
    /usr/bin/python3 -I - "$STAGED" "$MIGRATE" "$1" $WANT < "$TRUSTED/schema.py"
  else
    as_workops "$PY" -I - "$DB" "$MIGRATE" "$1" $WANT < "$TRUSTED/schema.py"
  fi
}
# The Dashboard's responsiveness figures live only in the server's memory, and the restart below
# clears them. Print their aggregates so every release log holds the figures from before it and a
# dry run reads them on demand: counts and durations of the server's own kinds, never content.
cat > "$TRUSTED/timings.py" <<'TIMINGS_PY'
import json
import re
import sys

NUMBERS = ('count', 'median_ms', 'p90_ms', 'max_ms')


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def name(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z_]{1,40}', value)


timings = json.load(sys.stdin)['responsiveness']
kinds = {}
for kind, figures in timings['kinds'].items():
    if not name(kind) or not figures.get('count'):
        continue
    kinds[kind] = {key: figures[key] for key in NUMBERS if number(figures.get(key))}
    phases = {key: value for key, value in (figures.get('phase_median_ms') or {}).items() if name(key) and number(value)}
    if phases:
        kinds[kind]['phase_median_ms'] = phases
since = timings.get('since')
print(json.dumps({'since': since if isinstance(since, str) and re.fullmatch(r'[0-9T:+.Z-]{10,40}', since) else '',
                  'kinds': kinds}, separators=(',', ':'), sort_keys=True))
TIMINGS_PY
timings_before() {
  # Never blocks a release: no server yet, a refused read or an unexpected reply prints "unavailable".
  local out
  out="$(curl -fs -m 20 http://127.0.0.1:8769/api/dashboard 2>/dev/null | /usr/bin/python3 -I "$TRUSTED/timings.py" 2>/dev/null)" || out=
  echo "responsiveness since the last restart: ${out:-unavailable}"
}
SCHEMA="$(schema_check full 2>&1)" || fail "schema check did not run: $SCHEMA"
[[ "$SCHEMA" == refuse* ]] && fail "${SCHEMA#refuse }"
[[ "$SCHEMA" == same || "$SCHEMA" == migrate* ]] || fail "schema check did not run: $SCHEMA"

# All root-installed sources are private, verified copies. Nothing is executed from this staging
# until every hash agrees with the deploying machine; Git output from workops is untrusted bytes.
/usr/bin/python3 -I - "$CODE" "$REV" "$TRUSTED" "$ARTIFACT_MANIFEST" <<'ARTIFACTS_PY'
import base64, hashlib, json, pathlib, subprocess, sys
code, revision, staging, encoded = sys.argv[1:]
manifest = json.loads(base64.b64decode(encoded, validate=True))
if not manifest or len(manifest) > 128:
    raise SystemExit("deploy refused: invalid artifact manifest")
for name in manifest:
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not (
        name.startswith("ops/vps/") or name in {"tools/research_codex.py", "tools/research_schemas.py", "tools/codex_maintenance.py", "tools/ops_policy.py"}
    ):
        raise SystemExit("deploy refused: invalid artifact path")
required = {"tools/codex_maintenance.py", "tools/research_codex.py", "tools/research_schemas.py"}
required.update("ops/vps/" + name for name in (
    "wo", "wo-offsite-fetch", "put-credential", "work-ops-workspace.service",
    "wo-research-codex.socket", "wo-research-codex@.service",
    "wo-research-acceptance.service", "wo-research-acceptance.timer",
    "wo-daily-review.service", "wo-daily-review.timer", "wo-scan.service", "wo-scan.timer",
    "wo-backup.service", "wo-backup.timer", "wo-email.service", "wo-email.timer",
    "wo-research.service", "wo-research.timer", "wo-caretaker.service", "wo-caretaker.timer"))
if missing := required - manifest.keys():
    raise SystemExit("deploy refused: missing required artifacts: " + ", ".join(sorted(missing)))
for name, expected in manifest.items():
    path = pathlib.PurePosixPath(name)
    with subprocess.Popen(["sudo", "-u", "workops", "-H", "git", "-C", code,
        "show", f"{revision}:{name}"], stdout=subprocess.PIPE) as process:
        content = process.stdout.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            process.kill()
            raise SystemExit(f"deploy refused: oversized artifact {name}")
        if process.wait() != 0:
            raise SystemExit(f"deploy refused: cannot read artifact {name}")
    if hashlib.sha256(content).hexdigest() != expected:
        raise SystemExit(f"deploy refused: artifact hash mismatch for {name}")
    target = pathlib.Path(staging) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
ARTIFACTS_PY

# Refuse an incompatible rollback in preflight as well as under the deployment locks.
if [[ -e "$DATA/collection.db" ]]; then
  as_workops git -C "$CODE" cat-file -e "$REV:config/collection.yml" 2>/dev/null \
    || fail "target predates API accounting; choose a cap-compatible release"
fi

if [[ "$DRY_RUN" == 1 ]]; then
  echo "dry run: ${REV:0:7} can be deployed; schema: $SCHEMA; serving $(curl -s http://127.0.0.1:8769/api/health | sed -n 's/.*"revision": *"\([0-9a-f]*\)".*/\1/p' | cut -c1-7)"
  echo "dry run: checkout $(as_workops git -C "$CODE" rev-parse --short HEAD)"
  echo "dry run: timers $(systemctl is-enabled wo-scan.timer 2>/dev/null)/$(systemctl is-enabled wo-backup.timer 2>/dev/null); running site and timers unchanged"
  timings_before
  exit 0
fi

# Pass only this private script to the lock holder. Its child inherits the locked descriptors;
# finally releases them on success, command failure or interruption, with no shell path-open race.
export PUBLIC_ORIGIN CUTOVER AUTOMATION STAGED REV MIGRATE DRY_RUN BASE CODE DATA DB PY SCHEMA TRUSTED BEFORE_REV WANT
export -f schema_check timings_before
cat > "$TRUSTED/change.sh" <<'CHANGE'
set -euo pipefail
as_workops() { sudo -u workops -H "$@"; }
fail() { echo "deploy refused: $*" >&2; exit 1; }
# --- change: waits for a running ingest or backup, which hold the writer lock --------------------
# Another deploy may have completed while these locks were unavailable. Revalidate before even
# installing dependencies; do not repeat the full integrity scan while workers are blocked.
[[ "$(as_workops git -C "$CODE" rev-parse HEAD)" == "$BEFORE_REV" ]] || fail "checkout changed while waiting; rerun preflight"
[[ -z "$(as_workops git -C "$CODE" status --porcelain --untracked-files=no)" ]] || fail "$CODE has local changes"
if [[ "$CUTOVER" == 1 ]]; then [[ ! -e "$DB" ]] || fail "hosted database already exists"; fi
LOCKED_SCHEMA="$(schema_check versions 2>&1)" || fail "locked schema check did not run: $LOCKED_SCHEMA"
[[ "$LOCKED_SCHEMA" != refuse* ]] || fail "${LOCKED_SCHEMA#refuse }"
[[ "$LOCKED_SCHEMA" == "$SCHEMA" ]] || fail "schema changed while waiting; rerun preflight"
# Last read of the outgoing server's timings, before anything changes.
timings_before
trap 'echo "deploy FAILED at line $LINENO after changes began: the site may be stopped or half-deployed. Run ops/vps/check.sh; journalctl -u work-ops-workspace. Roll back only to a commit compatible with the current schema; after a migration, use a compatible correction or approved backup recovery (ops/HANDOVER.md). Pre-migration backups are in $DATA/backups/work-ops-pre-migrate-*.db." >&2' ERR
as_workops sh -c "cd $BASE && git -C $CODE show $REV:requirements.txt > run/requirements.txt && UV_CACHE_DIR=$BASE/run/uv-cache /usr/local/bin/uv pip install --no-config -q --python $PY -r run/requirements.txt"
# Both worker locks are already held by the parent before changing code.
if [[ "$CUTOVER" == 1 ]]; then
  # Root reads only the approved staging file; the unprivileged writer creates the destination.
  cat "$STAGED" | as_workops sh -c 'umask 077; set -C; cat > "$1"' sh "$DB"
fi
# Retain cap enforcement across rollback, and drain old unmetered source readers at cutover.
COLLECTION_TARGET=0
as_workops git -C "$CODE" cat-file -e "$REV:config/collection.yml" 2>/dev/null && COLLECTION_TARGET=1
if [[ -e "$DATA/collection.db" && "$COLLECTION_TARGET" == 0 ]]; then
  fail "target predates API accounting; keep collectors paused and choose a cap-compatible release"
fi
# Ingest is drained under the writer lock; research has its own lock held by this deploy.
if [[ "$COLLECTION_TARGET" == 1 ]]; then
  # Stop triggers first so no old Python process can start while the checkout changes.
  # Enabled states and unit schedules stay intact; the existing restart block resumes them.
  systemctl stop wo-scan.timer wo-research.timer
  systemctl stop wo-scan.service wo-research.service
fi
as_workops git -C "$CODE" checkout -q --detach "$REV"
if [[ "$COLLECTION_TARGET" == 1 && ! -e "$DATA/collection.db" ]]; then
  as_workops "$PY" "$CODE/tools/collection_admin.py" initialize
fi
if [[ "$COLLECTION_TARGET" == 1 ]]; then
  as_workops "$PY" "$CODE/tools/wo.py" --db "$DB" collection-policy --json >/dev/null
fi
if [[ "$SCHEMA" == migrate* ]]; then
  systemctl stop work-ops-workspace.service
  as_workops "$PY" "$CODE/tools/wo.py" --db "$DB" backup "$DATA/backups/work-ops-pre-migrate-$(date -u +%Y%m%dT%H%M%SZ).db"
  as_workops env TZ=Europe/London "$PY" "$CODE/tools/wo.py" --db "$DB" migrate
  echo "migrated: ${SCHEMA#migrate }"
fi
install -m 755 -o root -g root "$TRUSTED/ops/vps/wo" /usr/local/bin/wo
install -m 755 -o root -g root "$TRUSTED/ops/vps/wo-offsite-fetch" /usr/local/bin/wo-offsite-fetch
install -m 755 -o root -g root "$TRUSTED/ops/vps/put-credential" /usr/local/bin/put-credential
# Also runs as root; installing it changes no Codex version (ops/OPERATIONS.md "Codex updates").
install -m 755 -o root -g root "$TRUSTED/tools/codex_maintenance.py" /usr/local/sbin/codex-maintenance
# Install capability helpers from independently hash-verified staging, never the mutable checkout.
# This changes no authorized_keys or Mac route: activation remains separately approved.
/usr/bin/python3 -I - "$TRUSTED" <<'HELPERS_PY'
import os, pathlib, stat, sys, tempfile
sources = ['ops/vps/wo_agent_gateway.py', 'ops/vps/wo_read_gateway.py', 'ops/vps/wo_diagnostics.py',
           'ops/vps/diagnostic_io.py', 'ops/vps/agent_access.py', 'ops/vps/credential_check.py',
           'tools/ops_policy.py']
root = pathlib.Path('/usr/local/lib/work-ops')
for parent in [*reversed(root.parents), root]:
    if parent == root and not parent.exists(): parent.mkdir(mode=0o755)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('refused unsafe capability installation directory')
for source in sources:
    path = pathlib.Path(sys.argv[1]) / source
    # Older compatible revisions lack these files; leave installed capabilities untouched.
    if not path.exists(): continue
    target = root / path.name
    if target.is_symlink(): raise SystemExit('refused symlink capability target')
    fd, temporary = tempfile.mkstemp(prefix='.install-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(path.read_bytes()); stream.flush(); os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
HELPERS_PY
sed -e "s#@PUBLIC_ORIGIN@#$PUBLIC_ORIGIN#" -e "s#@REVISION@#$REV#" "$TRUSTED/ops/vps/work-ops-workspace.service" >/etc/systemd/system/work-ops-workspace.service
install -d -m 755 /usr/local/lib/work-ops-research
install -m 644 "$TRUSTED/tools/research_codex.py" "$TRUSTED/tools/research_schemas.py" /usr/local/lib/work-ops-research/
install -m 644 "$TRUSTED/ops/vps/wo-research-codex.socket" "$TRUSTED/ops/vps/wo-research-codex@.service" /etc/systemd/system/
as_workops install -d -m 700 "$DATA/research-acceptance"
install -m 644 "$TRUSTED/ops/vps/wo-research-acceptance.service" "$TRUSTED/ops/vps/wo-research-acceptance.timer" /etc/systemd/system/
install -m 644 "$TRUSTED/ops/vps/wo-daily-review.service" "$TRUSTED/ops/vps/wo-daily-review.timer" /etc/systemd/system/
install -m 644 "$TRUSTED/ops/vps/wo-scan.service" "$TRUSTED/ops/vps/wo-scan.timer" \
  "$TRUSTED/ops/vps/wo-backup.service" "$TRUSTED/ops/vps/wo-backup.timer" \
  "$TRUSTED/ops/vps/wo-email.service" "$TRUSTED/ops/vps/wo-email.timer" \
  "$TRUSTED/ops/vps/wo-research.service" "$TRUSTED/ops/vps/wo-research.timer" \
  "$TRUSTED/ops/vps/wo-caretaker.service" "$TRUSTED/ops/vps/wo-caretaker.timer" /etc/systemd/system/
systemctl daemon-reload
if systemctl is-enabled -q wo-research-codex.socket; then systemctl restart wo-research-codex.socket; fi
systemctl enable -q work-ops-workspace.service
systemctl restart work-ops-workspace.service

# A routine deploy keeps each timer's approved state; only an explicit flag turns them on or off.
if [[ "$AUTOMATION" == keep ]]; then
  AUTOMATION=0
  for t in wo-scan.timer wo-backup.timer wo-email.timer wo-research.timer wo-caretaker.timer; do
    if systemctl is-enabled -q "$t"; then systemctl restart "$t"; AUTOMATION=kept; fi
  done
elif [[ "$AUTOMATION" == 1 ]]; then
  systemctl enable -q wo-scan.timer wo-backup.timer
  systemctl restart wo-scan.timer wo-backup.timer
else
  systemctl disable --now wo-scan.timer wo-backup.timer >/dev/null 2>&1 || true
fi

# Quiet while the restarted service comes up (the first tries are refused); loud only if it never answers.
health=$(curl -fs --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8769/api/health) \
  || { echo "the site did not answer on loopback within 10 tries after the restart" >&2; false; }
as_workops "$PY" -c 'import json,sys; h=json.load(sys.stdin); assert h["status"]=="ok" and h["mode"]=="database" and h["database"]=="/srv/work-ops/data/work-ops.db" and h["revision"]==sys.argv[1], h' "$REV" <<<"$health"
[[ "$(ss -ltnH 'sport = :8769' | awk '{print $4}')" == "127.0.0.1:8769" ]]
echo "deployed ${REV:0:7}; timers: $AUTOMATION"
CHANGE
# Create missing files without root following any service-controlled path. noclobber preserves
# existing lock inodes (install /dev/null would replace them and defeat another worker's lock).
for lk in "$DATA/.research-worker.lock" "$DATA/.writer.lock"; do
  as_workops sh -c 'umask 022; set -C; : > "$1"' sh "$lk" 2>/dev/null || [[ -f "$lk" ]] || fail "cannot create lock"
done
/usr/bin/python3 -I - "$DATA" "$TRUSTED/change.sh" <<'LOCKS_PY'
import errno, fcntl, os, pathlib, stat, subprocess, sys, time

def directory_fd(path):
    # Anchor every component: O_NOFOLLOW on only the final file would allow a parent symlink.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in pathlib.Path(path).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

def acquire(directory, name, timeout):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"unsafe deploy lock: {name}")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for deploy lock: {name}")
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except BaseException:
        os.close(fd)
        raise

def run(data, script):
    directory = directory_fd(data)
    locks = []
    try:
        # Same order and wait bounds as workers; keep locks until the entire change finishes.
        for name, timeout in ((".research-worker.lock", 2700), (".writer.lock", 1200)):
            locks.append(acquire(directory, name, timeout))
        return subprocess.run(["/bin/bash", script], pass_fds=locks).returncode
    finally:
        for fd in reversed(locks):
            os.close(fd)
        os.close(directory)

if __name__ == "__main__":
    try:
        sys.exit(run(*sys.argv[1:]))
    except (OSError, ValueError) as error:
        raise SystemExit(f"deploy refused: {error}")
LOCKS_PY
REMOTE

if [[ "$DRY_RUN" != 1 ]]; then
  "$REPO_DIR/ops/vps/check.sh" --expect-rev "$REV"
  # Preserve release-time decryption verification, scoped to each consuming service.
  "$REPO_DIR/ops/vps/check-credentials.sh" wo-scan.service
  "$REPO_DIR/ops/vps/check-credentials.sh" wo-email.service
fi
}
main "$@"; exit $?
