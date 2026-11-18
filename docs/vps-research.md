# Job and contact research on the VPS

[Documentation index](README.md)

**Look at [Dashboard](https://work.example.com/#dashboard) → Job and contact research.** This is
separate from email delivery. The focused researcher uses the **Codex CLI already on the VPS,
signed in with ChatGPT**. Its 09:00/19:00 London timer is enabled; the former desktop automation
remains paused. There is no API-key fallback. See the dated
focused release ledger
for live scope, seeded usage and manual versus scheduled verification. Earlier broad-queue model
and import evidence remains in the original cutover ledger.

## What it does

The server owns the queue and runs at **09:00 and 19:00 Europe/London**, with up to 30 seconds of
jitter and a catch-up after a server restart. It reads the canonical site records, checks exact
public adverts and relevant existing contact routes, and maintains evidence assessments. No Mac, desktop chat, signed-in Chrome or Gmail connector is used.

The maintained set is the canonical opportunity projection: active/shortlisted work and ten
discovery-side targets. Six slots are reserved for promising LinkedIn-alert or captured-advert candidates needing
qualification; the remaining slots maintain usable discovery work. Unused capacity is shared.
Both sets rotate by oldest attempt before admission, using the same bounded latest-attempt lookup.
Qualification does not promote a candidate to Discovery or confirm its availability. This includes
suitable permanent roles and evidenced direct-work leads; it excludes personal outreach. Exclusions, skips and unknown terms remain authoritative.
Generic company signals are not specific work. Targets rotate by oldest attempt, active work first
on ties, then two qualification targets alternating with one existing opportunity, preserving both
under the six-call cap. Legacy focus/preparation flags cannot expand
the selection or enable message generation.

The panel separately counts all eligible jobs, those outside the maintained set and focus targets
waiting. A completed focused pass makes no claim about the rest of the database. Each pass allows
**six CLI invocations**, at most 30 targets and about 40 minutes; the service has a 45-minute timeout.
Missing context and blocked pages appear as gaps, not invented contacts or new outreach notes.
Person route checks appear under **Current contact route** on the person's brief. Each person has a
**personal route** check, which needs no employer, and one check per current employer link; a
personal confirmation never confirms an employer route. Stored `; `-separated source lists are read
as separate URLs, private mailbox links are never fetched, and primary pages are read before
LinkedIn (which robots rules block here). A route without a model claim cites the page actually
read, never a blocked profile or a page that was not attempted.

Confirmed checks less than eight hours old need no new read. Otherwise the worker reads sources
again. It can reuse a validated interpretation for seven days only when freshly retrieved text,
record/contact context and policy files match. Reused claims are validated again against the fresh
read. Changed text, record revisions or evidence policy invalidate reuse. Failed reads have a
24-hour cooldown that retains the actual failed-read timestamp; discovery has a seven-day cache.
None of these caches extends the 24-hour freshness window without a successful source read.

The model uses a small, selected work/contact context and factual requirements. It cannot execute
shell commands or mutate records. Public search finds candidate URLs; the server then reads the
actual pages. A search snippet cannot establish freshness. Only a matching, actually retrieved
source quote can support a confirmation, stamped with that read's time. Login-only pages, bot
challenges and blocked reads remain unverified; the worker does not transfer browser cookies or
change identities to bypass them. Public network posts can be found during research, but there is
no complete LinkedIn network-feed monitor.

Validated findings pass through the existing research importer. It preserves human edits, canonical
identities, workflow states and evidence URLs. Existing or deliberately cleared notes are never
replaced. No email, LinkedIn message or application is sent. Your job brief remains the place to
read the context, use the contact/CV/application links and act yourself.

## Reading the status

- **Researching:** the worker has recorded its attempt and is still processing.
- **Checks completed:** the selected work set finished with no recorded vacancy/contact gaps.
  Explicit closure can be a completed check; it does not mean every advert is open.
- **Finished with gaps:** some sources/contact routes remain unverified, or jobs are waiting. A
  pass that made its six permitted calls finished its share; its remaining targets wait for the next
  pass (`record_views.research_pass_capped`).
- **Nothing confirmed in N passes — needs attention:** at least four completed passes in a row
  confirmed nothing: no advert open or closed, no contact route and no target fully checked or still
  current (`record_views.research_confirmed`). Calls are being spent without new confirmations.
  The panel names the last pass that confirmed something within the latest 60 receipts. Blocked,
  interrupted and failed passes are execution states and neither extend nor end the run. It raises
  no failure card: diagnosis is agent work in a work-ops chat, not record-by-record triage. A
  target left decision-ready but not fully checked does not count as a confirmation.
- **Paused — Codex usage limit:** the Codex account allowance or the daily/weekly invocation limit
  prevents another request. Existing data remains usable.
- **Blocked — Codex sign-in needed / Failed:** the run cannot proceed; inspect the reason.
- **Stalled / Overdue:** the recorded attempt is too old. A stopped worker cannot stay green.

The panel shows when the latest attempt started and finished. Details separately record the last
pass without coverage gaps. It shows adverts confirmed open,
contact-route coverage and waiting jobs. **Research coverage** opens the issue links and CLI invocation/token counts. Reload to fetch a new receipt; elapsed-time warnings update while the page stays open.
Expected usage-limit and disabled outcomes exit successfully at the process level while retaining
their blocked/partial receipt. Sign-in and operational failures still exit nonzero.
An advert counts as open for 48 hours after its source last listed it or research last confirmed
it (`evidence_checks.advert_availability`); contact confirmations expire after 24 hours. Adverts on
Greenhouse, Lever or Ashby are confirmed from the employer board's public API by posting ID, with no
model call. A pass reads each board API once and keeps only its posting IDs, so several adverts on
one Ashby board cost one request. When only the board can be read, unsupported contact checks
retain that read's URL and timestamp and stay unverified. Saving or replaying the result cannot
invent a newer check or claim that a contact profile was read. Routine process completion never
refreshes those timestamps. There is no external paging service.

## Codex access and usage

The existing `/home/codex/.local/bin/codex` runs as the dedicated `codex` account and reuses its
ChatGPT sign-in. Authentication stays in that account; it is not copied to `workops` or the web
service. The runner forces ChatGPT authentication and has **no API-key fallback**. This consumes
the owner's existing Codex allowance; it does not create a separate $2/day API budget. It cannot report
account-wide allowance remaining from token counts alone. An account limit stops the pass visibly.

`wo-research.service` runs as `workops` and sends a bounded packet through a group-restricted Unix
socket. A socket-activated process runs `codex exec` as `codex`, which cannot access the production
data directory. It receives selected research context and public excerpts, not the entire database
or mail archive. Its structured result returns to the trusted worker for quote/identity validation
and canonical import. The scheduled model session never receives a database-writing command.
The separate [attended VPS record gateway](../ops/OPERATIONS.md#vps-codex-record-access) permits
requested record work from the Codex checkout; it does not give the research model shell tools,
database-file access or deployment authority.

The runner keeps the existing account's model choice, `gpt-6-astra` with high reasoning, but ignores
general development config. It uses ephemeral read-only sessions, disables shell, apps, plugins,
hooks, browser/computer tools and other agents, and permits web search only for URL discovery.
Each invocation has a five-minute timeout. The per-pass maximum is six invocations.
There is no immediate model retry after an uncertain outcome. Limits live in `config/research.yml`
and the service/runner files. No model-key credential is needed in `.env`.

If sign-in expires, an operator runs `codex login --device-auth` as the `codex` user and the owner
completes the browser authorization. Do not print or copy `auth.json`. Normal token refresh stays
on the server. There is no Mac dependency for routine execution.

## Operator ownership and recovery

| File or service | Responsibility |
| --- | --- |
| `tools/research_worker.py` | Work selection, per-item receipts, canonical import and crash reconciliation |
| `tools/approach_preparation.py` | Weekly preparation: survey, packet, deterministic note checks, rechecked atomic save and the weekly report |
| `tools/research_sources.py` | Public-only HTTP/HTTPS, DNS/redirect checks, robots rules and source evidence |
| `tools/research_model.py` / `research_codex.py` / `research_schemas.py` | Private socket client, bounded ChatGPT-authenticated CLI execution and structured validation |
| `workspace/migrations/016_research_budget.sql` | Retained historical schema; the Codex worker makes no cost-ledger writes |
| `ops/vps/wo-research.service` / `.timer` | Worker runtime bounds and twice-daily schedule |
| `ops/vps/wo-research-codex.socket` / `wo-research-codex@.service` | Group-restricted socket and isolated Codex account process |
| `workspace/record_views.py` / `workspace/js/research-status.js` | Safe site status and aging warnings (`preparation-status.js` is retired compatibility code) |

Use `ops/wo-live state` to read `research_processing` and `ops/wo-live show <id>` for record evidence.
Service diagnostics use `systemctl status wo-research.timer wo-research.service` and
`journalctl -u wo-research.service`; logs contain aggregate counts/fixed error types, not credentials
or source bodies. Record reads/writes remain through the approved gateway.

The worker stores immutable private source/finding batches under `/srv/work-ops/data/research-server/`.
It persists their filename and SHA-256 before import. After interruption it reconciles retained
batches through the same importer before making fresh model calls. Exact replay does not duplicate
observations or replace notes. The canonical database backup includes findings, run receipts and
recorded usage; raw batch files currently remain on the VPS. After a database-only restore,
missing batches are reported and checked afresh. Retained batches can be reconciled without another model call. A database restore does not reset
the account-wide Codex allowance.

Deployment uses schema 17 and follows the normal release gate. It installs root-owned runner
code in `/usr/local/lib/work-ops-research/` and the socket/service units. Routine deploys preserve
existing timer states. On a new installation the caretaker timer starts disabled and needs explicit
activation; this installation has been activated. Verify scheduled execution separately from a
manual run. The release ledger records the observed outcomes.

Official references checked 17 October 2026: [noninteractive Codex](https://learn.chatgpt.com/docs/non-interactive-mode)
and [ChatGPT authentication](https://learn.chatgpt.com/docs/auth). The installed CLI help and a real
noninteractive structured-output probe confirmed support on this VPS.

## Durable usage caps

Version-controlled [config/research.yml](../config/research.yml) owns these invocation limits;
[configuration ownership](configuration.md#focused-automation-migration-017) explains changes and release. Provider HTTP
attempts have a separate shared [collection policy](configuration.md#collection-policy).

| Lane | Daily maximum | Weekly maximum |
| --- | ---: | ---: |
| Research | 12 | 84 |
| Incident diagnosis (not implemented) | 4 | 4 |
| Card questions (not implemented) | 6 | 6 |
| Weekly improvement review (not implemented) | 1 | 1 |
| Evaluation (not implemented) | 5 | 5 |
| **All unattended lanes combined** | **16** | **100** |

Days and Monday-start weeks use Europe/London. Unused lanes do not top up research. The caretaker
uses zero model calls. These limits count CLI invocations, **not credits or individual internal
model turns**; one invocation may consume more tokens than another. Astra/high stays unchanged
until a separately authorized quality/cost evaluation. There is no API fallback or automatic top-up.

`tools/automation_usage.py` reserves an invocation in SQLite before dispatch. Completed, failed,
timed-out and unknown outcomes all count. Restarting a worker or changing its run ID cannot reset
the allowance. Migration cutover imports historical receipt counts once; the first focused run may
therefore wait until the next day or week. An interrupted legacy pass also reserves one uncertain
in-flight invocation, since older receipts could lose the last dispatch count on a crash. All future workers must use this same admission ledger
and lock. Both the caller and socket runner serialize execution, including a client dying mid-call.
The limits cover this site's unattended workers, not the owner's other Codex chats or interactive CLI use.

Database backups contain the ledger and interpretation cache. **Restoring an older backup can lose
reservations made after that backup.** Pause research before restoring. Reconcile known subsequent
usage through an attended recovery, or keep it paused until the following London Monday before
resuming. Never delete accounting rows or the seed marker to get more allowance.

See [operator pause, replay and recovery](../ops/OPERATIONS.md#focused-research-and-caretaker-recovery)
and [caretaker behavior](vps-caretaker.md).

## Weekly approach preparation

Retired by the opportunity-led workflow. Existing notes, immutable versions, decisions, receipts,
usage reservations and synthetic previews are preserved. No normal, alternate or recovery producer
creates a new outgoing message or approval. Legacy preparation batches are digest-checked, retained
and recorded as retired without importing their wording or silently resolving a decision. Full
exports retain the original receipts; browser summaries omit the old preparation panel.

The caretaker no longer supervises weekly preparation. It continues supervising evidence research
and existing operational services. Research schedules/caps are unchanged. There is no destructive
history migration. Use compatible code or a data-preserving forward repair for recovery.

### Evaluate preparation before activation

Activation and message-producing evaluations are retired. Existing reports and attended comparisons
remain readable and scoreable for historical analysis; their technical passes do not establish
usefulness of the current workflow. [Preserved report commands](commands.md#evaluate-weekly-preparation).
Use the [four opportunity-workflow tasks](../USER-GUIDE.md#validate-the-outreach-features) for current
human acceptance.

## One-time Monday acceptance

The follow-up for **Monday 19 October 2026** runs on the VPS at **10:05 Europe/London**,
independently of the Mac. The absolute-date `wo-research-acceptance.timer` runs once, after the
09:00 research service's 45-minute limit and a later caretaker tick. It does not start research.
The replaced desktop follow-up is paused after server activation is verified.

The verifier opens a read-only database snapshot, matches the morning receipt and timer trigger,
counts durable reservations including unknown outcomes, and reads back canonical imports against
hashed retained batches. Fresh confirmations need matching source quotes and actual check times;
retained facts, explicit closures, note presence, coverage gaps and later caretaker state are
reported separately. It makes **zero model calls and zero source requests**, cannot repair records,
and cannot change usage accounting. A useful-evidence count does not establish human usefulness,
contact willingness or hiring authority. Without a before-run snapshot it cannot independently
reconstruct all note/workflow preservation; those worker invariants have automated coverage.

It writes `report.md`, `report.json` and a hash manifest under
`/srv/work-ops/data/research-acceptance/2026-10-19/`. Completion is immutable and a repeated start
does no new work. Unexpected process failures have at most three starts per hour, five minutes
apart; an honest incomplete/failed research outcome is recorded once, without paid retries.
The timer uses a persistent catch-up if the VPS was offline. Late catch-up may no longer corroborate
that morning's timer trigger; the report keeps that verification gap explicit.

Read the verified Markdown through `ops/wo-live research-review --date 2026-10-19`. Before the
check completes this returns a clear “No completed acceptance report” message. The report contains
aggregate evidence, not personal names, advert text or credentials. It remains a private server
artifact; the normal database backup does not include these report files.

**Documentation delivery:** this server report is generated without the Mac. The VPS GitHub key is
read-only, so it cannot commit/push the repository's release ledger. At the next attended session,
read the report through the gateway, reconcile its evidence with the dated release ledger, then
commit/push the documentation normally. No GitHub write credential, autonomous deployment or
external notification channel is added. The report is available on demand through the gateway;
it does not post back into this desktop chat by itself.

[Inspect, stop and recover the follow-up](../ops/OPERATIONS.md#one-time-research-acceptance).

## Evidence briefs

**Enabled in configuration with `briefs: true`; release and activation evidence lives in the
delivery queue.** The work assessment combines remit, canonical fit, demand evidence, terms and decisive unknowns.
Relationship context stays on People; generated wording never becomes factual evidence. Public evidence
retains its source, quotation and retrieval date. Record ownership is not authorship: a later agent
correction is shown as recorded context even if the owner previously owned that field.

When activated, the existing focused pass maintains these briefs without creating or replacing
messages. It follows the same active/shortlisted/top-ten opportunity selection and oldest-attempt rotation. Existing and cleared notes do not prevent evidence maintenance.

The worker reads known pages and can discover up to four alternatives where public evidence is
missing. Discovery requires a recorded employer or exact profile, never a name alone. At most one
discovery and one interpretation invocation per target use the existing six-call pass and shared
daily/weekly caps. The new `brief` socket operation returns structured evidence, not instructions
or message prose. Excerpts and packet size remain bounded; private mail is not retrieved.

Literal current-employer/role claims require supported subject-bound wording. Unsupported claims,
negated statements and historical roles are withheld. Context excerpts stay quotations rather than
model paraphrases. A newly found public profile is a route to review, not verified deliverability;
it cannot silently replace an explicitly selected email/phone channel. New employment evidence
does not rewrite protected fields or organisation links. Unknowns and conflicts remain visible.

The source reader, cache, usage reservations, immutable batches, canonical importer and replay are
shared with existing research. Replay rebuilds validation from the retained response and excerpts,
and rechecks the record, contacts, week, interactions and focus eligibility. Actual evidence dates
do not change. Complete unchanged briefs with current route checks require no interpretation call.
Incomplete or blocked sources remain gaps; usage waits remain partial receipts.

**Context available** refers to minimum recorded context, not approval to send or complete public
verification. Optional gaps are shown separately. A personal catch-up does not require a current
employer; an employer-specific internal-advocate introduction does. Job approaches require a fresh
vacancy check. Missing relationship context or intent needs the owner's input, not another public search.
The worker reports sufficient briefs and unresolved/blocking gaps alongside calls and tokens.

The owner's 19 October instruction authorises deploying and enabling briefs and preparation together
for real-use validation, superseding the earlier staged evaluation/activation gate. Missing eligible
notes are prepared first; other targets receive brief maintenance. Newly prepared targets receive
brief maintenance on subsequent passes. Both features share the same six-call pass and durable
day/week limits; neither receives a separate allowance. Existing notes are never cleared to create
a generation test. The [bounded evaluation](commands.md#evaluate-evidence-briefs) remains available
as separate evidence, not a substitute for real use.
After activation, inspect the first actual timer receipt and canonical record readback through
`ops/wo-live`; a fixture evaluation is not live coverage. To stop evidence maintenance only, release
`briefs: false`. The existing research pause file stops the entire research worker.
