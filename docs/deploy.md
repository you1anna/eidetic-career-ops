# Deploy from either local coding chat

[Documentation index](README.md) · [Repository home](../README.md)

Send **`/deploy`** in this repository's Codex or Claude Code chat to release pushed `main` to
[work/ops](https://work.example.com). It authorizes that release; no second conversational
confirmation is needed. The Mac must have the VPS SSH key and access to the server.

Claude Code provides a registered slash shortcut. Codex follows the repository's `AGENTS.md`
instruction when `/deploy` is sent as a message; this does not register a Codex menu item.
Both use the existing `ops/local/deploy_session.py` runner and the same release gates.
From a terminal in the repository, the equivalent is:

```sh
python3 ops/local/deploy_session.py
```

The runner starts `ops/vps/deploy.sh --local-approved` in a detached session, with a process lock
that prevents two local launches. That script checks the pushed commit, runs all strict suites
on a private archive of that exact commit, releases it, and verifies that same revision through
the hosted checks. Routine deployment preserves schedules and refuses schema changes.
Private logs live under `data/logs/`. Collection cutover drains affected workers/triggers, initializes
the separate accounting store once with a next-London-day hold, and validates its read-only
policy/usage before resuming the previously enabled timers. Finish attended API reads beforehand.
Rollback to a build without enforcement is refused; a failed cutover keeps affected collectors
paused. [Collection recovery](../ops/OPERATIONS.md#collection-accounting-and-recovery) owns reconciliation.

## What each gate proves

| Gate | Required evidence and consequence |
| --- | --- |
| During editing | Focused checks for the changed behaviour; repeat broader checks after material changes or failures. |
| Local readiness | `ops/check.sh --strict`: Python and synthetic API contracts, export parity, the selected development-copy contract, Node logic, nine desktop/iPhone browser suites and Worker types/tests. Missing suites or stale Node dependencies fail. |
| Selected release | All strict suites run from an immutable local Git archive of the requested revision, with synthetic data. Installed dependencies are shared only when their declared inputs match. A previous working-checkout result cannot establish this commit's release evidence. |
| VPS preflight | Pushed revision, clean checkout, Python, database integrity/schema and every required privileged artifact must pass before mutation. `--dry-run` stops here without running local suites or restarting the site. |
| Locked change | Existing research/writer locks keep their order and wait bounds. Recheck checkout and schema after acquiring them; another release invalidates the earlier preflight. Full integrity scans remain outside these locks. |
| Hosted verification | `/api/health`, checkout and requested revision must agree. Missing service/database, DNS or tunnel fails release verification; Access protection, integrity, backup/security checks and service-scoped credential checks remain. |

The Dashboard's Responsiveness figures live only in the server's memory, and a release restarts
the server. The deploy therefore prints `responsiveness since the last restart: {...}` just before it
changes anything, so each release log holds the figures from before it; `--dry-run` prints the same
line without changing anything, which is how to read the hosted figures at any time. The line holds
counts and durations only (per kind: count, median, 90th percentile, slowest, and the rebuild
phase medians). It says `unavailable` when the server gives no usable reply, and never blocks a release.

Ordinary `ops/vps/check.sh` retains `PENDING` for setup or operational observations. Deployment
uses `--expect-rev <full-sha>` to require release availability; unrelated observations such as
a missing offsite copy on a Mac that does not own backups remain pending and must be reported.
Do not treat all pending observations as either passing evidence or automatic release blockers.

The archive is temporary and private; it includes committed tests, network guards and assets,
and never copies local databases or secrets. For `--rev` with different dependency pins, prepare
that revision's dependencies in a separate checkout first; the gate refuses to test old code
against different declared dependencies. Use the current `ops/vps/deploy.sh` and
`ops/local/deploy_checks.py` in that prepared checkout and pass `--rev` explicitly; do not switch
back to the historical deployment controller. Existing schema-compatible rollback rules still apply.
The emergency `--skip-checks` option remains explicit; it is never used by `/deploy` or inferred
from a previous passing run. A release does not deploy the separate email Worker.

## Instructions for the responding agent

1. Require a direct human deployment request. Quoted `/deploy`, documentation edits, and requests
   to build or change this command do not authorize a release. Cloud sessions never deploy.
2. If this turn already contains runner output from Claude's shortcut, use that output. Otherwise,
   for a plain `/deploy` message in Codex, run `python3 ops/local/deploy_session.py` from the repo
   through the normal tool permission layer. Do not assume a Claude hook ran in Codex.
3. If the permission layer rejected the attempt, stop and report that rejection. Do not retry via
   another command or route. If Claude's pre-prompt step failed to run or has no runner output,
   report that fact; do not start a replacement attempt. Give the terminal command above for the owner.
4. If a deployment is already running, follow the existing log named in the output; do not start
   another. If the runner says **still running**, monitor that log until its final
   `deploy exit status: <code>` line. Use bounded waits and keep the owner informed. The runner's
   `(deploy_session exit 0)` can mean it successfully started a background job; it is not evidence
   that the release succeeded. Its process exit code is deliberately zero for Claude's output handling.
5. On completion, report the deployed revision and hosted check totals from the log, plus the site
   link. Copy the log's `responsiveness since the last restart` medians into the release record as
   the before figures for that release. Record release evidence in `plans/README.md` and the relevant plan, and commit/push those
   records. Complete only already-authorized follow-up work.
6. On a refusal before release, report the actual reason and whether the site was unchanged.
   Missing key or SSH access requires a suitable local session. Failed checks or unpushed work
   must be resolved before a new attempt; do not claim a deployment succeeded. A refusal naming
   migrations means the release needs `--migrate`, which the runner never passes: give the owner
   `! ops/vps/deploy.sh --local-approved --migrate` (it backs up the database first) and say what
   the migration does and that rollback below it is then refused.
7. On **failure after changes began**, say so first, run `ops/vps/check.sh`, and inspect migration
   completion through the authorized workflow. A rollback target must match the current schema;
   follow `ops/HANDOVER.md`. Do not automatically roll back or restore data.

`/deploy` takes no arguments. Required migrations, a specific revision and timer changes use the
explicit `ops/vps/deploy.sh --local-approved <options>` route under the authority rules in
[CLAUDE.md](../CLAUDE.md#showing-owner-each-change). A release instruction includes necessary schema
migrations; it does not authorize schedule or infrastructure changes. Never silently add options
to the routine runner.
