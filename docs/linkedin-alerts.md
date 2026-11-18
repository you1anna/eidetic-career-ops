# Job-alert email processing on the VPS

[Documentation index](README.md) · [Repository home](../README.md)

For everyday use, see [email alerts explained](../USER-GUIDE.md#linkedin-email-alerts).
Current activation evidence owns what is actually deployed
and verified; the migration plan owns its test ledger.

For a visual explanation, see the [full system, email and research diagrams](system-map.md).

## Current path and responsibilities

The shared sender/label registry is [config/email-sources.json](../config/email-sources.json).
It contains LinkedIn’s existing sender and `jack@jackandjill.ai`. Jack and Jill support is built
locally; the plan index records whether buffer/VPS release and selective forwarding have been
activated. Never describe adding the registry entry as proof of delivery.

Jack and Jill uses `alert:jackandjill`, a native job UUID and canonical `/jobs/<uuid>/post` URL;
tracking parameters do not create another job. Its parser accepts role paragraphs or plain-text
Markdown, ignores quoted replies and footer links, and quarantines ambiguous/malformed cards.
Each paragraph owns its stated location/pay; company money is not remuneration. Generated prose
is kept as private `email_summary`, never advert text or a suitability requirement. Original adverts
must be read through existing research; redirects to sign-in trigger bounded public-source discovery.

Both sources retain independent counts, quarantine and last successful processing in the existing
email receipt. The worker carries source success forward across empty/failed polls; the projection
reads the latest receipt without scanning mail or adding a per-source receipt-history scan. One
source’s success cannot hide another’s parsing failure or 36-hour silence.

### Activating another registered sender

Finish local/strict checks and obtain the applicable release/account authority. Deploy the buffered
Worker with `wrangler.vps.jsonc`, release the VPS through the shared deployment runner, then add an
exact-sender Gmail filter forwarding to the already verified `alerts@example.com` address.
Keep originals and Inbox/unread state; global forwarding stays off. The matching filter should
label Jack and Jill mail **Work-ops/Jack and Jill alerts**. Do not forward the entire domain.

Backfill retained originals deliberately with their original Gmail receipt timestamps through the
canonical import route. Do not reset the normal spool checkpoint. Reconcile every URL with its
admission/rejection/deferral result, replay to prove a no-op, and check a later genuinely new email
through scheduled delivery. Manual backfill does not prove forwarding. Roll back a faulty source
by removing its specific forwarding filter and restoring compatible code; retain pending mail.

### Existing LinkedIn delivery


Gmail forwards **only** `jobalerts-noreply@linkedin.com` to `alerts@example.com`, keeping
originals, Inbox visibility and the **Work-ops/LinkedIn alerts** label. Global forwarding stays off.
Cloudflare Email Routing sends that address to `work-ops-email-buffer`. Its D1 binding retains raw
MIME before successful acceptance; the VPS polls authenticated HTTPS every 15 minutes.
No Mac, Codex process, Gmail connector session, inbound VPS port or Gmail mailbox credential is
required for this path. Other domain mail routes are unchanged.

The 17 October security fixes
deployed the admission and collection recovery controls below to the Worker and VPS (`9acbfe2`).
Fresh forwarded-mail delivery was subsequently reconciled in the
20 October caretaker check.
The activation index above owns current status; this does not establish exhaustive alert coverage.

| Component | Responsibility |
| --- | --- |
| `ops/email-intake/src/buffered.ts` / `wrangler.vps.jsonc` | Durable incoming-mail buffer, bounded authenticated `/v1/messages` and `/v1/ack`; no sending or browser interface. The older `src/index.ts` direct-POST pilot is not deployed. |
| `tools/email_worker.py`, `wo-email.service`, `wo-email.timer` | Pull, SHA-256 verification, durable local receipt, acknowledgment, parser/import and run receipts. Runs every 15 minutes, catches a missed timer on server restart. |
| `tools/mail_intake.py` | Existing private SQLite spool at `/srv/work-ops/data/email/mail.db`; replay preserves the first receipt and timestamp. No HTTP listener is needed for the pull design. |
| `tools/scan_alerts.py` and `tools/ingest.py` | Existing role rules, retained exclusions/quarantine, canonical identity/import, shared writer lock and human-edit preservation. Email imports use `email-import:` receipts so they cannot disguise a missed general daily scan. A run whose alert read staged no jobs records that read and skips the backup and import replay ([incremental runs](data-pipeline.md#the-daily-run)). |
| `workspace/record_views.py` / Jobs | Safe `email_processing` projection from `email:` receipts; no raw mail, credentials or personal email contents in the status. |
| `wo-backup.sh` / `tools/backup_email.py` | Nightly encrypted spool + reader-state snapshot, alongside the existing site backup; 30 encrypted email snapshots retained on the VPS. |

The server's token is systemd-encrypted as `WO_EMAIL_BUFFER_TOKEN` and exposed only to
`wo-email.service`. The same dedicated token is a Worker secret. It has no Cloudflare account
administration permissions; deployment OAuth credentials stay on the operator's Mac. Logs print
safe failure types and counts, never message contents or credentials.

The Worker accepts only an unambiguous original MIME `From`
mailbox from the shared registry or `forwarding-noreply@google.com` (destination
confirmation). Display names and folded headers are supported; unrelated, missing, malformed or
multiple senders are permanently rejected before storage. This application filter relies on
Cloudflare's upstream DMARC/ARC enforcement; a `From` match alone is not proof of authenticity or
that mail traversed the owner's Gmail. Caller-supplied `Authentication-Results` is never trusted.
Verify a real forwarded alert after release before declaring this admission path accepted.

## What successful processing means

A completed run must both collect successfully and finish parsing/import. Empty polling without an
actual alert remains **Waiting for first job alert**. The site separately exposes last attempt,
last successful check, last email arrival and last successful job-alert processing. Partial or
failed runs never replace overall last-success evidence. A source whose parsing/import completed
can retain that success when collection or another source failed; the overall failure stays visible.
A dead/stalled process stays visible; the browser
ages the displayed timestamp into **Overdue** after 45 minutes. No alert for 36 hours warns about
input silence even when polling itself succeeds. These are displayed warnings, not an external
paging service. An open page re-reads the run statuses when you return to it and every five minutes
while it is visible, so the warning judges the newest receipt; if that read fails, it keeps ageing.

The parser processes at most 200 mail receipts per pass. Matching candidates enter Jobs through the
existing importer, with original alert/receipt provenance. Jack and Jill paragraph digests and
`jj-card` radar layouts keep each role's title, employer/location and summary separate. Repeated
advert buttons do not create roles; conflicting identities or incomplete cards stay quarantined.
Explicit card salary/rate lines (including a pay range in a radar title) are retained as pay evidence
and normalized through the shared terms rules; salary alone does not establish permanent employment.
Previously processed mail is not silently replayed after a parser change: existing pay gaps remain
until a later alert or verified source supplies them. Imports do not imply advert availability,
suitability, hiring authority or a current contact route. Detailed research and tailored notes remain
separate work; only explicit source observations can refresh vacancy or contact checks. Network hiring
posts and recruiter messages are outside this sender filter. Nothing sends outreach or applies.

### Company logos and receipt counts

The repository parser retains an optional `current.company_logo_url` beside the job's pay text.
It accepts an image only inside the anchor for that exact LinkedIn job: HTTPS, hostname
`media.licdn.com`, `/dms/image/` and a `company-logo` path marker, with no credentials or alternate
port. Signed query parameters stay unchanged. Badges, pixels, attachments and images outside their
own job anchor are ignored. Import and compaction preserve this fact and its email provenance;
adding support does not replay previously processed mail.

The read projection selects the newest valid logo evidence for an unambiguous canonical company,
including confirmed merged names. Older jobs from that company use the same projected logo.
An agency logo stays with the agency's name; an ambiguous employer/client association gets initials.
Other sources may supply a URL through the same existing observation field and permitted origin;
this creates no provider request or broader image-host permission.

The browser reserves a 40px mobile / 64px desktop square beside the identity block and assigns the
image URL only when it enters the viewport. It requests the URL directly with no referrer and
asynchronous decoding. JavaScript retains initials until a successful load and restores them on
failure; browsers without Intersection Observer retain initials. The server's only new CSP
directive is `img-src 'self' https://media.licdn.com`. There is no server downloader, media service
or image cache. Deferred loading reduces initial work but still uses browser bandwidth and decoding.

The existing processing receipt and `email_processing.processing` projection contain:

| Field | Meaning |
| --- | --- |
| `cards` | Parsed cards before job deduplication across emails, and within the measured LinkedIn card layout. Repeated logo/content/title anchors within a card count once. |
| `logo_cards` | Cards with an accepted logo URL. |
| `logo_expired` / `logo_expiring_soon` | Reserved classification counts; “soon” means within seven days once expiry semantics are validated. Both currently remain zero. |
| `logo_expiry_unknown` | Accepted logos whose expiry cannot be established. This currently includes every accepted logo. |

The numeric shape of LinkedIn's `e` parameter does not establish its meaning. No retained evidence
currently validates it as an expiry timestamp, so no expiry is inferred. Image expiry never
establishes vacancy availability. Older/simple layouts without the measured card boundary retain
the existing grouping by job ID within an email. Receipts contain counts, not signed image URLs.

Local synthetic tests cover boundaries, invalid images, replay, compaction and company identity.
Parsing a retained original alert makes no image request and establishes neither link validity
nor future coverage. The
delivery record
owns measured local verification, deployment, scheduled receipts and the owner's device acceptance
as separate evidence.

## Agent run and recovery procedure

1. Read the Jobs status or the `email_processing` field from `ops/wo-live state`; inspect specific
   imported records with `ops/wo-live show <id>`. Do not query the live database through raw SSH.
   For service diagnosis, inspect `systemctl status wo-email.timer wo-email.service` and the service
   journal. A running timer alone is not proof of forwarding or import.
2. Reconcile an original Gmail alert against its retained MIME hash/receipt and `alert:linkedin`
   source evidence. Read/import data through the approved gateway. Do not treat an old manual replay,
   Google destination-verification email, empty buffer or successful process exit as fresh delivery.
3. A lost acknowledgment replays identical bytes safely. Cloudflare releases the raw buffer copy
   only after the VPS confirms durable local storage; receipt identities remain for deduplication.
   If collection fails, processing still attempts previously retained mail. The next timer retries
   collection and pending imports. One malformed alert is quarantined while other valid alerts
   proceed. Parser quarantine requires an attended fix and deliberate `--retry-quarantine` recovery;
   ordinary empty runs cannot hide it. Preserve original check timestamps on retries. Equal-time
   receipts use their retained spool sequence to prevent older recovery mail replacing newer terms.
   For an authorized attended recovery, add `--retry-quarantine` to a one-off invocation of
   `tools/email_worker.py` using the existing email service's user, credentials and sandbox.
   Keep it off the scheduled service. This runs retry, staging and canonical import under the
   normal polling/writer locks and records `retry_quarantine: true` on its receipt. Running only
   the scanner would stage evidence without completing the import. Confirm a later normal poll
   succeeds with zero unresolved quarantine and no replayed role additions.
   Collection also continues past invalid or unretainable deliveries using
   sequence pagination. The run receipt preserves successful/failed counts and a bounded-pass
   `resume_after` cursor, so a failed prefix larger than 200 messages cannot block later deliveries
   on every run. At the end of a sweep it returns to retry the failures. This cursor is separate
   from the parser checkpoint: advancing it never acknowledges or deletes mail. Acknowledgment
   must confirm every durably retained identity; missing/malformed confirmation fails the run.
   Outstanding mail keeps the status partial or failed until recovery, even after useful imports.
4. Investigate mismatches, unsupported formats, failed imports and stale status using the existing
   delivery incident, rather than creating duplicate incidents or declaring success from a partial
   stage. Maintain a private ledger of IDs/receipts, including recovery provenance and exclusions.
   Do not update vacancy/contact checks from email arrival or import time.
5. Both buffers are bounded: 512 KiB per message; 1,000 retained messages / 64 MiB raw bytes in the
   VPS spool and 1,000 unacknowledged messages / 64 MiB raw bytes in D1. Jobs warns at 750 VPS
   messages or 48 MiB; arrange an attended archive/capacity change before full. Neither store silently
   evicts pending mail. Unexpected rejection before durable receipt still needs Gmail reconciliation;
   this does not claim guaranteed SMTP redelivery for every provider failure.
   Admission does not reclaim previously retained unrelated mail or remove the lifetime spool
   limit. Existing contents and reader state require an attended archive/capacity decision.

## Backup and restore

The nightly backup holds the shared import lock, makes a SQLite online backup of the spool, and
archives `pending/alerts` and `pending/observations.json` with it. Encrypted archives are
`/srv/offsite/work-ops/email-<UTC timestamp>.tar.gz.age`. They remain on the VPS: the existing Mac
pull job currently copies the site database archive only. Gmail originals are an independent
recovery source, and D1 retains mail not yet acknowledged. Do not describe the spool archive as
an off-machine backup until that transfer has been verified.

For an authorized restore, stop `wo-email.timer`, stop/wait for `wo-email.service`, and hold the
writer lock. Decrypt into a private staging directory; validate the spool's integrity and receipt
anchor against its checkpoint. Restore spool and reader state together with `workops` ownership
and private permissions. Reconcile observations with the current database before importing; never
replace newer unrelated source evidence with an older snapshot. Start the service, inspect the
receipt and live records, then resume the timer. Mail received after the backup must be reconciled
from retained originals; D1 acknowledgment is not reversible. Never silently reset a mismatched
checkpoint to zero.

## Previous approaches

The temporary Mac bridge and direct-HTTP pilot
are historical evidence. The D1 buffer was chosen for this migration to avoid requiring VPS
availability at email arrival. Do not reactivate the old IMAP or direct-POST designs as routine recovery.

## Twice-daily research

Vacancy/contact research uses the existing **Codex CLI on the VPS**, with its ChatGPT login,
at **09:00 and 19:00 London time**. It maintains active/shortlisted work and ten discovery-side
targets, including six reserved qualification slots for promising email-alert candidates.
Unused slots are shared and candidates rotate by oldest attempt. Import alone never establishes
that the vacancy is open or suitable. It does not collect Gmail alerts or send outreach.
The former **Jobs and contacts: twice-daily checks** desktop automation is paused; routine
research no longer relies on the Mac. The [research runbook](vps-research.md) owns execution,
source-access limits and site status. The focused release ledger
records real-run and scheduled-run evidence separately. Real email collection and import are
recorded in the email ledger.
Research success does not establish email delivery, and email success does not establish freshness.

## Search set

Six saved searches are configured in LinkedIn as daily email and notification alerts, verified
in Manage job alerts on 6 October. The existing Head of Engineering alert was retained; five
new alerts cover Manager, Head QE, QE Lead/AI evaluation, FDE/integration and Founding.
All currently use the existing London 25-mile setting, which also returns London-based remote roles.
The table below records the role families; broader UK-remote searches remain a useful extension.
Contract/interim/FTC are preferred;
retain suitable permanent work. Use London for office/hybrid work and UK for remote work.
No relocation or regular foreign travel; foreign remote clients must fit the agreed UK-hours window.
Review an advert's stated terms rather than infer pay or availability from an alert.

| CV | Search titles |
| --- | --- |
| Director | Head of Engineering, Director of Engineering, VP of Engineering, CTO |
| Manager | Engineering Manager, Senior Engineering Manager, Engineering Lead |
| QE | Head of Quality Engineering, Director of Quality, Head of QA |
| QELead | Quality Engineering Lead, QA Lead, Test Lead, SDET, Test Architect, AI Evaluation, AI Reliability |
| FDE | Forward Deployed Engineer, Integration Engineer, Implementation Engineer, Systems Integration |
| Founding | Founding Engineer, Founding Software Engineer, first engineering hire |

Excludo exclusion is applied centrally after ingestion, independently of search or relevance scores.
Alert silence never closes a job. LinkedIn scraping is not part of this integration.
