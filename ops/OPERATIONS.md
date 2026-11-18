# work-ops operations

[Documentation index](../docs/README.md)

Status: **hosted since 27 September 2026** at `https://work.example.com` (UpCloud VPS, Cloudflare
Tunnel and Access). The runbook and record are in HANDOVER.md; local development is in
[LOCAL.md](LOCAL.md).

[The system map](../docs/system-map.md) shows the live data flow, local/remote responsibilities,
security boundaries as checked on 9 October. Current release checks are described below.

## Current boundary

- The live database is `/srv/work-ops/data/work-ops.db` on the VPS. Use `ssh ops@work-ops-vps wo …`
  (agents: `ops/wo-live …`, enforced by `ops/local/agent_guard.py`);
  the Mac's `data/work-ops.db` is a development copy.
- The server listens on `127.0.0.1:8769` only; `cloudflared` publishes that one hostname and validates
  the Access token. UFW allows SSH only. `ops/vps/check.sh` is read-only.
- VPS timers: `wo-scan.timer` (daily about 05:30 UTC: free sources, decision #18, plus the approved
  Companies House source, #29, and Adzuna/Reed feeds, #39; their credentials were installed and
  verified on 1 October, and each group runs only while its required credentials are available) and
  `wo-backup.timer` (02:15 UTC: a local copy with 3 kept, an age-encrypted copy in
  `/srv/offsite/work-ops` with 30 kept, and a receipt `data/backups/last-backup.json` the dashboard
  reads; it runs no git). The timers specify UTC; the worker processes use Europe/London time.
- The site runs from `/srv/work-ops/work-ops`, detached at the commit `deploy.sh` last shipped;
  `/api/health` names it. Nothing else moves that checkout: a push to `main` reaches the site only
  through a deploy.
