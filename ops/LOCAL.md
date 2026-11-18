# The Mac and other development checkouts

[Documentation index](../docs/README.md)

Since 27 September the workspace is hosted (`https://work.example.com`; see
[OPERATIONS.md](OPERATIONS.md) and HANDOVER.md). The Mac serves nothing and schedules
one LaunchAgent job, the daily off-host site-database backup. The former Codex desktop research
schedule is paused; [VPS research](../docs/vps-research.md) uses the server's existing Codex ChatGPT
login at 09:00/19:00 Europe/London. See the
focused release ledger for verified run evidence.
Email collection itself runs entirely on the VPS/Cloudflare. See the
[system diagrams](../docs/system-map.md) for the boundaries. The server and ingest LaunchAgents and the phone relay were
retired then; git history keeps them.

## Set up a checkout

```sh
python3.12 -m venv .venv                 # 3.12 on every host (.python-version)
.venv/bin/pip install -r requirements.txt
ops/check.sh                             # Python, contract, browser and email Worker checks
```

The repository's SessionStart hook (`ops/session-start.sh`) makes `.venv` when it is missing, so a
cloud session starts ready. `ops/check.sh` runs the contract suite on a backup copy of
`data/work-ops.db` when the checkout has one, otherwise on `data/dev/work-ops.db`, a synthetic
database of made-up records built by `tools/make_fixture.py` (the owner's data is not committed, #28).
The source is opened read-only and only the disposable contract copy is migrated. View-parity
checks likewise reconstruct the export's declared schema, then migrate their temporary copy.
The separate contract entry point checks sequential review, one offered transition per field for every record
and unchanged saves. Four disjoint partitions cover each record once per operation, in separate
copies; the exhaustive synthetic API contracts run once in the Python suite. Empty contract
inputs fail. Release checks use a private archive of the selected commit and synthetic data;
they do not link this Mac's development database into the release snapshot.
The browser suite always builds fresh synthetic records with deterministic approval, outreach,
outcome and history cases; it does not use personal records from the development database.

Python 3.12 and the pinned dependencies in `requirements.txt` (PyYAML and pypdf) are sufficient
for the application and its Python tests. pypdf reads attended private PDF uploads.
SQLite and gzip support come from Python's standard library; no additional package is required for
the decision workspace. Node and Playwright are local browser-test dependencies, not VPS runtime
dependencies.
For Mac development, install `requirements-dev.txt` to run Python tests under pytest/xdist.
`ops/check.sh` uses one worker per core on Macs with at least 4 GiB; on the VPS it installs no
Python test dependencies and uses one worker, falling back to unittest if pytest is absent.
The pytest network guard permits loopback only; it does not apply to the unittest fallback.
See [focused commands](../docs/commands.md#focused-development-checks).

The browser suite needs Node and the browsers matching the pinned Playwright package. Install them
when setting up a machine that will run browser checks:

```sh
npm ci --prefix tests/ui
node tests/ui/node_modules/playwright-core/cli.js install chromium webkit
ops/check.sh --strict
```

`ops/check.sh` installs missing JavaScript dependencies with `npm ci` using the committed lockfile;
it does not download browsers automatically. In default mode unavailable browser checks print
`SKIP` and do not fail the command. Strict mode exits non-zero for a failure or skip and is required
by deployment. `ops/check.sh --strict contract ui` selects just those suites; `--help` lists usage.
The default run also checks the email Worker when `ops/email-intake/node_modules` is installed.
Missing Worker dependencies print `SKIP`, so strict mode, and therefore a deploy, fails until
they are installed (changed 3 November; before then a note let 12 of the 13 release stages pass).
Install them with `npm ci --prefix ops/email-intake` on any Mac used for a release. Setup is in
[the email Worker README](email-intake/README.md).
Both Node suites reject installed package versions that differ from their lockfiles, including
transitive packages. Rerun `npm ci --prefix tests/ui` or `npm ci --prefix ops/email-intake` as named
by the failure; this installs the committed versions without upgrading them. The Worker gate
tests its production buffer configuration and actual D1 migrations separately from its retained
direct-POST pilot tests.
Every run first checks whether Python can bind a temporary loopback listener. If permissions deny
it, the command fails immediately before running any suites. `ops/check.sh --preflight` runs only
this setup/port check; it is not test evidence and cannot be combined with `--strict` or suites.
In an agent sandbox, rerun through the command's permission mechanism with local-server permission;
do not change global sandbox settings. A passing preflight does not guarantee browser startup or
network access. Later browser `EPERM` failures still need their own diagnosis. On macOS, Cursor's
command sandbox (`CURSOR_SANDBOX=seatbelt`) and Codex's (`CODEX_SANDBOX=seatbelt`) cannot register
an application with the window server. WebKit then abort()s and macOS writes a crash report for
each helper. `ops/check.sh` and `tools/browser_launch.mjs` refuse to launch Chromium or WebKit
there, print one `FAIL`, and leave no crash report. Rerun the same command outside the sandbox.

Each step prints its start, elapsed time and log path, plus a heartbeat every 30 seconds while it
runs. A failure prints the last 40 lines; full stdout/stderr remains in the displayed temporary
directory (directory mode 700, files 600). Logs may contain development-record details: inspect
locally, do not commit or share unredacted. Remove that run's directory when finished; no automatic
retention beyond the operating system's temporary-storage cleanup is promised. Ctrl-C
stops the running step rather than continuing into subsequent suites.

Browser stages run at most two at a time on Macs with at least 8 GiB and two CPUs. They stay serial
on the shared VPS, low-memory hosts or when memory is unknown. `WO_UI_JOBS=1 ops/check.sh ui`
forces serial execution for diagnosis; only values 1 and 2 are accepted, and the VPS permits 1.
Each stage retains its own database, ephemeral port, process group and private log; interruption
stops running groups and cancels queued stages. Career-page captures are inside that run's log
directory during checks. A completely passing run removes those routine captures and keeps the
text logs; failures or skipped checks retain them for diagnosis. Assertions and both browser
engines are the same in serial and parallel modes.

Browser failures also retain bounded diagnostic evidence under that log directory; trace recording
is opt-in. Remove the printed run directory after diagnosis, or the specific standalone evidence
directory if a suite was run directly. Successful failure-evidence runs create no capture directory.
There is no cross-run retention daemon. Keep raw traces private; their detailed page/network
contents are suitable only for synthetic flows without authenticated sessions. Commands and limits
are in [browser failure evidence and visual comparisons](../docs/commands.md#browser-failure-evidence-and-visual-comparisons).

Visual checks and reference updates take an exclusive `tests/ui/visual-baselines/.run.lock`
containing the owning process ID. A second run fails immediately instead of racing an update.
Normal exits and handled SIGINT/SIGTERM interruptions release the owned lock and clean up owned
browser/server processes and disposable fixtures. SIGKILL, a process crash or power loss can
still leave it behind: inspect its recorded PID and confirm no visual check/update is running
before removing that exact lock file and retrying. Do not remove another active run's lock or
use a longer timeout to permit overlapping updates.
An unresponsive browser launch can also exceed the runner's existing five-second cancellation
grace before a browser handle is available; forced-kill recovery then applies. The tests cover
normal startup cancellation, cancellation during capture and failed executable validation, not
every possible browser/OS hang. Keep this limit explicit instead of extending the grace period.

### Reusable verification skill

The maintained source is [ops/skills/verify-changes](skills/verify-changes/SKILL.md). It selects
existing checks and optional diagnostics across repositories, with project commands and baselines
remaining in each repository. On a Mac, install a discoverable link from the checkout root:

```sh
mkdir -p "$HOME/.codex/skills"
ln -s "$PWD/ops/skills/verify-changes" "$HOME/.codex/skills/verify-changes"
```

If that name already exists, inspect it rather than replacing another skill. New sessions can
discover `verify-changes`; `$verify-changes` explicitly requests it. Moving this checkout requires
repairing the link. Remove only that symlink to uninstall; the versioned skill remains in this
repository. Vendor/plugin-cache skills are unchanged. The skill does not install DevTools,
external monitoring or a component workbench, and does not authorize deployment.

### Final checks and local previews

During edits, run the smallest relevant test file or name filter first; see
[focused check commands](../docs/commands.md#focused-development-checks). Finish with the complete
`ops/check.sh --strict` on the final code. Keep both synthetic unit contracts and the separate
development-copy contract suite: the former exercises exhaustive fixture cases, while the latter
checks offered actions on a migrated copy of existing data. They cover different risks.
Pending migrations are listed from `workspace/migrations/`; a hosted release that adds any needs
`--migrate`. Tests migrate disposable copies, never the source development or hosted database.

To look at a change in a browser, create a synthetic database at an unused path and serve it on a
spare port (8769 is the VPS's port and the SSH forward's):

```sh
.venv/bin/python tools/make_fixture.py data/dev/preview.db
.venv/bin/python workspace/server.py --db data/dev/preview.db --port 8791
```

Open `http://127.0.0.1:8791`, then follow [the user guide](../USER-GUIDE.md).
If the database path already exists, choose a new unused name; the fixture builder refuses
to overwrite it. Stop the preview with Ctrl-C. For the repeatable guide walkthrough:

```sh
node tests/ui/user-guide.mjs
```

It builds a fresh fixture per browser and is also included in `ops/check.sh ui`.
`WO_GUIDE_CAPTURE_DIR=/tmp/work-ops-guide-captures node tests/ui/user-guide.mjs` saves real
desktop and mobile captures. Inspect them before copying selected images into documentation.
The walkthrough exercises the opportunity lifecycle and factual tracking on synthetic records. No external contact/application is sent and no Cloudflare sign-in is simulated as a pass.

## Outreach prototype

**Historical experiment, superseded by the opportunity-led app on 26 October.** Retained for evidence;
its email preparation/handoff behavior is not part of the current app. Use the database-backed
preview above to validate the replacement.

The outreach experiment is an
isolated local interface with three invented contacts. It uses no database, live records or
email connection. Start from the repository root:

```sh
.venv/bin/python prototypes/outreach/serve.py
```

Open `http://127.0.0.1:8792`. Stop with Ctrl-C. This loopback server exposes only the prototype
assets and the existing site stylesheet/DOM helper, not the repository directory. Use the same
host and port to return to this browser's saved examples. Another browser/device has separate
state; mobile layout testing does not establish cross-device sync. **Reset examples** resets
only this prototype's synthetic storage and can recover corrupt saved data after confirmation.

Try Maya for preparation from relationship context, Sam for reusing an existing draft, and Jordan
for returning to a specific commitment. **Preview email handoff** opens a local simulated composer;
it cannot send or sync Gmail edits. **Record contact** optionally keeps exact wording and saves
the next step, or explicitly no next step. Saving errors retain input. A stale-tab conflict
offers **Reload saved version**; copy any unsaved wording you want to keep before discarding it.

These explicit prototype checks are separate from `ops/check.sh`:

```sh
node --test prototypes/outreach/state.test.mjs
node prototypes/outreach/browser-test.mjs
```

The browser check starts its own temporary loopback server and uses the existing pinned
Chromium/WebKit dependencies. The normal Mac browser permission rules above apply. To inspect
synthetic captures, set `WO_OUTREACH_CAPTURE_DIR` to a temporary directory when running that check.
No browser storage or captures belong in Git. No production change or deployment is included.

## Restricted VPS Codex checkout

This account uses synthetic fixtures for tests and previews. Do not pull production backups or
copy the live database, credentials or service identity here. Explicitly requested record reads
and edits use the already configured `ops/wo-live` route; it grants no server shell or deployment
access. See [VPS Codex record access](OPERATIONS.md#vps-codex-record-access).

For browser work, use the verified launch options and normal command approval described in
`/home/codex/.config/website-ops/README.md`. Keep Chromium sandboxing enabled and listeners
on loopback. A sandbox startup error is not permission to use `--no-sandbox`.

### Account tool maintenance

On this VPS, the existing account cron runs
`/home/codex/.codex-toolchain/daily-update.py` daily at 04:15 UTC and at boot, with a
30-minute timeout. It updates account-owned Node/npm, Python, uv and GitHub CLI. It checks
protected Codex/Git, cached OS package availability, the reboot marker and this checkout's
direct dependency pins, but reports work requiring administrator access or compatibility
review instead of changing those installations or deploying the application.

Read the latest result with:

```sh
cat /home/codex/.codex-toolchain/status.json
```

`current` applies only to the named check. `administrator_required`, `review_required`,
`failed` or `unavailable` means work remains; the updater then exits nonzero. OS availability
uses cached APT indexes and flags metadata older than 48 hours. This is not a full dependency
security audit. Results and the latest log stay local; no external notification delivery is
configured. A successful cron invocation alone does not establish that every tool is current.

The updater uses an exclusive lock, versioned installations and atomic link activation.
Its synthetic checks are at `~/.codex-toolchain/test_daily_update.py`. The previous GitHub CLI
binary and updater script are retained there for rollback. Project upgrades still use the
repository's dependency pins, checks and release gates. This non-admin account cannot perform
protected OS/Codex/browser upgrades or reboot; use the existing Mac administrator session.

## A fresh copy of live data

**Authorized Mac only.** This backup workflow does not apply to the restricted VPS Codex checkout.

```sh
ops/pull-backup.sh data/work-ops-$(date +%F).db    # newest encrypted VPS backup, decrypted on the Mac
```

It needs the age private key (`~/.config/work-ops/backup.agekey`, also in the owner's password manager).
Never copy the live `.db` file; its WAL may hold committed pages.

Collection API development uses fixtures. Independent Mac provider dispatch is blocked; use
`ops/wo-live collection-policy --json` for deployed usage and the existing approved VPS fetch/probe
route for an attended read. A local ledger is test data, not a production quota authority. The
[collection recovery procedure](OPERATIONS.md#collection-accounting-and-recovery) owns restoration
and conservative holds. New encrypted record backups include the accounting snapshot; the existing
off-host transfer carries it without a new credential or file format.

## The daily off-host backup

The job is installed per Mac. Verified 3 November: the MacBook Air owns the loaded daily job.
After a morning SSH-transfer failure, its 20:44 UTC rerun retained the latest encrypted backup
and passed the restore drill (schema 19 migrated to 20 in scratch; integrity OK; zero quality
defects). The Mac mini has no offsite job, key or copy; it does not need a duplicate installation.
On a Mac without `data/offsite/`, `ops/vps/check.sh` reports the off-host checks as PENDING.
The Air's successful receipt is local evidence, not a promise that a sleeping/offline Mac can pull.

This pulls the site database only. The encrypted email spool/state archives currently remain on
the VPS; see [email recovery](../docs/linkedin-alerts.md#backup-and-restore).

```sh
python3 ops/local/service.py offsite-install     # com.macmini.workops-offsite, daily 07:30 (#23)
python3 ops/local/service.py offsite-status
python3 ops/local/service.py offsite-uninstall
```

`ops/offsite-weekly.sh` keeps the VPS's newest encrypted backup in `data/offsite/` (8 kept) and runs
`ops/restore-drill.sh` on that copy: it decrypts it to a scratch folder, checks integrity and record count,
brings a schema that is behind this checkout forward with the code's migrations (as a restore onto newer
code would; `ops/local/restore_check.py`) and runs `wo quality --strict`. A backup with a migration this
checkout lacks fails with "git pull". Quality defects are reported apart from the restore (exit 3,
"RESTORED, with data defects"): they are a data problem to fix with `ops/wo-live quality`, not a failed
backup. Each run writes `data/offsite/last-run.json` (`passed`, `defects` or `failed`), which
`ops/vps/check.sh` and the session-start hook report. Logs: `~/Library/Logs/work-ops/offsite.log` and `offsite-error.log`.
Until 14 October the drill required the backup's schema to equal the code's, so it failed on most days
after a migration was committed before its deploy; `check.sh` saw only the copy's age and stayed green.

launchd runs a missed 07:30 the moment the Mac wakes, often before the network is up, so the job waits
for the VPS (12 tries, 30 s apart) and writes `FAILED` in `offsite.log` when it gives up. On 30 September
and 1 October 2026 the earlier single attempt failed that way and no copy was kept. A copy more than two
days old is reported by `ops/vps/check.sh` ("This Mac") and by the session-start hook; run
`ops/offsite-weekly.sh` by hand to catch up.

SSH to the VPS shares one connection for 10 minutes (`ControlMaster`/`ControlPersist` in the Mac's
`~/.ssh/config`, 29 Sep 2026): at about 1 s of latency a new connection costs about 9 s, and a shared
one about 2 s per `wo` call. Scripts that use the restricted off-host key (`pull-backup.sh`,
`offsite-weekly.sh`) pass `-o ControlPath=none`. The shared socket is keyed by host and user, not by
key, so without it they would reuse the main key's connection, or your commands would run under the
restricted key's forced command.

## If the VPS is lost

When the VPS is unavailable, use an encrypted copy already retained in `data/offsite/`;
`ops/pull-backup.sh` requires the VPS. `ops/restore-drill.sh data/offsite/<archive>.db.age` checks a
retained copy without the VPS, on a scratch copy only. Preserve the encrypted original. Decrypt the selected completed `.db.age` archive to a new,
private path with `age --decrypt -i ~/.config/work-ops/backup.agekey -o <new-file.db> <archive.db.age>`.
Validate SQLite integrity, schema versions and nonempty records before migration; rehearse any
required migrations on that restored copy and run `wo --db <new-file.db> quality --strict`.
Then follow the approved rebuild procedure, including its access and backup checks.
Starting a replacement writer is a recovery decision, not an automatic effect of restoring a copy.

## Private career reference files

The [reference-data map](../docs/reference-data.md) owns message/CV lookup, application source
placement and local-library recovery limits. These gitignored files are separate from development
database copies and the off-host site backup. Preserve their sources before local cleanup.

## Repository hygiene

Inspect size before removing files: `du -sh data .venv ops/email-intake/node_modules`
shows the main local stores and dependency environments when present. Ignored files are not
necessarily disposable; `.gitignore` is an allowlist, not a retention policy.

- Source-tree `__pycache__/`, `.pytest_cache/` and Finder `.DS_Store` files are reproducible caches
  or metadata. They can be removed when checks are idle; Python and pytest regenerate their caches.
- Generated CSV/JSON/Markdown views are replaceable only when their source database and regeneration
  path are known. Do not confuse them with raw imports, evidence or backups.
- Retain `data/` databases, WAL/SHM sidecars, pending observations, raw imports, research/evaluation
  evidence and encrypted off-host backups. Use the established backup retention and restore checks;
  age or file size alone is not permission to delete the only copy.
- Keep active `.venv`/Node dependencies and nested repositories. On 23 October the owner explicitly
  requested removal of `.venv-connector` and the `workops-research-synthetic` Codex registration; both
  were removed with all other Codex settings preserved. Historical synthetic databases/evidence
  remain. Future global client changes still require their own scope.

Use explicit reviewed paths, never a blanket `git clean -fdx`. Record removed artifacts and recovered
space in the cleanup ledger. Removing local caches saves disk space; it does not shrink the live
SQLite database or the full `wo state` response. State export capacity
bounds receipt-processing memory while preserving every historical receipt and the existing output.
