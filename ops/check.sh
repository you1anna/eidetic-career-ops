#!/usr/bin/env bash
# Every local check, with progress, timings and private per-step logs.
#   ops/check.sh                  unit + contract + ui + worker
#   ops/check.sh unit ui          just those suites
#   ops/check.sh --strict visual  explicit image comparisons on a supported baseline environment
#   ops/check.sh --strict         fail if any suite fails or cannot run (deployment gate)
# On a Mac (the Air or the mini) Python tests run under pytest, one worker per core when the machine
# has at least 4 GiB of RAM, and requirements-dev.txt is installed when pytest is missing. The VPS,
# including the Codex checkout at /home/codex/workspaces/work-ops, shares the machine with the live
# site, so checks there install nothing and use one worker; without pytest the same files run under
# unittest. WO_PYTEST_WORKERS overrides the count and WO_CHECK_PROFILE=vps|mac overrides detection.
# Independent browser suites use at most two workers (one on the VPS/low-memory hosts).
# WO_UI_JOBS=1 forces serial browser checks on a Mac.
# Contracts run on a backup copy of data/work-ops.db when that file is in the checkout (a Mac's
# development copy), otherwise a synthetic database from tools/make_fixture.py. The live database
# stays /srv/work-ops/data/work-ops.db and is not opened here. Browser checks always use a fresh
# synthetic fixture, copied per browser. The email Worker suite runs only where its own dependencies
# are installed (npm ci --prefix ops/email-intake).
# A suite that cannot run here prints SKIP with the reason; strict mode also exits non-zero.
set -uo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1

# Bash reads a script while running it, so the body is one function: an edit to this file during a
# run cannot change the run.
main() {
py=.venv/bin/python
failed=0; skipped=0; strict=0; preflight_only=0; step_no=0
usage() {
  printf '%s\n' 'Usage: ops/check.sh [--strict] [unit|contract|ui|visual|worker ...] | --preflight' \
    'With no suites, run unit, contract, ui and worker.' \
    'visual is a separate, explicit comparison against reviewed platform-specific images.' \
    '--strict makes unavailable checks fail the command; default mode reports SKIP.' \
    '--preflight checks Python setup and local-port permissions only; it runs no suites.' \
    'Each step prints progress every 30s and retains its full output in a private temporary directory.'
}
suites=()
for arg in "$@"; do
  case "$arg" in
    --strict) strict=1 ;;
    --preflight) preflight_only=1 ;;
    --help|-h) usage; exit 0 ;;
    unit|contract|ui|visual|worker) suites+=("$arg") ;;
    *) echo "unknown option or suite: $arg" >&2; usage >&2; exit 2 ;;
  esac
