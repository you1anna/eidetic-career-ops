#!/usr/bin/env bash
# VPS hardening from decision #24, one step at a time. Each step is idempotent, verifies itself and
# undoes its change if the verification fails. It installs files from the deployed commit
# (/srv/work-ops/work-ops), so deploy first.
#
#   ops/vps/harden.sh ssh              X11 and agent forwarding off (sshd drop-in, sshd -t, reload)
#   ops/vps/harden.sh tunnel           cloudflared as its own user in a systemd sandbox
#   ops/vps/harden.sh tunnel-revert    undo the tunnel step
#   ops/vps/harden.sh offsite-key      authorise ~/.ssh/workops_offsite.pub for wo-offsite-fetch only
#   ops/vps/harden.sh journal          cap the system journal at 300 MB, about four weeks (15 Oct 2026)
#   ops/vps/harden.sh journal-revert   back to systemd's default cap (10% of the disk, about 3 GB)
#   ops/vps/harden.sh ssh-tailnet      SSH from the tailnet only (21 Oct 2026); restores itself in
#                                      10 minutes unless a fresh tailnet login from this Mac succeeds
#   ops/vps/harden.sh ssh-tailnet-revert  allow SSH from the internet again
#   ops/vps/harden.sh workspace-loopback  refuse local access to 127.0.0.1:8769 except from the tunnel
#                                      and the service/admin accounts (so nothing bypasses Access)
#   ops/vps/harden.sh workspace-loopback-revert  remove that loopback restriction
set -euo pipefail
SERVER_HOST="${SERVER_HOST:-100.64.0.10}"
SSH_USER="${SSH_USER:-ops}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/work_ops_vps}"
step="${1:?usage: ops/vps/harden.sh ssh|tunnel|tunnel-revert|offsite-key|journal|journal-revert|ssh-tailnet|ssh-tailnet-revert|workspace-loopback|workspace-loopback-revert}"
PUBLIC_IP="${PUBLIC_IP:-203.0.113.10}"
RESTORE=wo-ssh-public-restore
if [[ "$step" == ssh-tailnet && "$SERVER_HOST" != 100.* ]]; then
  echo "ssh-tailnet must run over the tailnet (SERVER_HOST=$SERVER_HOST)" >&2; exit 2
fi
pub=""
if [[ "$step" == offsite-key ]]; then
  [[ -r "$HOME/.ssh/workops_offsite.pub" ]] || ssh-keygen -q -t ed25519 -N '' -C 'work-ops offsite (Mac)' -f "$HOME/.ssh/workops_offsite"
  pub="$(cat "$HOME/.ssh/workops_offsite.pub")"
