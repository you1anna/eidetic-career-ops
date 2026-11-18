#!/usr/bin/env bash
#
# Read-only health check for the work-ops VPS and its public URL. Changes nothing.
# Prints PASS / FAIL / PENDING per check; exits 1 if anything FAILs.
#
# Usage: ops/vps/check.sh [--expect-rev <full-sha>]
# Release mode binds evidence to the requested commit and requires the site to be reachable.
#
set -uo pipefail

SERVER_HOST="${SERVER_HOST:-100.64.0.10}"
SSH_USER="${SSH_USER:-ops}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/work_ops_vps}"
PUBLIC_HOST="${PUBLIC_HOST:-work.example.com}"
PUBLIC_IP="${PUBLIC_IP:-203.0.113.10}"  # the server's internet address; SSH uses the tailnet (SERVER_HOST)
EXPECTED_REV=
if [[ $# != 0 ]]; then
  if [[ $# != 2 || "$1" != --expect-rev || ! "$2" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
    echo 'Usage: ops/vps/check.sh [--expect-rev <full-sha>]' >&2; exit 2
  fi
  EXPECTED_REV="$2"
fi

pass=0; fail=0; pending=0
ok()   { printf '  \033[32mPASS\033[0m    %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m    %s\n' "$*"; fail=$((fail+1)); }
todo() { printf '  \033[33mPENDING\033[0m %s\n' "$*"; pending=$((pending+1)); }
unavailable() { if [[ -n "$EXPECTED_REV" ]]; then bad "$*"; else todo "$*"; fi; }

echo "Server $SERVER_HOST"
remote=$(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 "$SSH_USER@$SERVER_HOST" 'bash -s' <<'REMOTE' 2>/dev/null
cd /tmp
for u in work-ops-workspace.service wo-scan.timer wo-backup.timer cloudflared.service; do echo "unit:$u $(systemctl is-active "$u" 2>/dev/null)"; done
DB=/srv/work-ops/data/work-ops.db
# Read-only URI: a plain connect() would CREATE the file and then report a healthy empty database.
if sudo -n -u workops test -s "$DB"; then
  echo "db $(sudo -n -u workops /srv/work-ops/venv/bin/python -c "import sqlite3,sys;print(sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True).execute('pragma integrity_check').fetchone()[0])" "$DB" 2>/dev/null || echo unreadable)"
else
  echo "db absent"
fi
echo "backupage $(sudo -n -u workops find /srv/work-ops/data/backups -name 'work-ops-*.db' -mtime -2 2>/dev/null | wc -l)"
echo "scanage $(sudo -n -u workops find /srv/work-ops/data/pending -name 'last-run.json' -mtime -2 2>/dev/null | wc -l)"
echo "fetchage $(sudo -n -u workops find /srv/work-ops/data/logs -name 'fetch.jsonl' -mtime -2 2>/dev/null | wc -l)"
echo "codedirty $(sudo -n -u workops git -C /srv/work-ops/work-ops status --porcelain --untracked-files=no 2>/dev/null | wc -l)"
echo "coderev $(sudo -n -u workops git -C /srv/work-ops/work-ops rev-parse HEAD 2>/dev/null)"
echo "healthrev $(curl -s http://127.0.0.1:8769/api/health | sed -n 's/.*"revision": *"\([0-9a-f]*\)".*/\1/p')"
echo "backupresult $(sudo -n journalctl -u wo-backup.service -q -o cat --no-pager 2>/dev/null | grep -E '^(Finished|Failed to start) ' | tail -1 | cut -c1-8)"
echo "backupat $(sudo -n -u workops cat /srv/work-ops/data/backups/last-backup.json 2>/dev/null | sed -n 's/.*"at": *"\([^"]*\)".*/\1/p')"
echo "listeners $(ss -ltnH | awk '{print $4}' | grep -v -E '^(127\.|\[::1\])' | sort -u | tr '\n' ' ')"
echo "rootlogin $(sudo -n sshd -T 2>/dev/null | awk '/^permitrootlogin /{print $2}')"
# Each allow rule as profile, or profile@interface for a rule limited to one interface.
echo "ufw $(sudo -n ufw status | awk '/ALLOW/{r = $1; for (i = 2; i < NF && $i != "ALLOW"; i++) if ($i == "on") r = r "@" $(i + 1); print r}' | sort -u | tr '\n' ' ')"
# The summary the page loads; nothing on the site requests the much heavier full state.
echo "local $(curl -s -m 60 -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8769/api/state?view=summary')"
# Memory kills since the last deploy (deploy.sh rewrites the unit file), looking back two days at most.
since=$(( $(stat -c %Y /etc/systemd/system/work-ops-workspace.service 2>/dev/null || echo 0) )); floor=$(( $(date +%s) - 172800 ))
if journal=$(sudo -n journalctl -u work-ops-workspace.service --since "@$(( since > floor ? since : floor ))" -q -o cat --no-pager 2>/dev/null); then
  echo "oomkills $(grep -c "Failed with result 'oom-kill'" <<<"$journal")"
else echo "oomkills"; fi
echo "reboot $([ -e /var/run/reboot-required ] && echo pending || echo none)"
echo "python $(sudo -n -u workops /srv/work-ops/venv/bin/python -c 'import sys,yaml;print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo missing)"
echo "chkey $(sudo -n test -s /etc/credstore.encrypted/COMPANIES_HOUSE_API_KEY && echo present || echo absent)"
echo "feedkeys $(for n in ADZUNA_APP_ID ADZUNA_APP_KEY REED_API_KEY; do sudo -n test -s /etc/credstore.encrypted/$n && echo -n 1; done)"
# Credential decryption is a separate explicit, service-scoped check (check-credentials.sh).
# Routine observation must not instantiate a unit that decrypts every stored secret.
echo "plainenv $(sudo -n test -e /srv/work-ops/ingest.env && echo present || echo absent)"
echo "webcreds $(systemctl show work-ops-workspace.service -p InaccessiblePaths --value 2>/dev/null | grep -c /run/credentials)"
echo "legacy $(systemctl list-unit-files --no-legend hub-sync.timer docker.service containerd.service 2>/dev/null | awk '$2=="enabled"{print $1}' | tr '\n' ' ')"
echo "journalcap $(sudo -n test -s /etc/systemd/journald.conf.d/20-work-ops.conf && echo present || echo absent)"
REMOTE
)
val() { awk -v k="$1" '$1==k{ $1=""; sub(/^ +/,""); print; exit }' <<<"$remote"; }

if [[ -z "$remote" ]]; then
  bad "SSH as $SSH_USER failed — server down, key missing, or firewall changed"
else
  [[ "$(val python)" == 3.12 ]] && ok "venv Python 3.12 with PyYAML" || bad "venv python: $(val python) (ops/HANDOVER.md, Prepare the VPS)"
  [[ -z "$(val legacy)" ]] && ok "no legacy hub-sync or Docker units enabled" || bad "legacy units enabled: $(val legacy)"
  predb=0; [[ "$(val db)" == absent ]] && predb=1
  if [[ "$(val unit:work-ops-workspace.service)" == active ]]; then ok "workspace service running"
  elif [[ $predb == 1 ]]; then unavailable "workspace service not installed yet (deploy.sh at cutover)"
  else bad "workspace service not running (systemctl status work-ops-workspace)"; fi
  # Before the cutover the store and its jobs do not exist yet; that is pending, not broken.
  if [[ "$(val db)" == absent ]]; then
    unavailable "no database yet — supply the approved local snapshot with --local-approved --cutover --database"
  else
    [[ "$(val db)" == ok ]] && ok "database integrity ok" || bad "database: $(val db)"
    if [[ "$(val unit:wo-scan.timer)" == active ]]; then
      ok "scan timer running (separately approved automation)"
      [[ "$(val scanage)" -ge 1 ]] && ok "scan ran in the last 2 days" || todo "approved scan has not run in the last 2 days"
      [[ "$(val fetchage)" -ge 1 ]] && ok "fetch log written in the last 2 days (SSH wo fetch-log)" || todo "no fetch log in the last 2 days (ingest and wo fetch write it from 28 Sep)"
    else
      bad "scan timer disabled — the approved daily automation is off (deploy.sh --local-approved --enable-automation)"
    fi
    if [[ "$(val unit:wo-backup.timer)" == active ]]; then
      ok "backup timer running (separately approved automation)"
      [[ "$(val backupage)" -ge 1 ]] && ok "backup newer than 2 days" || todo "approved backup has not run in the last 2 days"
    else
      bad "backup timer disabled — the approved daily automation is off (deploy.sh --local-approved --enable-automation)"
    fi
    coderev=$(val coderev); healthrev=$(val healthrev)
    [[ "$(val codedirty)" == 0 ]] && ok "code checkout has no local changes" || bad "code checkout has local changes (only deploy.sh moves it)"
    if [[ -z "$healthrev" ]]; then unavailable "the running service does not report its commit yet (deployed before 28 Sep)"
    elif [[ -n "$EXPECTED_REV" && ( "$healthrev" != "$EXPECTED_REV" || "$coderev" != "$EXPECTED_REV" ) ]]; then
      bad "expected release ${EXPECTED_REV:0:7}; service reports ${healthrev:0:7}, checkout ${coderev:0:7}"
    elif [[ "$healthrev" == "$coderev" ]]; then
      # The same paths as ops/session-start.sh: a plans or docs commit needs no deploy.
      behind=$(git -C "$(dirname "$0")/../.." rev-list --count "$coderev..origin/main" -- tools workspace config ops tests requirements.txt 2>/dev/null || echo "?")
      ok "serving ${coderev:0:7}, the checked-out commit; ${behind} code commit(s) on origin/main not deployed"
    else bad "service reports ${healthrev:0:7} but the checkout is ${coderev:0:7} (restart or redeploy)"; fi
    case "$(val backupresult)" in
      Finished) ok "last backup job run succeeded (local copy and encrypted copy)" ;;
      Failed*) bad "last backup job run failed: SSH sudo journalctl -u wo-backup -n 40" ;;
      *) todo "the backup job has no recorded run" ;;
    esac
    backupat=$(val backupat)
    [[ -n "$backupat" ]] && ok "last backup receipt $backupat" || todo "no backup receipt yet (written by the nightly backup from 28 Sep)"
  fi
  if [[ "$(val local)" == 200 ]]; then ok "workspace answers on loopback"
  elif [[ $predb == 1 ]]; then unavailable "workspace not serving yet (cutover)"
  else bad "workspace loopback returned $(val local)"; fi
  case "$(val oomkills)" in
    0) ok "workspace not killed for memory since the last deploy (2 days at most)" ;;
    '') bad "could not read the workspace journal for memory kills" ;;
    *) bad "workspace killed for memory $(val oomkills) time(s) since the last deploy (journalctl -u work-ops-workspace)" ;;
  esac
  listeners=$(val listeners)
  # Tailscale's own listeners are bound to the tailnet interface (`%tailscale0`), not the internet.
  [[ -n "$listeners" && -z "$(tr ' ' '\n' <<<"$listeners" | grep -v -E ':22$|%tailscale0:' | grep .)" ]] && ok "only SSH listens publicly" || bad "public listeners: $listeners"
  case "$(val ufw)" in
    OpenSSH@tailscale0) ok "firewall allows SSH on the tailnet only" ;;
    OpenSSH) todo "firewall still allows SSH from the internet (ops/vps/harden.sh ssh-tailnet)" ;;
    *) bad "firewall allows: $(val ufw)" ;;
  esac
  [[ "$(val rootlogin)" == no ]] && ok "root SSH login disabled" || bad "root SSH login: $(val rootlogin)"
  [[ "$(val reboot)" == none ]] && ok "no reboot pending" || todo "reboot pending after updates (sudo systemctl reboot)"
  [[ "$(val plainenv)" == absent ]] && ok "no plaintext key file on the server" || bad "/srv/work-ops/ingest.env exists: move its keys to the encrypted store (ops/vps/send-credential.sh), then delete it"
  if [[ "$(val webcreds)" -ge 1 ]]; then ok "web server cannot read the daily run's keys"
  elif [[ "$(val chkey)" == present ]]; then bad "work-ops-workspace.service can reach /run/credentials (redeploy its unit)"
  else todo "web server unit predates the credential guard (next deploy installs it)"; fi
  [[ "$(val chkey)" == present ]] && ok "Companies House key held encrypted, for the daily run only" || todo "Companies House key not installed (ops/vps/send-credential.sh COMPANIES_HOUSE_API_KEY)"
  case "$(val feedkeys)" in
    111) ok "Adzuna and Reed keys held encrypted, for the daily run only" ;;
    "") todo "Adzuna and Reed keys not installed (ops/vps/send-credential.sh, three names)" ;;
    *) bad "only some of the Adzuna and Reed keys are installed: the feeds will not run" ;;
  esac
  [[ "$(val unit:cloudflared.service)" == active ]] && ok "tunnel connector running" || unavailable "tunnel connector not installed/running (HANDOVER.md)"
  [[ "$(val journalcap)" == present ]] && ok "system journal capped at 300 MB" || todo "system journal uncapped (ops/vps/harden.sh journal)"