done
if (( preflight_only && (strict || ${#suites[@]}) )); then
  echo '--preflight cannot be combined with suites or --strict' >&2; exit 2
fi
(( ${#suites[@]} )) || suites=(unit contract ui worker)

step() {
  local name=$1
  shift
  step_no=$((step_no + 1))
  $py ops/local/check_runner.py run "$name" "$check_logs/$step_no.log" "$@"
  local code=$?
  # An interrupted run must not continue with the next suite.
  (( code == 130 || code == 143 )) && exit "$code"
  (( code == 0 )) || failed=1
}
queue_step() {
  local name=$1
  shift
  step_no=$((step_no + 1))
  $py ops/local/check_runner.py queue "$ui_manifest" "$name" "$check_logs/$step_no.log" "$@" || return 1
}
skip() { echo "SKIP  $1 — $2"; skipped=1; }

if [[ ! -x $py && -x ops/make-venv.sh ]]; then
  ops/make-venv.sh 2>&1   # the same environment the SessionStart hook makes, when it did not run here
fi
if [[ ! -x $py ]]; then
  echo "FAIL  no .venv, and ops/make-venv.sh could not make one: python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt"
  exit 1
fi

# Every suite includes local HTTP tests. Refuse once before a denied bind cascades through them.
$py ops/local/check_runner.py preflight || exit 1
(( preflight_only )) && exit 0
check_logs=$(mktemp -d "${TMPDIR:-/tmp}/work-ops-check.XXXXXX") || exit 1
echo "Check logs: $check_logs (private; retained until you remove them or temporary storage is cleared)"
SECONDS=0
trap 'echo "Checks finished in ${SECONDS}s; logs: $check_logs"' EXIT

database() {
  if [[ -s data/work-ops.db ]]; then echo data/work-ops.db; return; fi
  local dev=data/dev/work-ops.db
  if [[ ! -s $dev || tools/make_fixture.py -nt $dev || -n "$(find workspace/migrations -newer "$dev" -name '*.sql' 2>/dev/null)" ]]; then
    mkdir -p data/dev && rm -f "$dev"
    $py tools/make_fixture.py "$dev" >/dev/null || return 1
  fi
  echo "$dev"
}

check_profile() {
  if [[ -n ${WO_CHECK_PROFILE:-} ]]; then printf '%s\n' "$WO_CHECK_PROFILE"; return; fi
  local user; user=$(id -un 2>/dev/null || true)
  if [[ -d /srv/work-ops || $user == codex || $user == workops ]]; then echo vps; else echo mac; fi
}

pytest_workers() {
  if [[ -n ${WO_PYTEST_WORKERS:-} ]]; then printf '%s\n' "$WO_PYTEST_WORKERS"; return; fi
  if [[ $(check_profile) == vps ]]; then echo 1; return; fi
  $py -c 'import os
try:
    ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
except (ValueError, OSError, AttributeError):
    ram = None
print(1 if ram is not None and ram < 4 * 1024**3 else "auto")'
}

ensure_pytest() {
  $py -c 'import pytest' >/dev/null 2>&1 && return 0
  [[ $(check_profile) == vps ]] && return 1
  $py -m pip install -q -r requirements-dev.txt >/dev/null 2>&1 && $py -c 'import pytest' >/dev/null 2>&1
}

run_pytest() {
  local name=$1
  shift
  local dist=()
  if $py -c 'import xdist' >/dev/null 2>&1; then dist=(-n "$(pytest_workers)"); fi
  step "$name" $py -m pytest "${dist[@]}" -q "$@"
}

for suite in "${suites[@]}"; do
  case $suite in
    unit)
      if ensure_pytest; then run_pytest 'Python tests (tools, workspace, ops/local)' --durations=10
      else
        echo "note  pytest is not installed on this host; the same files run under unittest. On a Mac: $py -m pip install -r requirements-dev.txt"
        step 'Python tests (tools, workspace, ops/local)' bash -c "$py -m unittest discover -s tools -p 'test_*.py' && $py -m unittest discover -s workspace -p 'test_*.py' && $py -m unittest discover -s ops/local -p 'test_*.py'"
      fi
      if db=$(database); then
        views=$(mktemp -d); step "views parity (generated from $db)" bash -c "$py tools/refresh_views.py --db '$db' --root '$views' >/dev/null && $py tools/refresh_views.py --check --root '$views'"; rm -rf "$views"
      else echo "FAIL  views parity: could not build a development database"; failed=1; fi ;;
    contract)
      if db=$(database); then
        if ensure_pytest; then WO_CONTRACT_DB="$db" run_pytest "contract on a copy of $db" --durations=5 workspace/check_contract.py
        else
          echo "note  pytest is not installed on this host; the contract runs under unittest"
          step "contract on a copy of $db" env WO_CONTRACT_DB="$db" $py -m unittest discover -s workspace -p check_contract.py
        fi
      else echo "FAIL  contract: could not build a development database"; failed=1; fi ;;
    worker)
      # A suite that did not run is a skip: --strict (the release gate) fails rather than passing it by.
      if [[ ! -d ops/email-intake/node_modules ]]; then
        skip 'email Worker' 'its dependencies are not installed here; run npm ci --prefix ops/email-intake'
      elif ! command -v npm >/dev/null; then skip 'email Worker' 'npm unavailable'
      elif ! $py ops/local/check_runner.py node-deps ops/email-intake; then failed=1
      else step 'email Worker types and tests' bash -c 'npm --prefix ops/email-intake run -s typecheck && npm --prefix ops/email-intake test --silent'
      fi ;;
    ui|visual)
      if ! command -v node >/dev/null; then
        skip 'browser smoke' 'node unavailable'
        continue
      fi
      if [[ ! -d tests/ui/node_modules/playwright-core ]]; then
        if ! command -v npm >/dev/null || ! npm ci --prefix tests/ui --prefer-offline --no-audit --no-fund >/dev/null 2>&1; then
          skip 'browser smoke' 'could not install pinned dependencies; run npm ci --prefix tests/ui'
          continue
        fi
      fi
      if ! $py ops/local/check_runner.py node-deps tests/ui; then failed=1; continue; fi
      if [[ $suite == ui ]]; then
        step 'page logic (node:test): status clocks, evidence, templates, week brief, preparation' node --test tests/ui/*.test.mjs
      fi
      export WO_UI_ARTIFACT_DIR=${WO_UI_ARTIFACT_DIR:-"$check_logs/browser-artifacts"}
      ui_jobs=${WO_UI_JOBS:-$($py ops/local/check_runner.py browser-jobs "$(check_profile)")}
      if [[ $ui_jobs != 1 && $ui_jobs != 2 ]]; then
        echo 'WO_UI_JOBS must be 1 or 2' >&2; exit 2
      fi
      if [[ $(check_profile) == vps && $ui_jobs != 1 ]]; then
        echo 'the shared VPS permits only one browser suite at a time' >&2; exit 2
      fi
      # The interruption probe and comparisons intentionally share the baseline lock.
      [[ $suite == visual ]] && ui_jobs=1
      # WebKit's macOS app registers with the window server as it starts. An agent seatbelt
      # (Cursor: CURSOR_SANDBOX=seatbelt, Codex: CODEX_SANDBOX=seatbelt) makes that abort, and
      # macOS writes a crash report for every helper. Do not launch Chromium or WebKit from here.
      if [[ ${OSTYPE:-} == darwin* && ( ${CURSOR_SANDBOX:-} == seatbelt || ${CODEX_SANDBOX:-} == seatbelt ) ]]; then
        echo "FAIL  browser checks were not started: this command is inside an agent command sandbox"
        echo "      WebKit aborts while registering as a macOS application and leaves crash reports."
        echo "      Re-run ops/check.sh outside the sandbox through the client's normal permission request. No browser was launched."
        failed=1
        continue
      fi
      # The browsers the pinned playwright-core has here. When its own Chromium build is missing, a
      # pre-installed one runs the desktop half: $WO_UI_CHROMIUM, or $PLAYWRIGHT_BROWSERS_PATH/chromium
      # (a cloud session). WebKit has no stand-in, so the iPhone half then reports SKIP, which strict
      # mode (the deployment gate) still rejects.
      probe='const {chromium,webkit}=require("playwright-core");const fs=require("fs");console.log([["chromium",chromium],["webkit",webkit]].filter(([,b])=>fs.existsSync(b.executablePath())).map(([n])=>n).join(" "))'
      have=$(cd tests/ui && node -e "$probe" 2>/dev/null)
      chromium_at=${WO_UI_CHROMIUM:-}
      if [[ -z $chromium_at && " $have " != *" chromium "* && -n ${PLAYWRIGHT_BROWSERS_PATH:-} && -x $PLAYWRIGHT_BROWSERS_PATH/chromium ]]; then
        chromium_at=$PLAYWRIGHT_BROWSERS_PATH/chromium
      elif [[ -z $have && -z $chromium_at && -n ${PLAYWRIGHT_BROWSERS_PATH:-} ]]; then
        have=$(cd tests/ui && env -u PLAYWRIGHT_BROWSERS_PATH node -e "$probe" 2>/dev/null)
        [[ -n $have ]] && unset PLAYWRIGHT_BROWSERS_PATH
      fi
      browsers=; label=; webkit_failed=
      if [[ " $have " == *" chromium "* || -n $chromium_at ]]; then browsers=chromium; label="desktop Chromium${chromium_at:+ at $chromium_at}"; fi
      if [[ " $have " == *" webkit "* ]]; then
        # An installed WebKit can still fail on a machine where the app itself will not start.
        # The seatbelt case returned above and did not launch it. This probe is the unsandboxed check.
        launch='const {webkit}=require("playwright-core");webkit.launch({timeout:30000}).then(b=>b.close(),e=>{const l=String(e.message).split("\n");const h=l.find(x=>/Abort trap|exitCode=/.test(x));console.log((l[0]+(h?" ("+h.trim()+")":"")).slice(0,300));process.exit(1)})'
        if why=$(cd tests/ui && node -e "$launch" 2>&1); then
          browsers=${browsers:+$browsers,}webkit; label=${label:+$label, }'iPhone WebKit'
        else
          webkit_failed=1; failed=1
          echo "FAIL  iPhone WebKit is installed but did not start: ${why:-no output}"
          echo "      The iPhone check did not run. Desktop Chromium still runs when it is available."
        fi
      fi
      if [[ -z $browsers && -n $webkit_failed ]]; then
        skip 'browser smoke: desktop Chromium' 'unavailable for the installed playwright-core (no browser check ran)'
      elif [[ -z $browsers ]]; then
        skip 'browser smoke' 'Chromium and WebKit unavailable for the installed playwright-core (no browser check ran)'
      else
        # The browser contract needs deterministic approval, outcome, route and history cases.
        # ContractTests above still check backup copies of the development data.
        fixture_dir=$(mktemp -d)
        db="$fixture_dir/ui-fixture.db"
        if [[ $suite == visual ]] || $py tools/make_fixture.py "$db" >/dev/null; then
          ui_manifest="$check_logs/browser-stages-$step_no.jsonl"
          echo "Browser suites: $ui_jobs at a time; independent databases, ports and logs"
          queue_failed=0
          if [[ $suite == visual ]]; then
            queue_step "visual interruption cleanup (synthetic Chromium)" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/visual-cleanup-check.mjs || queue_failed=1
            queue_step "visual comparisons ($label) against reviewed synthetic baselines" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/visual-regression.mjs || queue_failed=1
          else
            queue_step "browser failure evidence ($label) on synthetic pages" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/browser-evidence-check.mjs || queue_failed=1
            queue_step "browser smoke ($label) on synthetic records" env WO_UI_DB="$db" WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/smoke.mjs || queue_failed=1
            queue_step "opportunity workflow, bounded rendering and recovery ($label)" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/opportunity-workflow.mjs || queue_failed=1
            queue_step "user guide walkthrough ($label) on fresh synthetic records" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/user-guide.mjs || queue_failed=1
            queue_step "email delivery status ($label) on synthetic records" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/email-status.mjs || queue_failed=1
            queue_step "deferred email leads ($label) on synthetic records" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/email-leads.mjs || queue_failed=1
            queue_step "research coverage and budget ($label) on synthetic records" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/research-status.mjs || queue_failed=1
            queue_step "job contacts and availability checks ($label) on fresh synthetic records" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/job-evidence.mjs || queue_failed=1
            queue_step "People sending batches, templates and recovery ($label)" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/sending.mjs || queue_failed=1
            queue_step "incoming roles and existing hiring processes ($label)" env WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" node tests/ui/incoming-role.mjs || queue_failed=1
            queue_step "career/ops public pitch ($label)" env WO_UI_DB="$db" WO_UI_BROWSERS="$browsers" WO_UI_CHROMIUM="$chromium_at" SCREENSHOT_DIR="$check_logs/career-captures" node tests/ui/career-ops.mjs || queue_failed=1
          fi
          if (( queue_failed )); then failed=1
          else
            $py ops/local/check_runner.py batch "$ui_jobs" "$ui_manifest"
            code=$?
            (( code == 130 || code == 143 )) && { rm -rf "$fixture_dir"; exit "$code"; }
            (( code == 0 )) || failed=1
          fi
        else echo "FAIL  browser smoke: could not build synthetic records"; failed=1; fi
        rm -rf "$fixture_dir"
        [[ $browsers == *chromium* ]] || skip 'browser smoke: desktop Chromium' 'unavailable for the installed playwright-core (the desktop check did not run)'
        [[ $browsers == *webkit* || -n $webkit_failed ]] || skip 'browser smoke: iPhone WebKit' 'unavailable for the installed playwright-core (the iPhone check did not run)'
      fi ;;
  esac
done
if [[ $strict == 1 && $skipped == 1 ]]; then
  echo 'FAIL  strict checks require every selected suite to run'
  failed=1
fi
# Successful synthetic captures have served their assertions; keep the small text receipts.
# Failures/skips retain evidence for diagnosis. This path is owned by this invocation only.
if (( failed == 0 && skipped == 0 )); then
  if ! rm -rf -- "$check_logs/career-captures"; then
    echo "FAIL  could not remove successful captures: $check_logs/career-captures" >&2
    failed=1
  fi
fi
return $failed
}
main "$@"; exit $?
