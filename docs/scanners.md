# Scanner reference

[Documentation index](README.md) · [Repository home](../README.md)

Source definitions live in `config/portals.yml`, collection policy in `config/collection.yml`,
and source descriptions in `config/channels.yml`.

## Adapters

| Command | Sources | Market | Current access |
| --- | --- | --- | --- |
| `scan_boards.py` | Greenhouse, Lever, Ashby, SmartRecruiters, Rippling | employer headcount | public readers; approved daily VPS run |
| `scan_boards.py` feeds | Adzuna, Reed | agency and employer adverts, contract and permanent | daily on the VPS since 1 Oct (#39); probes passed 24 Sep |
| `scan_tenders.py` | Contracts Finder, Find a Tender | public contract demand | approved daily VPS run; schemas probed |
| `scan_awards.py` | Contracts Finder, Find a Tender awards | supplier delivery-partner research | free public APIs; joins daily ingest under the owner's 4 Oct source-expansion request |
| `scan_hn.py` | monthly “Who is hiring?” thread | founder-posted work | approved daily VPS run; endpoint probed |
| `scan_alerts.py` | Supported job-alert emails in the private VPS spool | shared role families | separate `wo-email.timer`, every 15 minutes; LinkedIn forwarding configured; Jack and Jill pending activation; current delivery proof in the activation ledger |
| `scan_news.py` | UKTN RSS (tech.eu off since 1 Oct, #33) | companies that have just raised money (trigger signals, not adverts) | daily; feeds probed 25 Sep |
| `scan_companies_house.py` | Companies House Public Data API | registered status, current officers and share allotments for companies to approach with a recorded number | monthly or newly relevant chosen work; never matches a name |

`wo ingest` runs all of them (see [data-pipeline.md](data-pipeline.md#the-daily-run)). On the VPS,
`wo-scan.timer` runs `wo ingest --keyless` daily: the free sources (decision #18) plus Companies House
(#29) and the Adzuna and Reed feeds (#39), each added by name while its key is installed.

```sh
ops/wo-live collection-policy --json
.venv/bin/python tools/coverage_report.py --config config --data data
ops/check.sh unit
```

Use `--offline-fixture` on individual adapters for local fixture development (their `--help`
documents the directory/file format); the tests build isolated synthetic configurations and stores.
Production scans run through ingest so every adapter gets the same demand snapshot. A standalone
scan without that context defers dispatch. Probes leave records unchanged but consume the shared
provider allowance, so run them only through the existing authorized VPS service-user route.

## Configuration

`tracked_companies` supplies a company name and supported careers URL. Provider and slug are
inferred for Greenhouse, Lever, Ashby and SmartRecruiters. Unsupported URLs are isolated.
Source coverage derives active board scopes from these same URLs: retired receipts remain in
history but cannot make the replacement source look broken. A newly configured board stays unread
until it has a receipt. Remote.com moved to its own ATS; its old Greenhouse reader is disabled and
the current openings page is an attended source. Wayve's active reader is Ashby.

`feeds` supplies provider query parameters and environment-variable names. Credentials are never
stored in configuration; `tools/credentials.py` reads them from the local environment loaded by `wo ingest` from the gitignored
`.env`, or the scan service's decrypted systemd credential directory on the VPS. Each feed is the union
of exact-phrase searches over `role_phrases`, covering contract and permanent work (Adzuna's
`what_or` matches single words, and it filters engagement with `contract=1`/`permanent=1`, not
`contract_type`). Feeds are presence-only: absence never closes an advert; Reed's stated
`expirationDate` does. Reed search summaries are enriched with its documented job-detail endpoint,
read once per advert and cached in `data/pending/feeds/reed-details.json`. Each run reports attempted
calls, including failures. Overlapping search results are deduplicated after retrieval; this does
not save the search requests themselves.

[Collection policy](configuration.md#collection-policy) owns durable daily limits and scheduling.
Every recognised provider attempt passes through `tools/collection_budget.py` at the shared HTTP
boundary, including research, probes, generic fetch, retries, redirects and network-enabled dry
runs. The authority is `/srv/work-ops/data/collection.db`; local development uses fixtures. Use the
existing approved VPS route for attended API reads. A local deferral never triggers another-host,
browser or alternate-URL fallback. Browser page research blocks covered API subrequests.

Scanners reuse one ingest context derived from the shared selection/admission projection. Due
shortlisted/active work precedes fair exploration; task cursors advance on attempted failures too.
Denied tasks remain pending. Adzuna reads first pages only and records finding-query provenance in
observations; old records without that mapping get fair rotation without a promised refresh. Reed
keeps its unchanged-detail cache and enforces four searches/two detail attempts per scheduled day.
Current vacancy evidence still expires after 48 hours, even if its provider budget is exhausted.

When allowance ends after successful pages, retain those rows as incomplete evidence with
`stop_reason: budget`. A skip before dispatch creates a `deferred` receipt, never an empty source,
success, absence or new last-check timestamp. Authentication/rate-limit stops retain their existing
failure semantics. Partial reads cannot establish absence or clear an earlier source failure.
Source-health cadence follows policy/rotation; vacancy ageing remains a separate rule.

Companies House refreshes evidenced numbers monthly or after newly chosen work needs them. Changed
numbers select a new target; failed/incomplete bundles retain earlier facts and their dates.
Procurement and awards rotate within their shared provider allowance. Background sources run on
Tuesday/Friday while intake is full. Timers still invoke the same workers at their existing times.

`wo collection-policy --json` reads policy and indexed accounting without network access; Dashboard
→ Quotas and usage shows used/reserved attempts, scheduling deferrals and actual check dates.
Provider account allowances are secondary. Historical feed receipts retain their original counts;
they cannot establish a complete pre-cutover daily ledger. Use `--reuse-pending` for import replay.

`alerts` in `config/portals.yml` selects `transport: spool`, the private spool path and allowed
sender. The separate VPS email worker collects from Cloudflare using `WO_EMAIL_BUFFER_TOKEN`,
then invokes this parser and canonical ingestion. Gmail credentials and IMAP are not part of the
production path. The older IMAP adapter remains compatibility code, not an active service.
Raw MIME is retained by the spool; the parser keeps checkpoints, exclusions and quarantine.
Email dates remain alert evidence, never advert publication or current vacancy/contact checks.
The [email runbook](linkedin-alerts.md) owns transport, limits, retries and first-delivery evidence.

`tenders.filter` contains CPV prefixes, keywords, negatives and the minimum stated value. An
unstated value remains eligible for review. Both tender APIs use cursor pagination; Contracts
Finder uses `limit`, not page-number pagination.

`commercial_awards.sources` selects award readers. The reader uses the tender relevance settings
plus a software-delivery context check; generic medical/electrical testing is not a work lead.
Each provider is limited to two pages of 100 notices and a seven-day publication/update window.
Page caps report `partial`, never complete coverage. Actual award dates and contract periods stay
separate from notice updates: a recently updated historical award is not a recent win. Award or
contract values are retained with their scope and supplier count, never treated as subcontract budget.

`hn_who_is_hiring` bounds comment paging for one monthly story. Direct child comments are evaluated
with shared title, location and engagement rules. Replies and unsuitable comments remain archived
as ineligible observations and are not surfaced as candidates.

## Discovery archives

`data/pending/<date>.tsv` contains newly eligible job discoveries with normalized engagement, rate,
pay, workplace and IR35. `data/pending/hn/<date>.tsv` contains eligible direct HN hiring comments.

`data/pending/tenders/<date>.tsv` contains eligible tender releases. Identity is source-scoped
release ID (`source_key`); `release_id` identifies the notice/release and `ocid` groups releases in
one procurement process. Multiple releases under one OCID remain distinct. Rows retain buyer,
value, deadline, CPV, source URL and summary.

Each archive has `seen.json` for propose-once behavior and `last-run.json` for run receipts. These
are historical discovery and reconciliation inputs. SQLite is the working record store.

## Current observations and SQLite import

Adapters merge source receipts and current facts into `data/pending/observations.json`:

- `sources[scope]` records provider, state, actual timestamp, completeness and item IDs.
- `items[scope + U+001F + source_id]` records normalized facts, first/last seen, absences and source lifecycle.
- board observations carry company; board/feed observations carry normalized engagement, pay,
  rate, workplace and IR35; HN and tender observations carry explicit eligibility.
- award observations use `commercial_signal` under `award:<provider>` with a native
  provider/OCID/award/supplier identity. They go directly through observations, with no redundant
  discovery TSV. `pending/awards/last-run.json` records pages, notices and retained signals.

Award signals attach to the supplier company and project into `commercial_signals`, separately
from funding, jobs and tenders. They support a supplier-qualification question only. A named
delivery owner, current scope and external engagement route still need research. Expired or
withdrawn awards cease to support advice; absence from the moving window never closes a supplier.
Identical sightings advance timing without duplicating the award's evidence; changed facts survive.

**What is kept for each advert.** Most adverts on an employer board or feed are for unrelated
roles. A board/feed advert whose title does not match the title filter keeps its identity,
lifecycle, title, company, location, URL and stated terms (engagement, pay, IR35, workplace),
which are read from the text first. Its text, summary and derived qualification are dropped and it
is marked `compacted_at` with `compaction_reason: title-unmatched`, so imports skip it. A later
sighting with a matching title is ordinary evidence again. Matched titles keep everything, including
those later rejected for location, pay or domain.

Before the board scan, `wo ingest` writes `pending/protected-sources.json`: the board/feed source
identities, advert URLs and active job (organisation, title) keys that records hold
(`record_imports.protected_sources`). Adverts it names keep their full text whatever their title.
The scan trusts only this run's list: ingest removes it afterwards, and a standalone scan, or a
missing or unreadable list, keeps every advert whole. A record created during the scan is absent
from the list; its advert is skipped by that import rather than overwriting the record with empty
facts, and tomorrow's full sighting attaches. The ingest receipt reports `retention` (list state,
adverts stored without text, unmatched adverts kept because a record holds them), the store size
after import and each phase's process memory high-water mark.

Older advert text is compacted after import once the database holds that exact observation or no
record took it: seven days after closure, or thirty days unseen (`compact_observations`).

`wo.py import-scans` imports discoveries and observations into SQLite, updates source health and
structured views, and preserves human workflow state. Observations older than existing database
facts or health receipts are counted as stale and cannot rewind newer state.

Title intake, relevance and CV routing share comparison-only role equivalents in
`tools/job_rules.py`: QA/quality assurance, software-testing wording, seniority and discipline
variants of engineering leadership/founding roles, and ordinary punctuation. Original advert
titles remain unchanged. A matching title admits a candidate for qualification; it does not prove
suitability or vacancy availability. Negative occupations, junior/clearance/physical-work vetoes
remain; a pricing department does not veto an explicit engineering lead/manager, and a sales
department does not veto an explicit software-development-in-test role.

Rippling uses the public board's server-rendered listing data, with one bounded request per
configured employer. Only a validated, unfiltered and complete first page can establish absence.
Larger boards report partial coverage; malformed pages report failure. Neither closes previously
seen jobs. These listings do not provide the full advert, pay or publication date. Hyperlayer is
configured in `config/portals.yml`; a configured reader is not evidence of a scheduled live read.

Before creating a new automatic job (or its organisation), import rechecks explicit negative titles,
wrong work domain, disallowed location, excluded employers, source closure/expiry and known GBP pay
ceilings below the configured floor. Sparse evidence and unknown pay stay eligible for checking.
Existing jobs are matched first: later unsuitable source facts still update their evidence without
changing personal decisions. Legacy discoveries use the same check. Rejected sources remain in the
existing source store and may qualify on a later sighting; its normal retention rules still apply.
`intake_rejected` in import reports and ingest receipts counts reasons in that replay, not unique
new daily rejects. Manually saved jobs remain deliberate exceptions.

### Bounded automatic admission

After factual screening, `tools/discovery_intake.py` compares prospective jobs/tenders with existing
work using the shared relevance, intelligence and `work_selection` rules. `discovery_capacity` in
`config/relevance.yml` allows ten visible unchosen working items, a separate thirty-candidate qualification pool,
and sixty automatic record additions per rolling seven days. The existing model-call budgets are unchanged.
Recommendations take precedence over unresolved qualification. Explicit shortlists and active
processes are separate. Manual saves bypass automatic admission; unsaved candidates share attention.
Held records always receive source refreshes. Candidates outside the selection
remain in the pending store under its existing retention rules. No empty employer is created for a
deferred job. Company/reference signals continue through their existing pipeline.

Scoring runs outside the writer transaction. A changed SQLite `data_version` causes a fresh selection,
with at most three attempts. Identity and allowance are checked under the existing short transaction;
records and the bounded `meta.discovery_capacity_admissions_v1` counter commit or roll back together.
Admission uses actual import time, never a source's publication time. A malformed counter stops intake
without writes. Import reports and ingest receipts expose selected/admitted IDs, remaining allowance
and deferred reason counts. Repeated source items count again in that run's deferred totals.

Unchanged replay creates no duplicates. Existing history and personal edits are not deleted; only
expired seven-day counter entries are pruned. A failed transaction can be retried normally. If selection
repeatedly races with edits, retry the import after the current write finishes. Recovery to the previous
schema-compatible release preserves the counter and pending evidence, but removes its admission cap;
re-release the corrected limiter before resuming automatic intake if that cap must remain enforced.

## Reading, retries and the fetch log

Every request goes through `tools/web_read.py`: one user agent, a 25 s timeout and one retry policy.
Final responses have a 16 MiB limit and a 60-second body-read budget checked between socket reads.
Redirect bodies are closed without being read. HTTPS cannot redirect to HTTP; requests carrying
query parameters or private headers cannot redirect to another origin. Policy failures are
`bad-response`, are not retried, and require a reviewed endpoint update or investigation of the
response size. See the 9 October security audit.
A failure gets a kind that says what to do next:

| Kind | Cause | Retried? | What follows |
| --- | --- | --- | --- |
| `timeout`, `network`, `server` | no answer, connection error, HTTP 5xx | up to 2 more times (2 s, then 6 s) | the next run tries again |
| `rate-limited` | HTTP 429 | once, only if `Retry-After` is 30 s or less | the next run tries again |
| `challenged` | a bot challenge (`cf-mitigated: challenge`) | never | find another channel: API, feed or email |
| `refused` | HTTP 401/403 | never | check that public access is still offered |
| `not-found` | HTTP 404/410 | never | the source moved; fix the slug or URL |

Retries are bounded per process, so one bad host cannot use up a scheduled run: a host that fails 3
reads in a row gets no more retries, a process retries at most 30 times, and nothing is retried
after 300 s. The Adzuna and Reed feeds make one attempt per request (`single_attempt()`), because
each attempt uses up their quota. Nothing changes identity, user agent or network to get past a
refusal or a challenge.

The failure's kind, HTTP status and attempt count go into the source receipt's `detail`. The Sources
page turns it into a sentence (`records.FAILURE_TEXT`). Each request also appends one line to the
fetch log, `<data>/logs/fetch.jsonl`: scheme, host and path only, never the query or headers, which
can hold keys. It rotates at 2 MB, keeping one old file. `wo ingest` tags its lines with the run ID
and writes a summary to the receipt: requests, reads that recovered on retry, and failures by kind.
Read the log with `wo fetch-log [--failures]`, or `ssh ops@work-ops-vps wo fetch-log` for the VPS.

Each failure also becomes a task for the owner: a `failure` decision under "Waiting on you"
(`tools/failure_tasks.py`). A challenge, refusal, missing page or configuration error is raised by
the run that sees it. A timeout, network error, server error or anomalous empty read is raised only
after two runs in a row. A scanner that crashes without recording a source failure is raised too.
The ingest also raises a task when the nightly backup is overdue (over 30 hours), and the backup job
runs `wo watch`, which raises one when the daily ingest is overdue. An open task is withdrawn when
its source or job works again. The receipt records what was raised and what was cleared.

`wo fetch <url>` is the same reader for attended use, when an agent's own browsing tool is blocked.
It checks robots.txt for pages, then reads directly from this host. A page that comes back as a
script-rendered shell is read in headless Chromium (`tools/browser_read.mjs`, Mac only). From the
Mac, a source this host cannot reach (timeout, network or server error) is tried again on the VPS.
A challenge, a refusal or a robots.txt disallow ends the chain: exit code 2 plus what to try instead.
`--as json|raw`, `--browser` and `--via local|vps` override the defaults. Scheduled ingest never
uses the browser: it reads documented APIs ([adding-a-source.md](adding-a-source.md), step 2).
Covered API endpoints use the budgeted HTTP reader. From the authorized Mac, explicitly select
the existing VPS route (`wo fetch --via vps --as json <url>` or `ops/wo-live fetch --as json <url>`).
Local API dispatch and browser API subrequests are blocked; a budget wait never triggers another
host or browser fallback. The restricted VPS agent key remains unable to proxy network requests
as the service user; its ordinary webpage reads remain unprivileged.

## Run states and closure

`ok` and `empty` are complete reads. `partial` means results remained unread. HTTP, configuration,
network and parsing failures remain visible per source. A previously populated source that suddenly
returns no items becomes `anomalous-empty`, not closure evidence.

Presence refreshes current facts and clears prior absences. Failed, partial, inaccessible and
anomalously empty reads add no absence. A source-scoped job needs explicit closure or two complete
absences at least 24 hours apart. Neither age nor an advertised closing date closes it.

A listing is also availability evidence. While the latest actual read of any board or feed listed an
advert less than 48 hours ago, it counts as open (`evidence_checks.advert_availability`) and can
appear in **Worth a look**. Research reads cannot replace this for Adzuna, which refuses page reads,
or LinkedIn, which forbids them, so a blocked read is never counted against a listing. For adverts on
Greenhouse, Lever or Ashby, research confirms the posting through the same public APIs this scanner
uses (`scan_boards.posting_api_url`), because their pages are built in the browser. A posting missing
from that API stays unconfirmed; absence alone still closes nothing.

An advert no scanner has listed for more than 14 days (`freshness.unseen_days` in
`config/relevance.yml`) is ranked down with the reason "No source has listed it since <date>", so
The owner does not spend time on it. This is a ranking signal, not closure: a feed shows only its newest
results, and a board that moved is no longer read. Nothing is stored and no request is made; the
last sighting is the scanner observation's time, and an advert seen again recovers. The ingest
receipt counts these adverts under `stale` (archive/plans/2026-10-04-stale-adverts.md).

Tender reads use moving date windows, so their absence is never authoritative. Only an explicit
provider status such as withdrawn, cancelled or complete closes tender source lifecycle.

## Interpretation rules

- Missing pay and IR35 stay empty; personal floors are not source evidence.
- Aggregator figures without a stated unit keep an empty `pay_period`.
- A day rate spans every day rate the advert states in one currency (PAYE and umbrella, for
  example), and the floor is compared with the highest, so a PAYE figure given first cannot hide a
  contract whose contractor rate clears it.
- SmartRecruiters list results contain no advert body, so text-derived fields may be unknown.
- Provider `employment_type` and `department` remain raw alongside normalized fields.
- `search_queries` is a manual research aid, not runtime coverage.
- Coverage reports distinguish unread channels from measured zero yield.