- The Mac: `com.macmini.workops-offsite` (daily 07:30, #23; launchd runs a missed time on wake) keeps the VPS's newest encrypted backup
  in `data/offsite/` (8 kept) and runs `ops/restore-drill.sh`. The private key is
  `~/.config/work-ops/backup.agekey`, also in the owner's password manager.
- The owner's data is never committed (decision #28). The VPS's GitHub deploy key is read-only: it fetches
  code for deploys and never pushes. Browser tests and cloud checkouts use `tools/make_fixture.py`;
  contract tests can also use a read-only backup of a development database, migrating only that copy.
- There is no GitHub Actions workflow (the old scan workflow is in `archive/github/`).
- LinkedIn alert emails use `wo-email.timer` every 15 minutes. Gmail selectively forwards to
  `alerts@example.com`; a private Cloudflare D1 buffer holds mail until the VPS confirms
  durable receipt. `wo-email.service` parses/imports independently of the Mac and general scan.
  **Dashboard → LinkedIn email processing** displays actual outcomes and delivery timestamps.
  `WO_EMAIL_BUFFER_TOKEN` is encrypted for this service only. Nightly encrypted email archives
  retain spool and reader state together on the VPS; the existing Mac pull still copies only the
  site database backup. See [the runbook](../docs/linkedin-alerts.md#backup-and-restore).

## Dependencies (one set on every host)

| Component | Local development | VPS |
| --- | --- | --- |
| Python | `.python-version` (3.12), `.venv`; bootstrap with `ops/make-venv.sh` | same Python minor version (uv, `/opt/uv/python`), `/srv/work-ops/venv` |
| Python packages | Runtime pins in `requirements.txt` (PyYAML and pypdf); pytest/xdist in `requirements-dev.txt` for development | Runtime pins only, installed by `deploy.sh` |
| SQLite | Python's bundled `sqlite3`; exercised by strict tests | Python's bundled `sqlite3`; exercised by `ops/vps/verify.sh` |
| age | Installed for off-host backup decryption | Installed for encrypted backups; restore drill verifies compatibility |
| Node + Playwright | UI smoke tests, and `wo fetch`'s browser step | not installed: `wo fetch` there reads directly only |

`.python-version` pins 3.12. Rebuild a venv with `python3.12 -m venv .venv` (Mac) or as in
HANDOVER.md (VPS), then install `requirements.txt`.
The decision/outreach engine adds no Python dependency: its YAML configuration uses the existing
PyYAML pin, while SQLite, HTTP serving and gzip compression use the standard library. Node and
Playwright remain local verification tools.

## Repeatable verification before deployment

Run `ops/check.sh --strict` locally, including both browser engines. Then run
`ops/vps/verify.sh` to test committed `HEAD` with the VPS's existing Python runtime.
`ops/vps/verify.sh --list` previews the exact archive contents without connecting.
The current transfer list includes all committed files under `workspace`, `tools` and `config`,
including the decision-intelligence engine, outreach JavaScript module, problem playbooks,
migration 007 and their tests. Check that list after committing new files; uncommitted files are
not part of the candidate.

The remote verifier sends only the selected committed code, tests and configuration, excluding
career records, evidence, provenance, exported data, credentials and Git history. Uncommitted
changes are explicitly excluded. It creates a private temporary directory under
`/srv/work-ops/run`, builds synthetic databases, runs strict unit/contract checks and shell syntax
checks, and removes its directory on normal success or failure. Contract copies are migrated before
serving them; the local browser suite always uses fresh synthetic records. It does not install dependencies,
deploy code, restart services or use the production database. Browser tests still run locally.
`SERVER_HOST`, `SSH_USER` and `SSH_KEY` support the same overrides as the health checker.

A failed SSH session or server shutdown can leave scratch storage behind. Inspect only
`/srv/work-ops/run/verify-candidate.*` directories for abandoned verification runs before removing
them; do not remove a directory belonging to an active run. If a future change needs a new source
path, review and extend the verifier's explicit transfer list before using it.

Use `ops/vps/check.sh` separately to check production health. Passing candidate tests does not
mean that candidate is deployed; compare its printed revision with `/api/health`.

## Deploying

`ops/vps/deploy.sh --local-approved` ships the local HEAD, which must be pushed:
1. `ops/check.sh --strict` runs here. Failed or unavailable checks stop deployment before SSH;
   `--skip-checks` is for emergencies only. Browser setup is in [LOCAL.md](LOCAL.md).
2. On the VPS, before the running site changes: the commit must be on origin, the code checkout
   clean, Python 3.12, and the database schema must match the commit's migrations. A new migration needs `--migrate`
   (it stops the site, backs up to `data/backups/work-ops-pre-migrate-*.db`, migrates and starts it);
   a database newer than the code (a rollback past a migration) is refused.
3. It waits for a running ingest or backup (the writer lock), pins the checkout to the commit,
   installs the units and the `wo` wrapper, restarts the site, and confirms `/api/health` reports the
   commit.
4. The scan and backup timers stay as they are; `--enable-automation` and `--disable-automation`
   change them. (Until 27 Sep a deploy without the flag switched them off.)
5. `ops/vps/check.sh` observes production health, then separate scan/email credential checks verify
   decryption in short-lived, network-isolated units. Routine health checks do not decrypt secrets.

Check first with `--local-approved --dry-run`. Preflight prepares deployment directories, a missing deploy key/checkout,
refreshes Git refs and cleans the temporary cutover artifact. It leaves the running site, database
schema and timers unchanged, and does not run the local test suites. Roll back with `--rev <sha>`:
the current script deploys that commit; a commit from before the pinned-checkout layout (28 Sep)
is refused. The first deploy on a new VPS
needs `--cutover --database /absolute/path/to/approved.db` and refuses to replace an existing hosted
database. `--local-approved` confirms the deploy is intended; gates such as new schedules or
infrastructure changes still need the owner's answer on the site.

The decision and outreach release adds `007_decision_integrity.sql`; use `--migrate` for its
preflight and deployment. It preserves draft versions and records outcome provenance. Earlier
draft approvals retain their history but need confirmation of the current saved text when the
historically approved version cannot be proved. Applying the migration does not send outreach or
execute decisions.

Never copy a live SQLite `.db` file alone; its WAL may hold committed pages. Use `wo backup`.

## Keys for the daily run

A keyed source runs on the VPS only when the owner approved it by name (`KEYED` in `tools/ingest.py`):
Companies House (#29) and the Adzuna and Reed feeds (#39, 1 October). LinkedIn mail uses the
separately approved `wo-email.service` and its own buffer token, not these daily-run keys. A key is held with
systemd's credential store, not a file the services can read:

- `ops/vps/send-credential.sh COMPANIES_HOUSE_API_KEY` (on the Mac) pipes the value from `.env` to
  `sudo put-credential` on the VPS, never printing it or putting it on a command line.
  `put-credential` encrypts it with the host key into `/etc/credstore.encrypted/<NAME>` (root only).
  The sender validates the approved credential name before reading `.env`, and supports the same
  `SERVER_HOST`, `SSH_USER` and `SSH_KEY` overrides as deployment. It requires existing trusted SSH
  access; it does not install the client SSH identity or the server host key.
- `wo-scan.service` decrypts it with `LoadCredentialEncrypted=` into that run's private
  `$CREDENTIALS_DIRECTORY`; `tools/credentials.py` reads it from there. It never enters any process
  environment. `wo-scan.sh` adds `--keyed companies-house` or `--keyed feeds` only when those keys are present.
  It does not collect LinkedIn mail. `wo-email.service` loads only its encrypted buffer token;
  [the email runbook](../docs/linkedin-alerts.md) owns its receipt and recovery procedure.
- `work-ops-workspace.service` runs as the same user, so `/run/credentials` and the credential stores
  are inaccessible to it. There is no `/srv/work-ops/ingest.env`; `ops/vps/check.sh` fails if one appears.
- The key is bound to this server's host key: a rebuilt server needs it sent again.
- `put-credential` prints "credential.secret is not located on encrypted media". That is expected: the
  VPS has no TPM, so the host key (`/var/lib/systemd/credential.secret`, root only) is on the same disk.
  The store keeps keys from the web server, from the `workops` user outside the run and from anyone
  holding only the encrypted file; it does not protect against root on the server or a full disk copy.
- To deliberately verify decryption without printing values, use
  `ops/vps/check-credentials.sh wo-scan.service` or `ops/vps/check-credentials.sh wo-email.service`
  from an authorized Mac after deployment of the checker. Each loads only installed credentials
  declared by that service, reports counts, and refuses unsupported declarations. These checks
  start temporary sandboxed units; they are separate from read-only diagnostics and remain release gates.
  Installed and verified on 1 October: Companies House, Adzuna (id and key) and Reed.

Companies House reads only companies to approach whose number was recorded from a page that states it:
`wo company-number <id> <number> --evidence <url>` (`none` removes a wrong one). It never matches a
name: on 30 September a third of exact-name matches were unrelated companies. To find candidates for
a person to verify: `tools/scan_companies_house.py --propose "<name>"` (stores nothing).

Live-read evidence and request budgets for the keyed providers are in
archive/docs/provider-validation-2026-09-24.md.

## Hardening (decision #24)

`ops/vps/harden.sh <step>` applies one step from the deployed commit, verifies it and undoes it on
failure:
- `ssh`: X11 and agent forwarding off (`/etc/ssh/sshd_config.d/20-work-ops.conf`); TCP forwarding
  stays for `ops/vps/open.sh`. Password and root logins were already off in the base config.
- `tunnel`: `cloudflared` runs as its own `cloudflared` user in a systemd sandbox (drop-in
  `cloudflared.service.d/work-ops.conf`). It checks for 4 tunnel connections and that anonymous
  `/api/state` still gets the Access redirect, else reverts; `tunnel-revert` undoes it by hand.
- `offsite-key`: the Mac's `~/.ssh/workops_offsite` key is authorised with `restrict` and the forced
  command `wo-offsite-fetch`, so it can only name or send an encrypted backup. The daily off-host job
  and `ops/pull-backup.sh` use it, which lets the main key (`~/.ssh/work_ops_vps`) carry a passphrase.
- `journal`: caps the system journal at 300 MB (`/etc/systemd/journald.conf.d/20-work-ops.conf`, written
  inline) and vacuums the rest, then confirms journald still logs. On 14 Oct it held 941 MB from June
  onwards, mostly SSH probes and firewall blocks, growing about 10 MB a day. The owner chose the cap on
  15 Oct; `journal-revert` restores systemd's default (10% of the disk).

SSH to the VPS is key-only (`maxauthtries 6`; root and passwords off; agent and X11 forwarding off).
One key per device, so one can be revoked without the others. Keys authorised for `ops` on
8 October 2026 (rechecked 21 October): the Air's `work_ops_vps` (`SHA256:pW6Izdar…`, also authorised for
`codex`), the restricted off-host key (`SHA256:GiEWgPF5…`), the Mac mini's `work-ops-macmini`
(`SHA256:gIe4HDbW…`), and the
two Termius SSH ID keys published at `sshid.io/example` (comment `#SSH ID - @example`). The iPhone's
Termius signs in with the first of these (`SHA256:examplefingerprintreplacewithyourownkeyAAAAA`);
the second (`SHA256:examplefingerprintreplacewithyourownkeyAAAAA`) has not yet been seen in a login.
Install keys as fixed lines checked by fingerprint, never with `curl … >> authorized_keys`.
If Termius reports "No more authentication methods to try" and the log says "maximum authentication
attempts exceeded", it offered its other keys first: select only the SSH ID passkey on the host.

Both Macs' `work_ops_vps` keys carry passphrases. The Air is configured for macOS Keychain
(`UseKeychain yes`; a login without an agent was recorded on 21 October).
The Mac mini does not, by the owner's choice on 21 October: after reboot/logout the owner loads its key
with `ops/vps-unlock` in a terminal. This asks for the passphrase and proves the VPS answers.
Agents never ask for, store or type the passphrase; it never belongs in `.env` or another file.

`ops/vps-unlock --check` first checks the matching agent key or an unencrypted key, retaining the
Mac's existing Keychain reload. If local readiness cannot be established, a bounded, non-interactive
SSH `true` also allows Keychain-only authentication. Exit 0 means signing readiness or a successful
connection; 2 means a missing key; 3 means rejected authentication with the encrypted key absent
from an accessible agent; 4 means an inconclusive agent/SSH check. Network errors and an inaccessible
agent must not be labelled a locked key. Use the normal scoped permission flow if an agent sandbox
cannot reach the login agent or network; never bypass a denial or weaken global sandbox settings.
Tailscale supplies the private network route; SSH/Keychain supplies authentication.

On 22 October a sandboxed `ssh-add -l` returned “Operation not permitted”; the old checker hid this
and returned “locked”. The same check and SSH `true` succeeded through the normal permission layer
with the VPS key already loaded. The corrected checker and session/status guidance preserve that
distinction. Login-agent access, live gateway reads, deployment and all 32 hosted checks passed.
A separate empty-agent check initially could not reload the VPS key. The owner refreshed the saved
passphrase in his terminal with `ssh-add --apple-use-keychain ~/.ssh/work_ops_vps`; the repeat test
loaded the VPS identity automatically into an isolated empty agent and authenticated a fresh SSH
connection with connection reuse disabled. This verifies fresh-agent recovery without clearing the
login agent; the Mac was not rebooted. Preserve the Mini's separate Keychain choice. No SSH security
setting or Tailscale configuration was changed.

Future SSH/Tailscale changes must verify live gateway reads, deployment preflight, hosted health and
fresh-agent recovery separately. An existing multiplexed connection does not prove reboot recovery.

### Mac SSH

SSH into either Mac matches the VPS (21 October): keys only, root and agent forwarding off.
`ops/mac-ssh` reports a Mac's SSH, tailnet and remote-session state. `ops/mac-ssh setup` (the owner, in a
terminal, on each Mac: its last step uses sudo) does all of the following:

- authorises the other Mac's `work_ops_vps` key and the iPhone's Termius key (`SHA256:5SYs…`), each
  checked by fingerprint, so each Mac uses one key for the VPS and the other Mac. Each line carries
  `from="100.64.0.0/10,192.168.1.0/24,fe80::/10,fd00::/8,127.0.0.1,::1"` (security audit, 23 October):
  sshd listens on every interface, macOS ignores `ListenAddress` for its launchd socket and the Macs
  have global IPv6 addresses, so this is what keeps SSH to the tailnet, the home LAN and loopback.
  A line an older setup wrote without it is replaced. `ops/mac-ssh keys` does only this step;
- records the VPS's tailnet host key after checking it against the pinned fingerprint;
- writes `~/.ssh/work-ops.conf` and includes it first in `~/.ssh/config`, giving `ssh air`,
  `ssh mini`, `ssh work-ops-vps` and `ssh codex-vps`, all over the tailnet (the old public address
  is sent to the tailnet too);
- installs tmux, so a phone session survives a dropped connection (`tmux new -A -s main`);
- runs `ops/mac-ssh remote` (below);
- installs `/etc/ssh/sshd_config.d/010-keys-only.conf`, checks it with `sshd -T`, and confirms that
  a login to itself is offered only `publickey`.

State on 22 October (`ops/mac-ssh` on the Air): the Air has run setup. It offers only `publickey`,
authorises the Mac mini's key and both Termius keys, knows the VPS tailnet host key, has tmux and runs
`com.macmini.claude-remote`. The Mac mini's setup is unverified: it has been offline since 21 October.

### Remote sessions

The owner moves between the Air, the Mac mini and the iPhone, so neither Mac is "the" host. Each Mac runs
the same thing, and the phone uses whichever is awake. `ops/check.sh` is the same on both Macs:
pytest with one worker per core when the machine has at least 4 GiB, installing
`requirements-dev.txt` when pytest is missing.

- `ops/mac-ssh remote` installs the LaunchAgent `com.macmini.claude-remote`. It keeps
  `claude remote-control` running in `~/Projects` under `caffeinate -s`, which holds off sleep on
  mains power only. A Mac that is plugged in therefore shows in the Claude app (named after its host)
  and can start new sessions there; on battery it sleeps as usual. Errors go to
  `~/Library/Logs/claude-remote.log`. Undo: `launchctl bootout gui/$(id -u)/com.macmini.claude-remote`
  and delete the plist.
- It also sets Codex's `keepRemoteControlAwakeWhilePluggedIn = true`, the same rule for Codex remote
  control (restart the Codex app to apply).
- Nothing wakes a sleeping Mac over the tailnet, and the Air sleeps with its lid shut. The Mac mini
  is the one to rely on while away, with one gap: the Tailscale Mac app and this LaunchAgent run only
  in a logged-in session (Tailscale's "Three ways to run Tailscale on macOS"), so after a reboot or
  power cut the Mac mini is off the tailnet until someone logs in at it. Automatic login (needs
  FileVault off) or Tailscale's open-source `tailscaled` closes that gap; neither is set up.
- For a shell, Termius on the iPhone, with Tailscale connected, opens `mini`, `air` or `work-ops-vps`.
  `claude` or `codex` started there runs inside tmux.

Remote control and Codex remote travel through Anthropic's and OpenAI's relays, not the tailnet.
Cloud sessions still have no route to the VPS (Cloud sessions in CLAUDE.md).

### Tailscale

The owner's tailnet (`tailnet-example.ts.net`, signed in as owner@) joins the Macs, the VPS and
the iPhone (on the tailnet since 21 October). MagicDNS is on. The tailnet addresses below stay fixed
while a device keeps its registration; scripts and `~/.ssh/work-ops.conf` use them, so a rename in the admin console
breaks nothing.

| Device | Tailnet name | Address |
| --- | --- | --- |
| MacBook Air | `owners-macbook-air` | `100.64.0.21` |
| Mac mini | `owners-mac-mini-1` | `100.64.0.22` |
| VPS | `work-ops-vps` | `100.64.0.10` |
| iPhone | `owner-iphone` | `100.64.0.24` |

The Mac mini's old registration (`owners-mac-mini`, `100.64.0.23`) has been removed. Key expiry is
off for the VPS and the Mac mini; the Air's and iPhone's keys expire on 19 April 2027 (checked
22 October with `tailscale status --json`). Re-authenticate those two before then. The Air and the VPS
ran Tailscale 1.104.2, the current release, on 22 October; the Air's app updates itself.

If `ops/vps-unlock --check` exits 4 with `connect to host 100.64.0.10 port 22: Operation timed out`,
run `tailscale status` on that Mac first. On 3 November the Mac mini's client was stopped; with the owner's
agreement, `tailscale up` restored it and the check then reported the actual state, a locked key (exit 3).

The VPS joined on 21 October from Tailscale's signed apt repository
(`/etc/apt/sources.list.d/tailscale.list`, key `2596 A99E AAB3 3821 893C 0A79 458C A832 957F 5868`)
with `--ssh=false` (logins stay on OpenSSH and its keys) and `--accept-dns=false` (the server's DNS
is unchanged). Every script defaults to its tailnet address. The owner chose tailnet-only SSH on
21 October and closed public SSH the same evening (`ops/vps/check.sh`: SSH on the tailnet only, port 22
closed on the public IP, 31 checks passing). `ops/vps/harden.sh ssh-tailnet`:

- allows SSH on `tailscale0` only and removes the public rule;
- schedules an automatic reopening in 10 minutes, which it cancels only after a fresh tailnet login
  from the Mac succeeds;
- confirms that port 22 no longer answers on 203.0.113.10.

`ops/vps/harden.sh ssh-tailnet-revert` reopens public SSH, and `ops/vps/check.sh` reports which
state the firewall is in. If tailscaled fails on the VPS, the site keeps running through the tunnel,
but SSH needs the UpCloud web console. Undo the tailnet join itself with
`sudo tailscale logout && sudo apt-get remove tailscale`, after reopening public SSH.

On the Macs, the Tailscale app's CLI is not on PATH; `~/.zshrc` aliases `tailscale` to
`/Applications/Tailscale.app/Contents/MacOS/Tailscale`. The agent guard (`ops/local/agent_guard.py`)
treats the VPS's public address, its tailnet address and the `work-ops-vps`/`codex-vps` aliases alike.

## Restricted Mac agent access and diagnostics

**Implemented, not activated.** Implementation and verification ledger.
Approval for deployment and each Mac's key authorization remains separate from implementation.
Until activation, `ops/wo-live` retains its existing administrator transport. No keys are installed
merely by pulling this code or running `ops/agent-access plan`.

The two capabilities have separate Ed25519 keys and root-owned forced commands:

| Capability | Client | Authority |
| --- | --- | --- |
| Live records | `ops/wo-live <command>` | Existing record allowlist, always actor `agent`; revisions/import validation unchanged |
| Operational observations | `ops/wo-diagnostics status`; `ops/wo-diagnostics logs wo-email.service` | Fixed service metadata and classified journal events; no record reads or changes |

Both keys are limited to the activating Mac's exact tailnet IPv4 address. They grant no shell,
PTY, forwarding, arbitrary file reads, credential access, service control or deployment. Restricted
SSH ignores user SSH configuration, does not use the login agent, and disables connection reuse:
an open administrator ControlMaster cannot lend its authority to a restricted request. Strict
host-key checking uses the existing pinned VPS tailnet known-host entry. Missing/revoked keys and
invalid configured routes fail closed. No record mutation is automatically retried.

This narrows **these capabilities**, not every process on the Mac: the administrator identity
remains available under the normal scoped permission layer for explicitly authorized work.
`agent_guard.py` is a Claude hook, not an OS security boundary for other clients. Do not infer that
setting the default route makes a same-user process incapable of requesting administrator access.

### Activate one Mac

1. Review `ops/agent-access plan --device air --source 100.64.0.21`. For the mini use
   `--device mini --source 100.64.0.22`. This prints the proposal and changes nothing.
2. After the owner explicitly approves the release, `/deploy` installs the reviewed helpers from
   independently hash-verified artifacts. It does not authorize new SSH keys or change Mac routes.
3. After the owner explicitly approves this Mac's trust change, run `ops/vps-unlock --check`, then
   `ops/agent-access activate --device air --source 100.64.0.21 --approved` on the Air (substitute
   the mini's arguments when there). The server checks the source matches the actual SSH connection.
   No firewall, sshd, sudoers, schedule or existing key is changed.
4. Activation creates `~/.ssh/workops_live` and `~/.ssh/workops_diagnostics` with private permissions,
   preserves existing keys, appends exact restricted authorizations, verifies positive and negative
   capabilities, forwarding denial and installed helper hashes, then atomically publishes each
   route under `~/.config/work-ops/`. Existing different routes/authorizations cause refusal.
5. Verify `ops/wo-live decision list`, `ops/wo-diagnostics status`, and
   `ops/agent-access verify --device air --source 100.64.0.21`. For production acceptance, keep an
   administrator master open while doing this. Separately verify a fresh administrator login,
   existing offsite retrieval/restore drill and the VPS Codex record route. An unchanged key line
   is preservation evidence, not proof that these separate routes still work.

Local synthetic tests prove connection separation and forwarding denial with a real loopback SSH
server. The Air passed production activation and preserved-access checks on 24 October
(evidence);
Mini activation, reboot recovery and cross-device negative probes remain unverified. The full
administrator identity remains necessary for deployment, explicit credential verification and
attended diagnosis beyond the observation allowlist.

The record gateway intentionally refuses public web `fetch` and remote `--text-file` paths.
Use public-source tools as the calling account (`.venv/bin/python tools/wo.py fetch <url> --via local`),
and supply record text with `--text-file -` over stdin. Research/list uploads keep their existing
dedicated staging transport. This does not grant the service account a network-proxy capability.

### Read the diagnostics

`status` returns versioned JSON with a UTC observation time, fixed unit/timer state and run times,
origin revision, disk/memory capacity, installed-artifact hashes, selected SSH/firewall evidence,
and projected backup/crash metadata. `service_memory` gives each running service's cgroup peak,
its current anonymous/file split and `memory.events` counts (`max` = limit reached and cache
reclaimed, `oom_kill` = a process was killed); the peak alone includes reclaimable file cache.
A finished oneshot run is `absent`: its scan/email receipt records the same figures under `memory`.
It shares the caretaker's unit inventory and existing crash
and backup receipts; it does not run the caretaker, load the database or duplicate incident policy.
Off-host restore evidence remains explicitly unknown here; check `data/offsite/last-run.json` on
the relevant Mac. Unknown/missing fields are not passes. A successful command means observation
completed, not that every service is healthy. Ruleset hashes show observed content, not a fresh
negative-access probe.

`logs <unit>` permits only the workspace, tunnel, scan, backup, email, research, caretaker and
loopback-guard services. It classifies up to 50 journal entries from the last 24 hours into safe
event categories; raw messages, unknown text, tracebacks, environment, mail and model packets are
omitted. `unclassified` counts omitted entries; an empty event list does not prove no errors.
Use an explicitly authorized administrator session when deeper private diagnosis is necessary.

The collector admits one invocation at a time, caps individual commands at five seconds and
256 KiB, files at 64 KiB, and the complete request at 30 seconds. Busy exits 75; refused/unavailable
invocations exit 64. A retry of read-only diagnostics is safe. It creates only a root-owned runtime
lock; it changes no records, services, configuration or schedules and decrypts no credentials.

### Recover or revoke

On an interrupted activation, previous routes remain or individually verified restricted routes
may have been published. Keys can already be authorized even if the connection failed before its
receipt arrived. Inspect with administrator access, then rerun the **same** activation: it is
idempotent. Do not delete route files to work around a failed check or copy private keys between Macs.
The fixed source means re-registering a Mac in Tailscale requires attended reauthorization.

With explicit authority, `ops/agent-access revoke --device air --source 100.64.0.21 --approved`
removes only that device's two exact managed entries, preserving other keys byte for byte. Local
routes are retained so subsequent calls fail closed, not through the full administrator key.
Rerunning approved activation restores the same capabilities. A conflicting authorization is
refused, not silently broadened. Lost-device recovery uses administrator access from another
device or the UpCloud console to remove the two `work-ops-air-*` authorization lines. Never paste
private keys into a chat, command argument, repository or ticket.

## VPS Codex record access

Provisioned 21 October 2026 through the Mac's existing administration route. The owner authorized a dedicated,
restricted identity for attended live record reads and edits through `ops/wo-live`. This authority
does not grant the account a general shell as `ops`, administrator privileges, database/file access,
backup access, deployment or external messaging.

The account's pinned known-host entry and the VPS's root-owned public ED25519 host key both have fingerprint
`SHA256:examplefingerprintreplacewithyourownkeyAAAAA`. A connection with strict host checking reached
SSH authentication but previously failed because the account had no login key. The Air's pending
documentation and account authority were preserved; no working identity existed at inspection.

From `/home/codex/workspaces/work-ops`, use `ops/wo-live <command>`. The same checkout can run
`ops/check.sh`. That run installs nothing and uses one test worker: the machine also serves the
site, which is capped at 512 MB and has been killed for memory. Without pytest the same files run
under unittest. It reads a development database in the checkout, or builds a synthetic one; it does
not open `/srv/work-ops/data/work-ops.db`. The client reads
`~/.config/work-ops/live-gateway.json`: `ops@127.0.0.1`, identity
`/home/codex/.ssh/workops_live`, strict checking against `HostKeyAlias=203.0.113.10`. The dedicated
ED25519 key was generated on the VPS, with public fingerprint
`SHA256:examplefingerprintreplacewithyourownkeyAAAAA`. No Mac key, cookie or credential was copied.
The private key is Codex-owned, mode 600; the route file contains configuration only.

Its entry in `/home/ops/.ssh/authorized_keys` has `from="127.0.0.1",restrict` and the forced command
`sudo -n -u workops --preserve-env=SSH_ORIGINAL_COMMAND /usr/bin/python3 -I /usr/local/lib/work-ops/wo_agent_gateway.py`.
The helper and its directory are root-owned. It parses arguments without a shell, fixes the live
database and `--actor agent`, rejects unknown/abbreviated/global options and only accepts the
record gateway's commands. `fetch` is deliberately excluded: public web research runs as Codex,
not as a privileged network proxy. `fetch-log` remains a bounded record of prior reads.
Drafts and question answers accept only `--text-file -`. Research/list uploads use a plain basename
and stdin, a private service-owned staging folder, a 10 MiB input limit and guaranteed cleanup.
The CLI receives a clean environment, no SSH agent and a 180-second execution timeout.

The key grants no arbitrary `ops` or `workops` shell, sudo command, PTY, forwarding, file transfer,
direct database, backup, credential or deployment access. Codex still has no sudo rights or
filesystem access to `/srv/work-ops`. Existing administrator/offsite keys, service isolation,
sandboxing, approval prompts, schedules and the serving checkout were preserved.

To reproduce provisioning after inspecting current controls, run
`bash ops/vps/provision-agent-gateway.sh` from an authorized Mac with its administration key unlocked.
It verifies the host fingerprint, preserves other keys, generates a missing dedicated key on the
VPS and refuses conflicting route/key authorization. It does not deploy the application or change
sshd settings. Normal reviewed deployment updates the root-owned helper from verified artifacts;
it does not authorize new keys.

To revoke, remove the single `work-ops VPS codex live gateway` line from the `ops` account's
`authorized_keys` using the authorized administration route. Remove the Codex route file and key
only after revocation. Leave all other entries intact. For authentication failures check the route,
public fingerprint, key modes and the exact forced command; never disable strict host checking.

Observed verification: the dedicated identity accepted CLI help and rejected shell/sudo, shell
injection, SFTP, backup, migration, deployment, actor/database overrides (including abbreviation),
arbitrary text-file reads and a loopback fetch with exit 64. Remote and direct TCP forwarding failed
with exit 255. Through the VPS checkout's configured `ops/wo-live`, live `find`, `state`, `show`,
question listing, an explicitly requested person creation and readback succeeded; its created event
reports `actor: agent`. An empty research upload returned accepted/created 0, and private staging
cleanup is covered by the server unit test. Direct Codex access checks for the data, service SSH
and encrypted-credential directories all returned false. The account remains unable to sudo.

Local verification: 19 focused client/server tests passed. `ops/check.sh` ran all 22 suites with
no skips; the workspace suite initially lacked this Mac's pinned `pypdf` dependency. After installing
`requirements.txt`, all 411 workspace tests passed on rerun; the other 21 suites, including desktop
Chromium and iPhone WebKit, passed in the full run. Shell syntax and diff checks passed. The helper
was installed through the authorized provisioning command; no application deployment was run.

The separate scheduled research socket described in [VPS research](../docs/vps-research.md) does
not supply attended gateway access. Do not use it or direct database access to bypass the missing
SSH identity. No application deployment is needed or authorized by this provisioning request.

## Read-only agent access

**Released 4 November (`b8e077d`); no key uses it yet.** the owner's rule is that no bot acts through
his email, his Access sign-in or the site's loopback port. The site treats every client admitted by
Access as the owner, so a bot signed in that way could approve its own decision cards. A bot that acts
for him (first Hermes, planned for this VPS) gets its own key, bound to the read-only forced command
`ops/vps/wo_read_gateway.py`. `/deploy` installs the helper beside the record gateway; a key reaches
it only after the separately approved provisioning below.

| Request | Result |
| --- | --- |
| `brief`, `find`, `show`, `approach`, `quality`, `import-report`, `fetch-log`, `decision list` and `decision show`, `question list` and `question show` | Run with the record gateway's own argument rules, `--actor agent`, the live database and an empty stdin |
| Anything else: `state` (the whole database), every edit, upload, `week`, decision write and question answer; a missing or invalid caller; an installed record gateway too old to support it | Refused, exit 64; nothing runs |
| Another bot read already running for 20 s (`/srv/work-ops/run/agent-read.lock`) | Busy, exit 75. Retry: reads change nothing |

Each request writes one journal line (`sudo journalctl -t wo-read`):
`caller=… command=… status=… ms=…`, never its arguments. The helper is a separate file: it needs
`command_name` from the installed record gateway. After a rollback to an older gateway, every read
is refused, so a rollback cannot widen the key.

**What a bot receives.** It gets record fields, history, interactions, supplied intake text and
document file names, but never document contents. A test pins that last point. Everything a bot
reads also goes to its model provider.

**Capacity (measured 4 November).** Peak memory per read, on the Mac with a migrated copy of the
30 October database (2,729 records, 6,965 state events), two runs each:

| Read | Peak memory | Time |
| --- | --- | --- |
| `approach` | 114 MiB | 2.7 s |
| `quality` | 100 MiB | 1.9 s |
| Every other allowed read | 32–42 MiB | under 0.25 s |
| `state` (excluded) | 118 MiB | 3.4 s |

`fetch-log` was measured with no fetch log present on the copy.

`ops/wo-diagnostics status` at 17:47 UTC showed the VPS with 3.77 GiB of memory, 2.63 GiB
available and no swap; the workspace peak since its 14:08 restart was 211 MiB. The lock adds at
most one such process at a time. The VPS footprint is unverified until observed.

**Boundary for a bot account on this VPS.** The bot's own setup owns this; provisioning verifies it.
- A dedicated OS user. For Hermes this is `hermes` (uid 1002), created 4 November.
- The key is limited to `127.0.0.1`, so the bot's SSH connection must reach sshd over host
  loopback. Hermes's build uses rootless Podman with slirp4netns, which blocks host loopback
  unless that is allowed, so test this route during provisioning.
- No sudo, and no membership of `workops`, `ops`, `codex` or `cloudflared`.
- No access to `/srv/work-ops`, `/home/ops`, `/home/codex` or the credential stores.
- Its own `MemoryMax` and `CPUQuota`.
- Never added to the loopback guard (`work-ops-loopback.nft`): that would let it reach the site as
  The owner.
- Its messaging channel (Signal for Hermes) allows only the owner's number, with no groups and a
  registration lock. Anyone who passes that allowlist can read work-ops through the bot's replies.

**Release check (passed 4 November).** After a release that changes these helpers,
`ops/wo-diagnostics status` shows each artifact as observed and `ops/agent-access verify` passes on
the Air. Between pushing such a change and releasing it, that verify reports a mismatch. That is
expected; do not work around it.

**Provisioning (pending; not built).** It waits for the bot's account to exist, its boundary tests
to pass, and the owner's explicit approval.
- Like the VPS Codex line, the forced command relies on `ops`'s existing sudo to `workops`. This was
  checked after the 4 November release: through sudo a read ran, and `edit` and `state` were
  refused.
- It follows the VPS Codex pattern above (`provision-agent-gateway.sh`): the key is generated as
  that account, and its route is `ops@127.0.0.1` with host alias `203.0.113.10`.
- It adds one `authorized_keys` line:
  `from="127.0.0.1",restrict,command="sudo -n -u workops --preserve-env=SSH_ORIGINAL_COMMAND
  /usr/bin/python3 -I /usr/local/lib/work-ops/wo_read_gateway.py --caller <account>"`.
- The bot gets only `ops/wo-live`, `ops/local/wo_live.py` and `ops/local/ssh_route.py`, taken from
  one commit with `git archive`. Never a full clone: older history still holds private exports.
- Revoke by removing that single line.

## Codex updates

The VPS's Codex CLI runs from root-owned packages in `/usr/local/lib/codex-packages/<version>`, each
with its own AppArmor sandbox profile. The `codex` account links to one of them: `~/.local/bin/codex`
(which research runs), `codex-code-mode-host`, and the daemon's
`~/.codex/packages/app-server-daemon/current`. The boot unit `~/.config/systemd/user/codex-start.service`
names the same version. While `current` points outside Codex's own `releases/` folder, Codex's
self-updater stays off (`is_stable_standalone_release` in the 0.164.0 daemon source). Never run
`codex app-server daemon update`: it installs a codex-writable package and returns the daemon to self-updating.

`/usr/local/sbin/codex-maintenance` (source `tools/codex_maintenance.py`) does every step. `deploy.sh`
installs it from the released commit, checked against the deploying Mac's hash, never Codex's
`~/workspaces/codex-maintenance` draft. Every command needs `sudo` and runs only when the owner runs it.
The same lines work in Termius on the iPhone or in a terminal on either Mac
(`ssh -t work-ops-vps 'cd /tmp; sudo codex-maintenance …'`):

1. `sudo codex-maintenance check` shows the latest release, the links, the running versions and recent
   switches. Codex's daily account task also reports when a release is waiting.
2. `sudo codex-maintenance prepare latest` downloads the official npm package and checks its SHA-512
   against the registry (proof it matches what npm serves, not an independent OpenAI signature). It
   installs the package beside the active one, loads a renamed copy of the reviewed profile and tests
   the sandbox as `codex`. It changes nothing in use and needs 3 GiB free.
3. `sudo codex-maintenance activation-plan VERSION` prints the link and boot-unit changes and applies nothing.
4. `sudo codex-maintenance activate VERSION` re-verifies the package and refuses while any
   `wo-research*` unit is active. It saves the current links and unit (root-only, in
   `/usr/local/lib/codex-packages/activations/`), stops the daemon, switches the links and unit as
   `codex`, starts it, and requires the CLI, app-server and managed versions to agree on a path inside
   the package. On any failure it restores the saved state. Open Codex sessions on the VPS are interrupted.
5. `sudo codex-maintenance rollback` restores the state saved by the last `activate`.

Old packages and profiles are kept; nothing prunes them. A reboot is never needed: the boot unit
follows `activate`. After a reboot, `sudo codex-maintenance verify VERSION` confirms that the active
package's profile loaded. A tool change ships only through a reviewed commit and `/deploy`. Do not reinstall it from
the codex-writable draft.

Verified 24 October: the owner installed Codex's draft, and `prepare latest` prepared 0.164.0, with
`verify` passing; 0.161.0 stayed active. `status` and `activation-plan 0.164.0` from this file ran
read-only against the live host. `activate` and `rollback` are covered by unit tests only until
their first live switch.

Observed 3 November: 0.166.0 is active (prepared 30 October 23:58; the daemon started from it at the
31 October 00:07 boot), and the owner's `sudo codex-maintenance verify 0.166.0` passed. How the switch
was made is not recorded (maintenance ledger).

## OS package updates

Unattended upgrades install only Ubuntu origins (`/etc/apt/apt.conf.d/50unattended-upgrades`), so
Tailscale and cloudflared, from their own repositories, need attended updates. The owner runs them from
a Mac terminal. An agent's `sudo` over SSH is refused by Claude Code's permission layer (3 November);
give him the commands instead of retrying another way:

```sh
ssh -t -i ~/.ssh/work_ops_vps ops@100.64.0.10
tmux new -s upgrade      # a Tailscale upgrade restarts the tailnet link this SSH session uses
sudo apt update && sudo apt upgrade
```

Read the package list before answering: kernel packages mean a reboot, which needs its own plan.
Packages Ubuntu holds for a phased rollout (`apt-cache policy <package>` shows `phased N%`) need no
action. If the session drops, reconnect and run `tmux attach -t upgrade`. The UpCloud web console is
not a route for this, because `ops` logs in with its key only. `upctl` manages the server from
outside (start/stop, disks, backups, firewall) and cannot run commands on it; it is installed on
the Mac mini without an API token, and granting one is the owner's decision. Afterwards run
`ops/vps/check.sh` and record the result in the current maintenance ledger.

## Debugging the hosted site

`SSH` below means `ssh ops@work-ops-vps` (the tailnet; see [Tailscale](#tailscale)). Start with
`ops/vps/check.sh`: it is read-only and covers the services, database, timers, firewall and Access.

| Symptom | Where to look |
| --- | --- |
| Site does not load, or Cloudflare shows 502/530 | `SSH systemctl status cloudflared work-ops-workspace`; `SSH sudo journalctl -u cloudflared -n 50`. Check tunnel connectivity for 530. For 502, check both workspace health and origin Access validation: an issuer mismatch can reject requests while the workspace service is healthy. |
| Login loop or "access denied" | Access sign-in log: Cloudflare dashboard → Zero Trust → Logs → Access, or the API `accounts/{id}/access/logs/access_requests`. The app is "work"; one Allow policy, one-time PIN, 1-week application and policy sessions (saved and checked 16 October). The login-page name is "Work Ops". |
| Saves fail at once with **Failed to fetch** / **Load failed**, and DevTools shows `(blocked:csp)` on `/api/...` | The one-week Access sign-in ended while the tab was open. Sessions do not extend with use: the 16 October session ended at 08:52 on 23 October, mid-task. Access redirects every API call to the login page, and the page's `connect-src 'self'` blocks that redirect before it leaves the browser, so nothing reaches the origin or its log. Since `api()` started using `redirect: 'manual'`, the page reports **Your sign-in has ended** and offers sign-in in a new tab, which keeps typed text. Anonymous check: `curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -X POST https://work.example.com/api/state` returns 302 to the login page. |
| 502 after an Access team-domain change | Check `journalctl -u cloudflared` for `AccessJWTValidator` issuer mismatch. On 16 October existing tokens retained the old issuer after a rename; restoring the Cloudflare team domain and matching VPS `teamName` recovered connected Chrome access. Current team domain: `example.cloudflareaccess.com`. Before any future rename, revoke existing user sessions while the current domain still works and verify the old browser session returns to login. Stage and validate the matching server configuration before cutover. Keep JWT validation enabled. Anonymous redirects and service health do not prove authenticated access; verify the workspace in both an existing session and a fresh code login. |
| Login code late or missing | Request one code only: a new request cancels the last one, and Gmail files every code in one conversation (newest last). Search Gmail `in:anywhere from:noreply@notify.cloudflare.com`; the Access log records only completed logins, not code requests. Check the app's Allow policy and the One-time PIN identity provider exist, then [cloudflarestatus.com](https://www.cloudflarestatus.com). Incident of 30 Sep 2026 (codes delivered late, no fault found): 2026-09-30-access-otp-incident.md. |
| The site briefly fails, or `ops/vps/check.sh` reports "workspace killed for memory" | `SSH sudo journalctl -u work-ops-workspace | grep oom-kill`. systemd restarts the server within 5 seconds. The service is capped at 512 MB (`MemoryMax` in `ops/vps/work-ops-workspace.service`); one cached full state was about 80 MB of live objects on 21 October and grows with the data. Measure with a fresh copy before raising the cap: `ops/pull-backup.sh`, then load a scratch server on port 8791. |
| A save fails or is refused | `SSH sudo journalctl -u work-ops-workspace -n 100`: one line per save or refusal (`PATCH /api/records/X 409 12ms`), never its content; a 500 adds a traceback. Each page load (`GET / 200 3ms iphone`) and section view (`POST /api/seen/week 200`) is counted too, never which record; other successful reads are not logged. |
| The dashboard says "Daily run overdue" or "Nightly backup overdue" (over 30 hours) | `SSH systemctl list-timers wo-scan.timer`; `SSH sudo journalctl -u wo-scan -n 40` (one line per source, then the import and quality counts). |
| Nightly backup missing or failed | `SSH sudo journalctl -u wo-backup -n 40`. Check local snapshot creation, encryption errors, and `data/backups/last-backup.json`. The job no longer commits or pushes views. |
| Encryption reports permission denied | Check that `workops` can traverse `/srv/offsite` and write `/srv/offsite/work-ops`, and that `systemctl show wo-backup -p ReadWritePaths` includes the destination. Both filesystem permissions and the service sandbox must allow the write. |
| An encrypted backup ends in `.age.part` | It is unpublished output from an incomplete run. Do not rename it into a completed archive. A successful run publishes only its own output; validate completed backups with the restore drill before relying on them. |
| A source "failed" (a failure decision on This week), or a read is blocked | The sentence names the kind (bot challenge, refused, timed out …) and what follows ([scanners.md](../docs/scanners.md#reading-retries-and-the-fetch-log)). `ops/wo-live fetch-log --failures` lists recent failed requests from ingest and `wo fetch` (`/srv/work-ops/data/logs/fetch.jsonl`); `ops/wo-live fetch <url>` retries one read by hand. |
| Did a scheduled job fail? | Dashboard → Needs attention: failing sources, crashed scanners and an overdue ingest or backup each raise a "Something failed" task (`tools/failure_tasks.py`). Also `SSH systemctl --failed`, the dashboard's overdue warning, and `ops/vps/check.sh`, which fails when a timer is off. Nothing pushes an alert outside the site. |
| Which code is running | `ops/vps/check.sh` ("serving abc1234 …, N code commits not deployed"), or `SSH curl -s 127.0.0.1:8769/api/health`. |
| The data looks wrong | Read with `ops/wo-live show <id>` or `quality`; for a local rehearsal, use `ops/pull-backup.sh data/work-ops-<date>.db` at an unused path. Never copy the live `.db` file. |

Journald is persistent on the VPS (`/var/log/journal`, about 8 MB on 27 Sep). The system clock is UTC
(the timers say UTC); the services and `wo` run on Europe/London. Stored timestamps carry their
offset. A travelling Mac may use another timezone; do not assume its wall clock matches the services.
The off-host LaunchAgent's 07:30 is the installing Mac's local time.


## Server research

The `wo-research.service` / `wo-research.timer` pair runs focused public vacancy/contact research
at 09:00/19:00 Europe/London. It uses the existing VPS Codex ChatGPT login through
`wo-research-codex.socket`; the model runs under `codex`, separate from the `workops` importer.
No separate API key or dollar ceiling is required. `config/research.yml` owns activation/call limits.
See the [research runbook](../docs/vps-research.md) for usage limits, service isolation, recovery and
the release ledger for deployed evidence.

## Focused research and caretaker recovery

Migration 017 and focused research are deployed; the caretaker timer is active. A normal deployment
preserves existing timer states and installs a previously absent caretaker timer disabled. Deployment
waits for any old research pass before switching code, so historical usage is complete before
cutover; a lock timeout stops the release for inspection. Initial activation is separate from
deploying the files. User-facing state is documented in
[the guide](../USER-GUIDE.md#job-and-contact-research); [caretaker checks](../docs/vps-caretaker.md)
explain the deadlines and limits. Agent reads of live records/receipts use `ops/wo-live state`.
The server-shell commands below are for an attended operator, under the existing release/recovery
boundary; they are not authorization for an agent to bypass the live-data gateway.

### Stop and inspect

1. Pause new research: `sudo -u workops touch /srv/work-ops/data/research.pause`. The worker checks
   between targets; an already-dispatched call may finish and counts against the allowance. For an
   immediate attended stop, `sudo systemctl stop wo-research.service`. A socket process may still
   finish its bounded invocation, so restarting the worker cannot safely refund its reservation.
2. Pause caretaker checks: `sudo -u workops touch /srv/work-ops/data/caretaker.pause`. Its timer
   records Paused instead of clearing incidents. To stop it entirely, use
   `sudo systemctl disable --now wo-caretaker.timer`; the old panel then ages to Overdue.
3. Inspect fixed services with `systemctl status wo-research.timer wo-research.service
   wo-research-codex.socket wo-caretaker.timer wo-caretaker.service` and
   `sudo journalctl -u wo-research.service -u wo-caretaker.service -n 60 --no-pager`.
   Use site Research coverage/Caretaker checks for scope, usage, waiting IDs and failure cause.
   Do not print authentication files, mail bodies or model/source packets into chat.

### Recover the specific cause

| Cause | Attended action and required proof |
| --- | --- |
| Daily/weekly/account allowance | Wait for the next eligible London day/Monday or account reset. Do not raise caps, clear reservations or switch to an API key. New successful source evidence, not a timer restart, restores freshness. |
| Codex access | As `codex`, run its documented device sign-in; the owner completes authorization. Never copy `auth.json`. A bounded next pass must produce a valid receipt/import. |
| Blocked source | Read the explicit reason; correct a supported public URL or leave it unverified. Respect the 24-hour cooldown and seven-day discovery cache. No identity/proxy bypass or blind paid retry. |
| Worker interrupted after saving a result | Replay the retained batch using the command below. Digest/identity checks and the canonical importer preserve idempotency, evidence timestamps and human notes. A missing/corrupt batch is not proof of successful research. |
| Email backlog/quarantine/capacity | Follow the existing email archive/recovery runbook. Preserve originals and checkpoints. Require a new successful receipt with no pending/quarantined/high-watermark gap. |
| Stopped service/timer | Inspect the journal and cause, apply the attended fix, then enable/start only the intended unit. Require its successful receipt/health response. |
| Old/failed nightly backup | Inspect wo-backup and its published completion receipt; repair through the backup procedure. Verify the off-host copy/restore drill on the Mac separately. |
| Database restored from an older backup | Keep research paused. Later reservations may be absent: reconcile trusted subsequent usage or wait until the next London Monday before resuming. Do not reset the usage ledger/seed marker. Missing raw batches remain explicit recovery gaps. |

From `/srv/work-ops/work-ops`, an operator can reconcile retained research **without network reads
or model calls**, even while research is paused:

```sh
sudo -u workops /srv/work-ops/venv/bin/python tools/research_worker.py \
  --db /srv/work-ops/data/work-ops.db --data /srv/work-ops/data --replay-only
```

This imports only batches already named and hashed in interrupted receipts. It does not create a
new all-clear receipt, replenish usage, renew evidence time, or overwrite a user's note. Preserve
`data/research-server/` when available; normal database backups include the ledger/cache/receipts,
but not these raw batch files. Run no live replay without attended recovery authority.

Outgoing preparation is retired. Legacy retained preparation batches are marked retired without
creating drafts or resolving decisions; preserve their files and original receipts. Evidence-only
batches still revalidate context and revisions. Caretaker no longer raises a preparation-stall check.
No call/usage reservations are cleared. [Retirement contract](../docs/vps-research.md#weekly-approach-preparation).

### Resume and verify

After fixing the cause, remove only the relevant pause marker:
`sudo -u workops rm /srv/work-ops/data/research.pause` for research, or
`sudo -u workops rm /srv/work-ops/data/caretaker.pause` for caretaker. An authorized research test uses
`sudo systemctl start wo-research.service`; it remains within six/pass, 12/day, 84/week and the
aggregate 16/day, 100/week. Inspect the resulting receipt and a checked record. A partial result is
still partial; it need not spend further allowance to try to turn the dashboard green. An expected
quota wait or explicit pause exits zero but retains its blocked/partial receipt. Access errors and
worker failures exit nonzero. A completed oneshot service normally returns to inactive/dead; inspect
its exit result, latest receipt and active timer rather than expecting a continuously running process.

For initial caretaker activation, after migration/deployment and explicit activation authority:
`sudo systemctl start wo-caretaker.service`, inspect the recorded checks, then
`sudo systemctl enable --now wo-caretaker.timer`. A failed check makes the service exit nonzero
but its receipt/incident is still published. The next timer tick checks again. Confirm a second
**scheduled** receipt separately from the manual run. Never infer recovery from `systemctl start`
alone. The caretaker uses zero incident-diagnosis invocations; model-powered diagnosis is not built.

A normal rollback to schema-16 code is refused after migration 017. Use a compatible corrective
release or an explicitly approved backup recovery. Older broad-research code does not enforce
these caps: keep its service stopped during such recovery. Preserve accounting data; do not
down-migrate or discard the ledger to work around the release guard.

### One-time research acceptance

`wo-research-acceptance.timer` is a fixed-date VPS follow-up for **19 October 2026 at 10:05 London**.
It is distinct from the normal research/caretaker timers. [Report contents and delivery limits](../docs/vps-research.md#one-time-monday-acceptance).

- Inspect: `systemctl status wo-research-acceptance.timer wo-research-acceptance.service` and
  `sudo journalctl -u wo-research-acceptance.service -n 30 --no-pager`. Journal output is only the
  aggregate outcome; the report is private.
- Read: `ops/wo-live research-review --date 2026-10-19` from an agent session. An operator on the
  server uses `wo research-review --date 2026-10-19`. Readback verifies both report hashes.
- Stop before it runs: `sudo systemctl disable --now wo-research-acceptance.timer`. Stopping this
  timer leaves routine research and caretaker schedules intact.
- If the process fails before publication, inspect the stated cause, then run
  `sudo systemctl reset-failed wo-research-acceptance.service` and
  `sudo systemctl start wo-research-acceptance.service` under attended recovery authority. Early
  execution before 10:05 on the selected date is refused. No research/model call is started.
- If `.done` exists, the service condition skips execution. Preserve the original report and
  manifest even if it records missing evidence or no useful result. A revised investigation should
  be a separately authorized report; do not delete the marker to rewrite history. Hash failures
  require inspection, not silent regeneration.
- After execution, the absolute-date timer has no future recurrence. It can be disabled after
  reading the receipt. A completed oneshot service is normally inactive with a successful exit;
  use the generated report to judge research acceptance, not service activity alone.

Repository documentation sync remains attended: the VPS's GitHub deploy key can only read.
The server writes the acceptance report locally and does not push, edit the serving checkout,
contact anyone, post to desktop chats or spend additional model allowance.

### Public career/ops pitch

`/career-ops` and `/career-ops/` serve the public static pitch. Its four named files live under
`/career-ops/`; server routing and the first tunnel ingress rule both use explicit allowlists.
Cloudflare Access has a separate `career-ops public pitch` application for that path with an
Everyone Bypass policy. The original `work` application and the tunnel's protected hostname
catch-all remain in force, including for `/`, `/system-map` and `/api/state`.

The one-time origin change is `sudo python3 ops/vps/public-career-ops.py` on the VPS checkout;
it validates configuration and keeps `/etc/cloudflared/config.yml.before-career-ops` for rollback.
Restore that backup and restart cloudflared to withdraw public origin access; remove the separate
pitch Access application to restore its sign-in gate. Do not remove the private application's
policy or disable JWT validation on the catch-all. Unknown pitch paths must never return private
content. Verify anonymous HTTP after any access change; local tests cannot prove the edge boundary.

## Collection accounting and recovery

**Inspect before rerunning:** `ops/wo-live collection-policy --json` reads the deployed policy and
provider ledger through the approved gateway. Dashboard → Quotas and usage shows the same ceilings,
used/reserved attempts, deferrals and actual check dates. Ingest and caretaker diagnostics add a
`collection` projection; old receipts remain valid historical evidence. The file
`/srv/work-ops/data/collection.db` stores attempt reservations/outcomes and task cursors, not jobs.
No provider request is made by these diagnostics. Unknown accounting is not zero usage.

- A daily or scheduled budget wait needs no repair. Wait for the next London day or later due turn.
  Do not delete usage, change the clock, retry on the Mac, choose another host or alternate API URL.
- Invalid policy: correct [the owning YAML](../config/collection.yml), run its checks and release it.
  All consumers use the same validator. Existing records and the rest of the site remain readable.
- Missing/corrupt ledger: affected dispatch fails closed. Pause scan/research timers and drain their
  services through the normal privileged operations route before recovery. Retain the damaged file
  for diagnosis; never overwrite it with an empty allowance. Email and other timers are unaffected.
- Nightly backup embeds a consistent accounting snapshot in **the disposable record backup only**,
  before its existing age encryption/offsite transfer. A missing/corrupt accounting store fails
  the backup visibly. The live record database receives no duplicate accounting table or envelope.
  The Mac restore drill restores/compares that snapshot in scratch. Historical backups without it
  are labelled as predating collection accounting; they cannot establish today's remaining budget.

For an approved accounting recovery, use the server's current code as `workops`, with a decrypted,
verified record backup and a new destination path:

```sh
/srv/work-ops/venv/bin/python /srv/work-ops/work-ops/tools/collection_admin.py restore \
  --backup /srv/work-ops/data/backups/verified-recovery.db \
  --path /srv/work-ops/data/collection.recovered.db
```

The command refuses an existing destination and retains every backed-up reservation/cursor. It
holds dispatch until the next London midnight because post-backup usage is uncertain. After
verification, retain the previous accounting file and atomically publish the recovered file as
`collection.db` while collectors remain drained. Run `wo collection-policy --json`, verify the
hold and ceilings, then restore exactly the previously enabled timers. An old backup without an
accounting envelope requires explicit `collection_admin.py initialize` to a new path with the same
hold; retain the old evidence. An unexplained future clock keeps dispatch blocked and needs clock
reconciliation, never deletion of usage. These recovery commands do not grant new live authority.

**Release cutover:** the deployment runner drains worker locks, stops only the affected timer
triggers/services while replacing code, and initializes accounting once with a conservative hold
for the cutover day. Timer schedules/enabled states are unchanged. Finish attended provider reads
before cutover. The first full following London day establishes the new site-wide guarantee.
If deployment fails after draining, keep collectors paused until a cap-compatible build is restored;
inspect the failure receipt before resuming. A rollback target without collection enforcement is
refused. No older unmetered worker may resume, and accounting/cursors/evidence/personal work survive.

For seven scheduled days after the authorized release, inspect existing receipts for attempts per
provider (never over the configured ceiling), oldest rotation attempt, ageing chosen-work evidence,
useful admissions, deferrals, scan runtime/RSS and lock waits. Compare usefulness, not just traffic.
Local synthetic verification does not establish those hosted outcomes or physical-device usability.
