# Site caretaker

[Documentation index](README.md)

Open **Dashboard → Site functions → Site caretaker → Caretaker checks**.
The caretaker is deployed and its timer is enabled. See the dated
deployment and live verification ledger
for observed runs and remaining coverage gaps.

To address findings, send **`/caretaker`** in your local work-ops coding chat, or
**`/caretaker 53`** for one failure card. The [shared repair workflow](caretaker.md) pulls current
evidence, investigates and fixes within authority, then reports what still needs verification.

Every 15 minutes the caretaker checks the fixed site/tunnel services, scan/backup/email/research
timers, research socket, loopback database health, latest worker receipts, mail backlog/quarantine
and capacity, source outcomes, and backup evidence. It makes **zero model calls**. It never restarts
a service, retries a paid request, repairs records, changes policy or deploys code.

| Evidence | Outcome |
| --- | --- |
| Recent successful worker completion | Healthy for that check |
| Partial source or ingest coverage | Partial; does not clear a previous failure |
| Recent completed research pass (`ok`, or `partial` with no stop reason), even with unverified routes | Healthy for research execution; the gaps stay on Research |
| Active bounded run | Running; does not prove recovery |
| Research pass stopped at its own per-pass call cap, or by the daily/weekly invocation or account limit | Waiting; a stop reason is not recovery evidence. The detail names which limit stopped it; evidence still expires |
| Explicit pause marker | Paused; does not prove recovery |
| Stopped required timer/service or missing/stale receipt | Needs attention |
| Off-host restore receipt absent from VPS | Unknown; inspect the Mac evidence |
| Caretaker receipt older than 45 minutes | Site panel becomes Overdue |

Receipt deadlines: email 45 minutes, research 16 hours, daily ingest and nightly backup 30 hours.
Running deadlines: email 15 minutes, research 50 minutes, ingest two hours. Mail capacity warns at
750 archived messages or 48 MiB; pending/quarantined/unprocessed entries need inspection. The scan's
source outcomes remain individually visible, and its existing source-incident policy owns those
incidents to avoid a second set of duplicate decisions.

Other failures use one stable decision subject per check. Rechecking the same failure adds no new
decision. Healthy evidence can withdraw an open incident; partial/unknown/waiting/paused states do
not. A decision the owner has left for now stays parked until a successful check followed by failure.
The receipt records stable fingerprints and raised/cleared IDs. Receipts and decision history are
kept in the database; private detailed logs are inspected only when needed.

Since 24 October each stored receipt holds only the checks that changed since the receipt named in
its `base` (plus `ids` when checks were added, removed or reordered); a full list is stored at least
once a day. No check is lost: `record_views.caretaker_checks` rebuilds any receipt's full list, and
the site panel and `caretaker_processing` in `wo state` use it. A raw receipt in the full state's
`coverage.runs` therefore shows changes only. On 240 real receipts this stored 95% less and rebuilt
every list exactly (measurements).

The caretaker runs on the VPS being checked: if that server or its database is unavailable, it
cannot publish an incident. A previously open page will age the last receipt to Overdue. There is
no independent uptime service or external notification channel. Tunnel process health is checked;
end-to-end sign-in through Cloudflare still needs the normal hosted checks. The Mac's off-host copy
and restore drill are not silently claimed as VPS-verified.

## When a job fails

The caretaker cannot read the system journal, so each job leaves its own cause. When research, email,
ingest (`wo ingest`) or the caretaker dies on an unexpected error, or a site read or save returns
500, it writes `/srv/work-ops/data/crashes/<unit>.json`: the time, the error type, the code path
(the last three of the repository's own frames) and the last ten failure times. Only sqlite's
own message is kept, because other messages can contain input. The backup script records its
failing line and exit code. The caretaker adds the latest cause to the card it raises for that
unit or receipt, for example `Last error …: OperationalError: database is locked at
records.py:54 transaction <- email_worker.py:100 save_receipt <- research_worker.py:545 run`.
Three failures within 24 hours raise the unit's card even if the next run succeeded. That card
clears when 24 hours pass with fewer than three. A start that overlaps a run still in progress
also counts as a failure: it is a missed run.

The overdue watch, which runs after every email import (15 minutes), the daily ingest and the
nightly backup, also checks the caretaker itself. If its newest receipt is more than 2 hours old,
it raises **The site caretaker is overdue**, with its own last error, and clears it once the
caretaker runs again. An authorized operator with server-shell access can inspect the note, then
run `sudo journalctl -u <unit> -n 60` on the VPS for the full traceback. The restricted VPS Codex
gateway exposes receipts and failure cards, not these files or the journal; use
[the caretaker workflow](caretaker.md) and leave shell-only diagnosis to that operator.

### 21 October: research lost its receipt to a write lock

At 19:45:45 UTC a research retry crashed saving its final receipt (`database is locked`). The
15-minute email run holds the writer lock for most of a run that took 21–25 s (measured
20–22 October), even when no mail arrived. Research's 15 s sqlite wait ran out, so the database
write lock was held for at least 15 s. The caretaker raised the unit and research cards on its own, but the cause was
only in the journal. Fixes: research, email and the caretaker wait up to 120 s
(`db.WORKER_LOCK_WAIT`, `1cb6100`), and the failure notes above. A later wait logs
`database is locked` with the unit, run id, pid and wait, and the crash note keeps that run id.
The email receipt copies `phase_seconds`, `seconds` and `import.write_lock_seconds` from the import.

Exposure: ingest, the email import, backup and the caretaker's writes queue on `.writer.lock`,
a file lock with no timeout, so they cannot hit this. Research receipts and allowance rows,
the email worker's own receipts, site saves and `wo-live` commands write without it. Site saves
keep 15 s so a click never hangs; the journal showed none failing in the 7 days to 22 October.
The cause remains: each email run makes a pre-import backup and imports even when nothing new
arrived. The email receipt's phase timings and the journal's lock lines name which step held the
lock. Skipping the empty import is still a follow-up, not built.

See [pause, inspection and recovery](../ops/OPERATIONS.md#focused-research-and-caretaker-recovery).