fi

echo "Public $PUBLIC_HOST"
if [[ -z "$(dig +short "$PUBLIC_HOST" 2>/dev/null)" ]]; then
  unavailable "no DNS record yet (HANDOVER.md)"
else
  for path in / /api/state /api/status /api/dashboard /api/email-leads; do
    read -r code location < <(curl -s -m 15 -o /dev/null -w '%{http_code} %{redirect_url}\n' "https://$PUBLIC_HOST$path")
    if [[ "$code" =~ ^30[12]$ && "$location" == *cloudflareaccess.com* ]]; then ok "$path requires Access login"
    elif [[ "$code" == 200 ]]; then bad "$path is OPEN without login — take offline: sudo systemctl stop cloudflared"
    elif [[ "$code" =~ ^(502|530)$ ]]; then unavailable "$path returns $code — tunnel connector not connected"
    else bad "$path returned $code ${location:-}"; fi
  done
fi
# The Mac's daily off-host copy (decision #23): a copy of live data that survives losing the server.
offsite_dir="$(dirname "$0")/../../data/offsite"
if [[ -d "$offsite_dir" ]]; then
  echo "This Mac"
  newest=$(ls -1t "$offsite_dir"/work-ops-*.db.age 2>/dev/null | head -1)
  if [[ -n "$newest" && -z "$(find "$newest" -mtime +2 2>/dev/null)" ]]; then ok "off-host copy newer than 2 days (${newest##*/})"
  else bad "off-host copy is stale (newest: ${newest##*/}): run ops/offsite-weekly.sh; ~/Library/Logs/work-ops/offsite-error.log"; fi
  # A recent copy is not a restorable one: the daily job's receipt says whether its restore drill passed.
  receipt="$offsite_dir/last-run.json"
  result=$(sed -n 's/.*"result": *"\([a-z]*\)".*/\1/p' "$receipt" 2>/dev/null); at=$(sed -n 's/.*"at": *"\([^"]*\)".*/\1/p' "$receipt" 2>/dev/null)
  if [[ ! -s "$receipt" ]]; then todo "no off-host run receipt yet (ops/offsite-weekly.sh writes one from 15 Oct)"
  elif [[ -n "$(find "$receipt" -mtime +2 2>/dev/null)" ]]; then bad "off-host job has not run for 2 days (last $at): launchctl print gui/$(id -u)/com.macmini.workops-offsite"
  elif [[ "$result" == passed ]]; then ok "restore drill passed on the off-host copy ($at)"
  elif [[ "$result" == defects ]]; then bad "the off-host copy restores, but the data has quality defects ($at): ops/wo-live quality"
  else bad "restore drill failed ($at): ~/Library/Logs/work-ops/offsite-error.log, then rerun ops/offsite-weekly.sh"; fi
else
  # The job is installed per Mac; without its folder here the copy is unverified, not healthy.
  echo "This Mac"
  todo "off-host copy and restore drill not checked: no data/offsite on this Mac (check the Mac that runs com.macmini.workops-offsite; ops/LOCAL.md)"
fi
if curl -s -m 5 -o /dev/null "http://$PUBLIC_IP:8769/"; then bad "workspace port reachable directly on the server IP"; else ok "workspace port not reachable on the server IP"; fi
if nc -z -G 5 "$PUBLIC_IP" 22 >/dev/null 2>&1; then todo "SSH answers on the public IP (ops/vps/harden.sh ssh-tailnet closes it)"; else ok "SSH closed on the public IP"; fi

echo
echo "$pass pass, $fail fail, $pending pending"
[[ $fail -eq 0 ]]