fi
# ssh joins arguments with spaces and drops empty ones, so quote them for the remote shell.
ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$SSH_USER@$SERVER_HOST" "sudo bash -s $(printf '%q ' "$step" "$pub")" <<'REMOTE'
set -euo pipefail
step="$1"; pub="${2:-}"
CODE=/srv/work-ops/work-ops
DROPIN=/etc/systemd/system/cloudflared.service.d/work-ops.conf
creds() { awk '/^credentials-file:/{print $2}' /etc/cloudflared/config.yml; }
tunnel_ok() {  # 4 connections registered since $1, and an anonymous request still meets Access
  local since=$1 n code
  for _ in $(seq 1 30); do
    n=$(journalctl -u cloudflared --since "$since" -o cat --no-pager | grep -c 'Registered tunnel connection' || true)
    [[ "$n" -ge 4 ]] && break; sleep 1
  done
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' https://work.example.com/api/state || true)
  echo "tunnel: $n connections, anonymous /api/state -> $code"
  [[ "$n" -ge 4 && "$code" == 302 ]]
}
tunnel_revert() {
  rm -f "$DROPIN"; rmdir --ignore-fail-on-non-empty "$(dirname "$DROPIN")" 2>/dev/null || true
  chgrp root "$(creds)"; chmod 600 "$(creds)"
  systemctl daemon-reload; local since; since=$(date '+%F %T'); systemctl restart cloudflared
  tunnel_ok "$since" && echo "tunnel: reverted to root, running" || echo "tunnel: reverted but not healthy; see journalctl -u cloudflared" >&2
}
case "$step" in
  ssh)
    install -m 644 "$CODE/ops/vps/sshd-work-ops.conf" /etc/ssh/sshd_config.d/20-work-ops.conf
    if ! sshd -t; then rm -f /etc/ssh/sshd_config.d/20-work-ops.conf; echo "sshd -t failed; drop-in removed" >&2; exit 1; fi
    systemctl reload ssh
    effective=$(sshd -T | awk '/^(x11forwarding|allowagentforwarding) /{print $1 "=" $2}' | sort | xargs)
    [[ "$effective" == "allowagentforwarding=no x11forwarding=no" ]] || { echo "ssh: drop-in not effective ($effective)" >&2; exit 1; }
    echo "ssh: X11 and agent forwarding off"
    ;;
  tunnel)
    id cloudflared >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin cloudflared
    chgrp cloudflared "$(creds)"; chmod 640 "$(creds)"
    install -d -m 755 "$(dirname "$DROPIN")"
    install -m 644 "$CODE/ops/vps/cloudflared-hardening.conf" "$DROPIN"
    systemctl daemon-reload
    since=$(date '+%F %T'); systemctl restart cloudflared
    if tunnel_ok "$since" && [[ "$(systemctl show -p User --value cloudflared)" == cloudflared ]]; then
      echo "tunnel: running as cloudflared; $(systemd-analyze security cloudflared.service 2>/dev/null | tail -1)"
    else
      echo "tunnel: not healthy as cloudflared; reverting" >&2; tunnel_revert; exit 1
    fi
    ;;
  tunnel-revert) tunnel_revert ;;
  offsite-key)
    [[ "$pub" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ [^\"]*)?$ ]] || { echo "not an ed25519 public key" >&2; exit 2; }
    ak=/home/ops/.ssh/authorized_keys
    key="${pub#ssh-ed25519 }"; key="${key%% *}"
    if grep -qF "$key" "$ak"; then echo "offsite key: already authorised"
    else echo "restrict,command=\"/usr/local/bin/wo-offsite-fetch\" $pub" >> "$ak"; echo "offsite key: authorised for wo-offsite-fetch only"; fi
    ;;
  journal)
    # Inline rather than from the deployed commit: a journald limit does not depend on the site's code.
    install -d -m 755 /etc/systemd/journald.conf.d
    printf '%s\n' '# work-ops: ops/vps/harden.sh journal. About four weeks of logs at ~10 MB a day.' '[Journal]' 'SystemMaxUse=300M' \
      > /etc/systemd/journald.conf.d/20-work-ops.conf
    systemctl restart systemd-journald
    journalctl --vacuum-size=300M -q
    systemctl is-active -q systemd-journald || { echo "journal: journald is not running after the restart" >&2; exit 1; }
    logger -t work-ops "journal capped at 300M"
    for _ in 1 2 3 4 5; do
      [[ "$(journalctl -t work-ops --since '-2min' -q -o cat | grep -c 'journal capped')" -ge 1 ]] && break; sleep 1
    done
    [[ "$(journalctl -t work-ops --since '-2min' -q -o cat | grep -c 'journal capped')" -ge 1 ]] \
      || { echo "journal: journald is not recording after the restart" >&2; exit 1; }
    echo "journal: capped at 300M and recording; $(journalctl --disk-usage)"
    ;;
  journal-revert)
    rm -f /etc/systemd/journald.conf.d/20-work-ops.conf
    systemctl restart systemd-journald
    echo "journal: systemd's default cap restored; $(journalctl --disk-usage)"
    ;;
  ssh-tailnet)
    [[ "$(tailscale ip -4 2>/dev/null)" == 100.64.0.10 ]] && systemctl is-enabled -q tailscaled \
      || { echo "ssh-tailnet: tailscaled is not up as work-ops-vps; nothing changed" >&2; exit 1; }
    systemctl stop wo-ssh-public-restore.timer 2>/dev/null || true
    systemd-run --quiet --unit=wo-ssh-public-restore --on-active=10min /usr/sbin/ufw allow OpenSSH
    ufw allow in on tailscale0 to any app OpenSSH >/dev/null
    if ufw status | grep -qE '^OpenSSH( \(v6\))? +ALLOW'; then ufw delete allow OpenSSH >/dev/null; fi
    ufw status | grep ALLOW
    echo "ssh-tailnet: public SSH closed; it reopens in 10 minutes unless this Mac confirms"
    ;;
  ssh-tailnet-revert)
    systemctl stop wo-ssh-public-restore.timer 2>/dev/null || true
    ufw allow OpenSSH >/dev/null
    ufw status | grep ALLOW
    echo "ssh-tailnet: SSH allowed from the internet again"
    ;;
  workspace-loopback)
    command -v nft >/dev/null || { echo "nft is not installed; cannot guard the loopback port" >&2; exit 1; }
    install -d -m 755 /etc/work-ops
    sed -e "s#@WORKOPS_UID@#$(id -u workops)#" -e "s#@CLOUDFLARED_UID@#$(id -u cloudflared)#" \
        -e "s#@OPS_UID@#$(id -u ops)#" "$CODE/ops/vps/work-ops-loopback.nft" >/etc/work-ops/loopback.nft
    chmod 644 /etc/work-ops/loopback.nft
    nft -c -f /etc/work-ops/loopback.nft || { echo "loopback ruleset failed to validate; nothing changed" >&2; rm -f /etc/work-ops/loopback.nft; exit 1; }
    ufw_before=$(ufw status | head -1)
    install -m 644 "$CODE/ops/vps/work-ops-loopback-guard.service" /etc/systemd/system/work-ops-loopback-guard.service
    systemctl daemon-reload
    systemctl enable --now work-ops-loopback-guard.service
    revert_loopback() {
      systemctl disable --now work-ops-loopback-guard.service >/dev/null 2>&1 || true
      nft delete table inet work-ops-loopback >/dev/null 2>&1 || true
      rm -f /etc/work-ops/loopback.nft /etc/systemd/system/work-ops-loopback-guard.service
      systemctl daemon-reload
    }
    # Verify: the tunnel account and an admin account still reach the site; codex no longer does; the
    # public route through Access is unchanged. Roll the whole step back if any expectation fails.
    cf_code=$(sudo -u cloudflared curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8769/api/health || true)
    workops_code=$(sudo -u workops curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8769/api/health || true)
    codex_code=$(sudo -u codex curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8769/api/health || true)
    tunnel_code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' https://work.example.com/api/state || true)
    # The guard adds one table; UFW's own rules must be exactly as active as before.
    ufw_after=$(ufw status | head -1)
    echo "workspace-loopback: cloudflared=$cf_code workops=$workops_code codex=$codex_code tunnel=$tunnel_code ufw='$ufw_after' (codex expected blocked, tunnel 302, ufw unchanged)"
    if [[ "$cf_code" == 200 && "$workops_code" == 200 && "$codex_code" == 000 && "$tunnel_code" == 302 && "$ufw_after" == "$ufw_before" ]]; then
      echo "workspace-loopback: local access to 127.0.0.1:8769 restricted to the tunnel and service/admin accounts"
    else
      echo "workspace-loopback: expectations not met; reverting" >&2; revert_loopback; exit 1
    fi
    ;;
  workspace-loopback-revert)
    systemctl disable --now work-ops-loopback-guard.service >/dev/null 2>&1 || true
    nft delete table inet work-ops-loopback >/dev/null 2>&1 || true
    rm -f /etc/work-ops/loopback.nft /etc/systemd/system/work-ops-loopback-guard.service
    systemctl daemon-reload
    echo "workspace-loopback: restriction removed; any local account can reach 127.0.0.1:8769 again"
    ;;
  *) echo "unknown step: $step" >&2; exit 2 ;;
esac
REMOTE

if [[ "$step" == ssh-tailnet ]]; then
  # A new connection (not the shared one) proves the tailnet route before the restore is cancelled.
  ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ControlPath=none -o ConnectTimeout=15 \
    "$SSH_USER@$SERVER_HOST" "sudo systemctl stop $RESTORE.timer" \
    || { echo "ssh-tailnet: no fresh tailnet login; public SSH reopens by itself within 10 minutes" >&2; exit 1; }
  echo "ssh-tailnet: a fresh tailnet login works; the automatic restore is cancelled"
  if nc -z -G 5 "$PUBLIC_IP" 22 >/dev/null 2>&1; then echo "ssh-tailnet: port 22 still answers on $PUBLIC_IP" >&2; exit 1; fi
  echo "ssh-tailnet: port 22 closed on $PUBLIC_IP"
fi
