# Architecture

[Documentation index](README.md) · [Repository home](../README.md)

How ingestion, source evidence, SQLite records and the hosted workspace fit together. Scanner
contracts are in [scanners.md](scanners.md); adapter work is in
[adding-a-source.md](adding-a-source.md). The operating runbook for new data (scans, LinkedIn
exports, research batches, quality and repair) is [data-pipeline.md](data-pipeline.md).

For the complete deployed topology, schedules and recovery boundaries, use the
[system map and PNG diagrams](system-map.md). This page owns code boundaries and invariants.

## System shape

Opportunity sources have different identities and completeness rules. Employer ATS boards publish
one company's vacancies, aggregators index agency adverts, procurement APIs publish releases in a
buying process, and Hacker News publishes comments under a monthly thread. The common path is:

```text
scheduled or manual read shared rules/evidence          canonical records and views

scan_boards.py ──┐
scan_tenders.py ─┼──> PendingStore discovery TSV ──┐
scan_hn.py ──────┤                                  ├──> wo.py import-scans ──> SQLite
scan_news.py ────┘
       │                                             │                         │
       ├── job_rules.py                              └── observations.json ────┤
       ├── web_read.py                                                         │
       └── ObservationStore ── source health/current facts/lifecycle ──────────┘
                                                                                 │
                                                        workspace API + structured views
```

`config/portals.yml` owns source definitions, `config/collection.yml` owns request policy, and `config/channels.yml` describes coverage. Discovery adapters are run by `wo ingest`, manually or on the VPS daily schedule with `--keyless` plus the keyed sources the owner approved
by name (`wo-scan.timer`, about 05:30 UTC; decisions #18, #29 and #39). The Markdown hub import
and `records/hub/` were retired on 27 September (git tag `pre-cleanup-2026-09`).

Email uses a separate `wo-email.timer` every 15 minutes: Cloudflare D1 → private VPS spool →
`scan_alerts.py` → the same canonical importer. Run receipts use `email:` and `email-import:`
prefixes, separate from the general daily scan. The [email runbook](linkedin-alerts.md) owns
transport, acknowledgment and recovery details.

## Modules

| Module | Responsibility |
| --- | --- |
| `tools/alert_jackandjill.py` | Jack and Jill digest extraction only; generated prose stays outside qualification text |
| `tools/email_leads.py` | On-demand bounded pending-email read/detail and explicit shortlist orchestration; calls the existing record writer and read-only original-mail reader |
| `workspace/email_sources.py` / `config/email-sources.json` | Shared exact sender/source identity and labels; Worker imports the same registry |
| `tools/discovery_intake.py` | Pre-write shared ranking, rolling automatic admission counter and concurrent-edit retry; no new selection policy |
| `tools/record_imports.py` | Scan and research identity, chronology and import reports; shared by CLI and ingest |
| `workspace/records.py` | The record service's one entry point: re-exports the modules below as `records.<name>` |
| `workspace/record_read.py` | Shared deferred read snapshots and exclusion policy; compatibility exports remain in `record_views` |
| `workspace/decision_views.py` | Decision choices, read projections and batched history, independent of decision commands |
| `workspace/incoming_views.py` | Incoming-process evidence and document metadata projection, independent of intake commands |
| `workspace/record_core.py` | Write boundary: transactions, revision checks, validation, state events, edit ownership, observation storage |
| `workspace/record_identity.py` | Canonical source upserts, organisation identity and organisation links; import and manual paths share these implementations |
| `workspace/record_mutations.py` | Create, edit, transitions, working state, interactions, links, archive, retype, company numbers and saved jobs; delegates organisation identity to `record_identity` |
| `workspace/record_evidence.py` | Reviewed research findings and merges that keep every observation |
| `workspace/record_decisions.py` | The owner's decisions, card questions and the draft versions approvals bind to |
| `workspace/record_review.py` | LinkedIn and list review queue: cards, offered choices, accept/merge/reject |
| `workspace/source_coverage.py` | Source and channel health in words, and the freshness line |
| `workspace/work_selection.py` | Pure shared work admission/order, lifecycle groups and bounded research IDs; no I/O |
| `workspace/approach_history.py` | Pure historical status rules and snapshot-local decision/outcome indexes; shared latest-draft selection and compatibility constants for progress |
| `workspace/machines.py` | Derived approach progress, card actions, weekly attention and decision status. Stored field graphs stay in `states.py`. [State machines](state-machines.md) |
| `workspace/workspace_state.py` | Coherent state projections, explicit internal context without the receipt archive, single-build caches and versioned summary updates |
| `workspace/request_timings.py` | Bounded in-memory save, refresh and build timings; no I/O, paths or content |
| `workspace/dashboard_views.py` | `/api/dashboard`: capacity, quotas, 30-day hunt counts and responsiveness from bounded receipt/ledger reads; never builds state, takes the build lock or writes |
| `workspace/allowances.py` | Read side of the Codex invocation limits/summary and the automatic-admission ledger, moved from `automation_usage`/`discovery_intake` (which re-export them) |
| `workspace/js/site-status.js` / `dashboard.js` | Site problems and the one-line status (one owner, reusing each run's own outcome rule); the Dashboard tab composed from the summary, `/api/status` and `/api/dashboard` |
| `workspace/summary_transport.py` / `workspace/js/summary-transport.js` | Lossless response-local string sharing and browser restoration; no domain rules or I/O |
| `workspace/state_export.py` | Incremental complete-history JSON and private full-response file/gzip cache; transport consumes independent readers after the database snapshot closes |
| `workspace/record_outcomes.py` | Reported outcomes, corrections and handover into an opportunity |
| `workspace/record_week.py` | Historical weekly selections and restore compatibility; new production writes are retired |
| `workspace/record_export.py` | The flat CSV export |
| `workspace/record_views.py` | Batched record enrichment, coherent snapshots, per-record timelines and job briefing fields retained independently of source checks |
| `workspace/js/navigation.js` | Page, record, decision and action selection; routes, detail back-stack and unsaved-change checks, with injected browser/view effects |
| `workspace/js/people-context.js` | Pure unique-person action queue and inverse role routes from the shared indexed contact resolver; no writes or admission into This week/research |
| `workspace/js/record-detail.js` / `decisions.js` | Record sheets/inline controls and decision cards/answer forms; explicit collaborators supplied by `app.js` |
| `workspace/js/record-timeline.js` / `record-presentation.js` | Lazy timeline request reuse/invalidation and shared record formatting; neither owns application navigation |
| `workspace/js/contact-log.js` | Optional contact evidence form; persistence stays in progress/record services |
| `workspace/js/` | Other DOM/link, API, form and page helpers, composed by the `app.js` ES module |
| `web_read.py` | Bounded HTTP JSON reads and HTML-to-text conversion |
| `job_rules.py` | Candidate rules and normalized source-stated terms; the one whole-word matcher (`word`, which `relevance` re-exports) and memoised `title_equivalent` |
| `pending_store.py` | Append-once discovery archive plus source-scoped observations; `observation_reader.py` decodes the existing JSON in bounded chunks |
| `scan_boards.py` | Greenhouse, Lever, Ashby, SmartRecruiters, and credentialled feeds |
| `scan_tenders.py` | Contracts Finder and Find a Tender OCDS releases |
| `scan_awards.py` | Bounded public award reads; supplier commercial evidence, distinct from funding and open tenders |
| `tools/research_worker.py` / `research_sources.py` / `research_model.py` | Bounded server research, public source reads, isolated Codex CLI calls, validated canonical findings and run coverage; activation evidence |
| `tools/approach_preparation.py` / `workspace/js/preparation-status.js` | Historical validators, reports and compatibility code; generation/import entry points and live preparation UI are retired. Dated evidence |
| `tools/email_worker.py` / `scan_alerts.py` | Buffered mail collection, durable receipt, job-alert parsing and canonical import; run-status projection in `record_views.email_status` |
| `scan_hn.py` | Direct hiring comments in one monthly HN thread |
| `scan_news.py` | Funding stories from RSS news feeds; each becomes a dated signal on the organisation it names, never a record of its own |
| `scan_companies_house.py` | Registry facts (status, current officers, share allotments) for companies to approach whose number was recorded on evidence; never matches a name, never opens the database |
| `credentials.py` | A secret by name: the environment on the Mac, the service's systemd credential directory on the VPS |
| `tools/wo.py` | CLI argument parsing, migration/import/export commands, backup and source health; compatibility exports delegate to the canonical services |
| `workspace/normalise.py` | The one set of normalisation rules every import and repair applies (entities, whitespace, factual dates, name keys) |
| `workspace/profile_rules.py` | The one reader of the profile's contact rules (`config/profile.yml`): the former-employer flag, warning and leaver wording, and private note topics. Code names neither; tests install synthetic rules with `override` |
| `workspace/relevance.py` | Derived, explained ranking of every record from facts and `config/relevance.yml`; computed on read and never stored |
| `workspace/decision_intelligence.py` | Outreach actions and problem briefs from `config/problem_playbooks.yml`; separate fit, access, evidence and readiness |
| `quality.py` | Defect and gap checks (`wo quality`) and deterministic repairs through the record service (`wo repair`) |
| `coverage_report.py` | Measured discovery-channel yield from current config and scanner output |

SQLite is the working record store. It separates human workflow fields from observations,
interactions, links, review items, source health and import receipts. Scanner imports refresh source
facts but do not force a person's decision or commercial stage.

## Shared collection boundary

`workspace/collection_policy.py` validates the repo-owned policy and identifies covered API
endpoints. `workspace/collection_reads.py` supplies read-only indexed usage projections to the
Dashboard and CLI. `tools/collection_budget.py` is the only reservation writer, called by the shared
HTTP transport before each attempt/redirect. These modules never import the record writer.

The versioned `data/collection.db` ledger is separate from job records. A short `BEGIN IMMEDIATE`
reserves allowance and advances an attempted task, commits, then dispatches. A per-provider file
lock coordinates pacing; waits and network I/O hold no SQLite transaction. Counts use an indexed
London-day range; task selection reads only configured keys. Crash/uncertain reservations remain
spent. Production dispatch has one fixed VPS authority; an environment variable cannot select a
fresh Mac ledger. Email HTTP and Codex invocations retain their existing accounting and schedules.

`tools/collection_schedule.py` derives one coherent ingest context from canonical selection,
admission counters, chosen-work links and latest source observations. Subprocess scanners receive
that context and never open the live record database. Rotation cursors store identities/dates only.
Skipped work is a deferral receipt, not an observation. Exact retained board evidence can satisfy
research within 24 hours using its original timestamp; the 48-hour vacancy rule remains unchanged.
Partial results, explicit expiry, conservative absence, identity, human edits and revision checks
continue through the existing importer. No second full-response cache or folder restructure is needed.

`tools/backup_collection.py` embeds a consistent ledger snapshot in the disposable encrypted backup,
using the existing offsite format and permissions. Restore verification compares all retained rows;
production recovery adds a next-London-day hold. See [configuration](configuration.md#collection-policy)
and [operational recovery](../ops/OPERATIONS.md#collection-accounting-and-recovery).

## Identity and lifecycle

| Channel | Scope | Native identity |
| --- | --- | --- |
| ATS | `board:<provider>:<slug>` | provider job ID; URL only as fallback |
| Aggregator | `feed:<provider>:<configured-name>` | Adzuna ID or Reed job ID |
| Tender | `tender:<source>` | OCDS release/notice ID; OCID remains process grouping |
| Award supplier | `award:<source>` | Provider + OCID + award ID + supplier ID (normalized supplier name only as fallback) |
| HN | `hn:who-is-hiring:<thread-id>` | comment `objectID` |
| Registry | `companies-house` | company number, mapped to one record by `wo company-number` |

The dated TSVs are append-once discovery archives. `data/pending/observations.json` carries current
normalized facts, source receipts and source lifecycle. Presence refreshes facts and clears absence
observations. Failed, partial, inaccessible and anomalously empty reads cannot advance absence.
An empty presence-only window is a normal empty read; only a sudden empty authoritative inventory
is anomalous. Existing malformed pending evidence fails closed instead of becoming an empty store.

A source-scoped job can close from an explicit provider closure or from two complete absence
observations at least 24 hours apart. Elapsed time or an advertised closing date alone never closes
a record. Tender scans use moving date windows, so absence is not authoritative; only explicit
provider status closes a tender source lifecycle. The importer compares observation times so an old
archive or delayed receipt cannot rewind newer SQLite facts or source health.

A title/company match from a new source attaches only when its stated location and engagement
are compatible. Missing facts remain neutral; disagreements and ambiguous matches stay separate
with a `possible_match` link. Existing attachments are audited, not automatically split.
Organisation names merged into another record follow their survivor. A plain archive remains an
archive and is reported by the importer.

Research uses the canonical service and shared normalizer. Raw observations are retained, but
only strictly newer reviewed facts can change unprotected fields. Missing or blank text cannot
clear a fact. New merges copy observations to the survivor through the normal deduplication
boundary and keep the originals on the archive; the quality audit finds omissions in older merges.

## Derived views and data quality

Relevance ranks each record Strong, Possible or Lower with signed reasons. Deterministic
`job_rules` extraction distinguishes explicit essential, desirable, waived and ambiguous requirements,
retaining negation and alternatives. `relevance` compares those clauses with the sourced proof owner
in `config/problem_playbooks.yml` during the coherent read. Quoted research is ordinary attributed
evidence; a title or technology mention cannot create a mandatory requirement. Missing proof means
not established, never an assertion that the owner lacks the experience. Mixed subjects, unresolved
conjunctions and negation cannot manufacture positive fit. Advert projection retains one character
beyond the 16,000-character parsing limit so truncated requirements force explicit uncertainty.

Supported remit outweighs the combined positive title, contact, pay/location and freshness factors.
Repeated evidence adds no points. Unresolved essentials cap the band at Possible; engagement carries
no score bonus. Scores and comparison are never stored or used to move a record. Weights reload from
`config/relevance.yml`. The qualification digest cache now bounds extracted clauses to 8 MiB of
serialized values / 8,192 entries under a lock. A thread-protected pure comparison cache holds at most
4,096 serialized results / 8 MiB, keyed by source content, provenance and proof configuration. It returns fresh objects so summary trimming cannot contaminate later reads.
Historical drafts do not drive admission or research selection.
Relationship scoring groups interactions during its existing latest-contact pass, then considers
only each person's evidence. Those temporary buckets are released before qualification; no extra
cross-request cache or freshness rule is introduced.

`wo quality` separates defects (wrong data with a known fix) from gaps (facts not yet known).
`wo repair` applies the deterministic fixes through the record service as `agent`. Both are
idempotent, and `wo quality --strict` gates the runbook. `record_edits.actor` keeps the owner's edits
distinguishable from agent corrections: imports never overwrite either, and sourced research may
fill a field an agent left empty.

Current decision-maker and known-contact recommendations require current `works_at` or
`consultant_at` evidence; named senior employees require `works_at`. Historical, introducer and
related links remain visible without asserting employment, and `do_not_contact` is respected.

Record enrichment uses a fixed number of SQL queries rather than queries per record. One state
build reads the exclusion policy once and passes it to records and both decision projections. A deferred
read transaction keeps records, revisions and evidence coherent while WAL writers proceed.
CSV export reads flat fields and batched links in the same snapshot, without loading unused
evidence. Decision history batches related records and version bindings. Observation ordering uses
UTC instants; original timestamps remain available. The organisation-name gap check indexes
normalized whole-word prefixes instead of comparing every pair of firms.
The browser requests `/api/state?view=summary&encoding=strings-v1`, with 12 recent human events and bounded operational
status projections; it omits `coverage.runs` entirely. Raw advert bodies and per-record outcome actions are omitted after qualification, scoring
and the progress view have used them. A full timeline is fetched
when the record opens. JSON and static responses negotiate gzip. All interaction evidence contributes
to scoring first. `/api/state` and CLI state remain full. A delayed timeline response replaces
only its own section and never rebuilds an open form. State builds are serialized; an invalidated
cache is released before replacement, and the summary cache retains encoded JSON bytes rather
than another full state object. This bounds concurrent rebuild memory after writes.

Research, email and caretaker database connections wait up to 120 seconds for the SQLite writer
lock (`db.WORKER_LOCK_WAIT`); interactive connections keep 15 seconds. Each `db.Connection` carries
its own wait, so a long-lived connection keeps it however many others open after it. This wait is distinct from
the shared file writer lock. Failure notes and recovery rules are in
[the caretaker runbook](vps-caretaker.md#when-a-job-fails).

Historical drafts have immutable versions and approvals bound to reviewed wording; public
draft/approval writes are retired. Other decision processing requires the current answer revision, with explicit claim/block state
for consequential execution. Optional outcomes record only reported events, preserve the exact
advice snapshot and person/company context, and never send a message or advance commercial state.
The browser exposes decision history and aggregate outcome counts without inferred conversion odds.

## Weekly attention view

Deferred email review is separate from selection and the ordinary state/status response. The
existing email-processing card calls `/api/email-leads` on disclosure or submitted search, showing
at most 20 newest eligible, unsaved adverts. The streaming observation reader visits one item at
a time; a shared read lock serializes these reads without holding a SQLite writer lock. No second
inbox store, migration, scheduled work, ranking rule or model call is introduced. Full-file I/O
still scales with pending history; the response and retained result set are bounded. Repeated
observations of the same canonical advert URL are deduplicated; uncertain cross-provider matches
are not merged. Original email reads use the indexed receipt hash and verify the MIME bytes.

`/#dashboard/lead/<sha256(source-scope + U+001F + native-id)>` is stable before and after saving.
Detail shows email claims separately, with an original Gmail Message-ID search when available and
a download served only as an attachment. The explicit same-origin POST to
`/api/email-leads/<id>/shortlist` parses the source before its canonical transaction, rechecks exact
record identity, reuses save/transition and atomically attaches its source observation. Concurrent
clicks create one role; existing applications, human facts and automatic allowance remain intact.
Reads never imply research, shortlisting or application. The saved record can resolve its original
Review link on demand without adding fields to every state-summary item. Rollback is schema-compatible
and preserves pending mail, original receipts and all saved records.

`work_selection.build(state)` owns admission, weekly sections and ordering after canonical relevance
and intelligence are attached. The retained Jobs groups are `discovery`, `shortlisted`, `active`,
`closed` and `other`. References carry record/action identity, lane, the placement `rule`, the actual
score/admission reason, and optional dated source and decisive uncertainty. Discovery additionally
carries `worth_a_look`: leads qualify; adverts require open availability from `evidence_checks` /
`record_views`. This week shows three Best opportunities, expanding to ten; Jobs keeps the whole group.

### Placement rules

Each job, tender, opportunity, company and agency is placed by the first matching rule in
`work_selection.RULES`, in this order. The code on every reference names that rule, and the rule's
text is the reason shown where no score or recorded step supplies one.

| Order | Rule | Group | When |
| --- | --- | --- | --- |
| 1 | `closed.recorded` | Closed | Recorded closed, skipped, lost or won, or a terminal outcome |
| 2 | `active.process` | Active | Applied/interview/offer, an opportunity in contact, conversation or proposal, or a saved work commitment; a closed advert requires a reply, ongoing conversation/interview/offer or explicit commitment |
| 3 | `shortlisted.job` | Shortlisted | A job you shortlisted whose advert is not confirmed closed |
| 4–6 | `closed.source`, `closed.check`, `closed.outcome` | Closed | Source reports it gone; current evidence or a passed deadline confirms applications closed; a recorded outcome ends the approach |
| 7–10 | `other.excluded`, `other.unsuitable`, `other.duplicate`, `other.existing_work` | Other | Excluded; outside software and delivery work; a duplicate; a copy of active, shortlisted or closed work |
| 11 | `discovery.advert` | Discovery | A strong, current advert without conflicting terms |
| 12 | `other.advert` | Other | Any other advert that is not promising |
| 13 | `other.unchecked_advert` | Other | A promising advert research does not read: a feed listing, or a board advert no longer listed. Shortlisting makes it a research target |
| 14 | `qualify.advert` | Other | A promising advert research can read: a LinkedIn alert or one with an essential gap, rated possible or strong |
| 15 | `discovery.lead` | Discovery | A company, agency or recorded opportunity with a current, specific sourced remit and no essential gap |
| 16 | `qualify.lead` | Other | A direct lead with an essential gap, or a recorded opportunity rated possible or strong without a current remit |
| 17 | `other.lead` | Other | Any other recorded opportunity |
| 18 | `other.capacity` | Other | Outside the shared ten-item candidate working set; retained without changing state |
| 19 | `other.capacity_stale` | Other | Unresolved unchosen qualification older than fourteen days without current open-source evidence; retained |
| 20 | `other.context` | Other | A company without a current, specific remit |

A recorded opportunity is not a recommendation on its type alone (changed 29 October): it needs the
same current, specific remit as a company. Active and shortlisted references also carry an
`attention` cause from `machines.ATTENTION` (`reply`, `your_step`, `their_step`, `incoming`, `stage`,
`shortlisted`, `submitted`, `unassigned`) naming why they sit in Act now, Waiting or In progress.
`wo approach` (and `ops/wo-live approach`) prints This week's sections in page order
(`work_selection.WEEK_PAGE`/`week_page`, the order `js/week.js` renders), each item's rule and cause,
then the discovery items the page leaves to Jobs with their advert availability, and the research
targets. Tests pin every rule's group and section and the page's section order.

Direct-work evidence must establish a sourced request or owned delivery remit. Funding, product
descriptions, service marketing, speculative needs and hiring-only evidence do not establish one.
A known vacancy's source remains advert context even when the vacancy closes or is skipped; it
cannot become a second company recommendation. Human process and terminal-decision history retain
precedence when an evidence change withholds a new recommendation.

Confirmed advert closure overrides a shortlist or an unanswered application, including its elapsed-time
follow-up suggestion. Those records remain in Jobs → History and reference → Closed / skipped and
search, with their application status and history unchanged; they leave weekly attention and routine
research. Recorded replies, interviews, offers and explicit mine/waiting commitments retain precedence
over public-advert closure. A merely absent listing or unverified read does not establish closure.

Discovery sorts by suitability score, then qualifying evidence date, engagement preference and
stable identity. Below-floor stated GBP pay caps an advert at Possible; it cannot lead Best opportunities.
Unknown pay and other currencies remain unassessed. The evidence date is a lead's remit date, or an advert's publication or
dated research. A stated publication date takes precedence; otherwise an alert uses its original
email date before research dates. Brief facts carrying a daily listing/read time cannot refresh
the advert's age. Dated evidence precedes undated. A strong permanent role can precede a weaker contract.
Confirmed URLs, scoped source/requisition identities and explicit identity links group copies;
similar agency wording only creates a possible-match note. Active, shortlisted, skipped and excluded
confirmed work cannot reappear as a new recommendation. All histories and actual processes remain available.

All unchosen candidates share the ten places, including manual imports and previously edited
records. Preservation never implies unlimited attention. Explicit shortlisted and active records
stay separate by their existing placement rules; no source-origin exception remains. Jobs folds
Reference and Closed / skipped under History and reference, without backlog counts. Global search
retains every record. Needs checking is folded; optional Shortlist/Skip lets the owner act without
making it a required review queue.

Dashboard section links use `#week/section/<group>`; navigation retains the target through the
hashchange render, reload and browser Back. This week opens and focuses that section, including
an explicit empty state when its records have changed. Closing a record opened there returns to
that group. Dashboard outstanding replies use `peopleContext`'s existing unique-person `needs_reply`
selection and action identities; historical outcome counts do not create reply tasks. The intake
comparison is a collapsed native disclosure whose live form/results survive Dashboard refreshes.
These are browser presentation changes over the loaded summary, with no extra reads or writes.

The same owner supplies `act_now`, `waiting`, `in_progress` and six `needs_checking` references.
Existing recorded mine/waiting steps decide responsibility; incoming processes with free-text-only
steps stay In progress. `approaches` continues deriving progress and permitted controls; its former
unused attention projection is retired. Personal contact events never become company work.
Research consumes shared `research_ids`: active/shortlisted work plus ten discovery-side places,
six of them reserved for the `qualify.*` pool, with oldest-attempt rotation (never attempted first).
Feed-only discovery adverts wait until shortlisted because daily listing already determines
availability; no research allowance or schedule changes. Needs checking shows up to six of research's
next qualification targets, always within `research_ids`, showing the actual last attempt rather
than an invented queued status. A candidate research will not read is never shown there.
`js/research-status.js` owns presentation of recorded attempt outcomes, controlled source reasons,
London timestamps and regular-pass wording for cards, role detail and Dashboard. It uses the record's own
attempt, never a newer global run to infer its cause. Ambiguous pauses remain ambiguous; raw failure
types and locations stay in operator details. Latest-pass counters preserve missing versus measured
zero, and allowance counts retain their snapshot date. These are loaded projections: opening a view
does not start research, add a per-item read or alter the existing refresh cadence.
The worker alternates chosen active/shortlisted work with discovery qualification, starting with
chosen work. `research_scope.decision_plan` gives public application/qualification questions priority
within each lane, then process evidence, then routine maintenance. Fewer unresolved public questions
come first; equal priorities rotate by oldest substantive attempt, then shared projection order.
Allowance/access stops do not count as completed research. Known personal experience gaps without a
public blocker are shown as choices and excluded from model work; ambiguous source requirements remain
researchable. The summary carries at most six plan entries, and Dashboard shows at most three meaningful
questions with consequences. The worker passes the same question to the existing evidence packet. A six-call pass therefore
starts three targets from each lane when each needs one call, or chosen/qualification/chosen when
each needs two. Candidates beyond those targets remain outside the current focus,
not ineligible. Literal remit/requirement quotations from the same confirmed exact vacancy do not
need to repeat its title; person attribution and the vacancy confirmation boundary stay unchanged.

`js/opportunity-card.js` owns rich identity, practical terms, source, rationale and uncertainty, shared
with active progress headers. Evidence/requirements expand on demand from the loaded summary.
The existing remit paragraph can show a source-linked duty quote; `relevance.fit.evidence.kind`
distinguishes a remit from a requirement without altering either quote. The original summary stays
in the full assessment. `record-presentation.engagementLabel` presents the server-qualified terms
consistently, including explicit conflicts and unknowns, without changing stored facts.
Act now job cards and the leading job-detail assessment share pure `cardRemit` and
`jobDecisionFacts` presentation in `record-presentation`. The card combines existing terms and
replaces only a default step with fit evidence; the detail adds the explicit missing-fact checklist.
`approaches.progress.next_is_default` identifies default wording independently of talking points;
recorded steps, replies and incoming-process instructions clear that flag even if their wording
matches a default. Availability policy stays server-owned; `job-evidence` presents/ages its answer
and refreshes the detail's missing-availability line with the same clock. No new per-card read or
write is involved.
`js/job-actions.js` supplies revision-checked Shortlist/Skip and existing Undo recovery. Jobs still
renders ten rows initially, increasing by ten. There are no per-card API or SQL reads; outgoing
wording and complete timelines remain in full exports and on-demand history. Compact summaries
omit raw adverts, duplicate extracted requirements, unused optional-unknown strings and the score
factor's duplicate evidence object; `fit.requirements` and `fit.evidence` retain the sourced disclosure.

`record_views.company_logos` batches the newest valid logo-bearing observation for each unambiguous
canonical company, including merge aliases. One company-keyed map and compact record references
share newer evidence with older jobs. Agency/client name ambiguity gets initials. `source_images`
owns URL validation; parser capture requires the exact job anchor and permitted LinkedIn company-logo
path. There is no server image fetch, cache, attachment resolution, historical replay or migration.
The shared browser Intersection Observer assigns image sources only in view, with no referrer,
asynchronous decoding, JavaScript fallback and reserved 40px mobile / 64px desktop dimensions.
CSP allows images from self and `https://media.licdn.com` only. Other source images cannot expand it.

Keep parsing, network/model work and validation outside write transactions. Preserve WAL, revision
checks, serialized rebuilds, byte-only summary caching and existing lock waits. Benchmark
100/1,000/3,000 candidates with independently growing history, fixed query counts, zero read writes,
payload/RSS and writer timings. Logo/source projections return bounded rows but currently scan/sort
retained observations: fixed query count does not imply history-independent cost. Measurements and
remaining usability/hosted gaps belong to the delivery record.

### Recorded approach next steps

Migration 019 adds `approach_next_steps`, one current value and revision per `(record_id, action_id)`.
`record_outcomes.next_step` is its standalone writer; contact recording uses the same writer in its
atomic transaction. `state_events` retains changes. Outcome snapshots bind only their own change,
so correction restores a previous step only if no subsequent edit would be overwritten. Omission
preserves it; completion/clearing are explicit. These edits do not invent contact or advance state.

`workspace_state` reads current steps in one batch. `approaches` projects their current value, with
incoming processes and explicit terminal states retaining authority. Stored context preserves a
commitment if recommendation changes. Merges with step history remain refused. One underlying
record owns each step; This week does not copy commitments from Jobs or People.

`js/next-step.js` owns the common form. `js/contact-log.js` records factual contact with optional
context and a step, without a draft or message payload. Existing backups include migration 019.
After its writes, recover with compatible code or a data-preserving forward repair: never roll back
to a writer lacking step-aware correction.

`js/sending.js` owns the ephemeral People sending dialog: at most ten selected people and one
rendered editor, twenty search results, per-person wording retained only while open. Its pure
`sending-model.js` indexes eligible projected contact actions once per selection pass and uses
`template-text` and `week-brief.contactRoute` for wording and routes. `/api/message-templates`
reads the existing `config/templates.yml` on demand without a database read or summary expansion.
The dialog calls the canonical outcome/correction endpoints with captured revisions, omits
`sent_message` and `next_step`, and preserves edits across failures and explicit conflict refresh.
The server's duplicate protection remains intact; a brief post-save UI guard also prevents a second
tap from recording the next recipient. Instagram maps explicitly to Other with its name in the note.
No migration, persistent draft store, mailbox access or provider sending is introduced.

### Weekly component APIs

The UI retains DOM factories and explicit collaborators. `app.js` provides state, navigation,
modal and mutation boundaries. `progress.js` supplies shared permitted controls and Undo; page
composers render server references. The API uses actual projected action IDs and current revisions.
`job-evidence.createContactRouteLookup` owns one contact adjacency index for the current summary,
shared by Jobs, search, People and record detail. Replacing records or links invalidates it;
status-only updates reuse it. Mutable standalone callers retain the uncached builder. `outreach.createOutreachViews` indexes
action/research membership once per snapshot and checks current exclusions at lookup time. Both
retain original ordering and duplicate semantics, and hold only their current indexes. Their memory
grows with records, links and actions; they do not make the summary payload independent of data size.

`job-evidence.confirmedOpenAgencyRole` gates agency recommendations on current research-confirmed
availability; a fresh listing alone is insufficient. The existing evidence clock refreshes only
the read-only agency role sections when eligibility expires, preserving surrounding unsaved forms.
`week-brief.contactRoute` validates and selects recorded contact channels for the shared **Contact**
links. Opening a channel is read-only; only the existing contact log records an event. General
connections and contact-route checks do not establish responsibility for a specific hire.

Every logical full summary ends with a `version`. The browser opts into `encoding=strings-v1`:
`summary_transport.py` shares repeated strings of at least 12 characters in a response-local table.
The `{encoding, strings, value}` envelope uses `~N` string references; literal leading `~` is escaped
as `~~`. Keys and table entries remain literal. `js/summary-transport.js` restores every original
value before rendering or editing, including quoted evidence, signed URLs, nulls and absent keys.
No evidence is truncated or fetched separately. The plain summary route remains unchanged.
Each full form (ordinary JSON and `strings-v1`) gets its raw/gzip byte pair on first request, under
the existing lock; a save is answered with a delta, so most builds make neither. Both share the
build's version and invalidation and are discarded with it. The codec packs the tree it has just
parsed in place (`encode(..., owned=True)`); there is no retained decoded dict or database work in it. This small separate module keeps transport out of domain rules;
measured savings and its CPU/memory cost are in the current delivery record.

After a save, `app.js` `load()` asks
`/api/state?view=summary&since=<version>&encoding=strings-v1` and `js/summary-delta.js` applies the reply: changed and
removed records and work items (by id), any new order, and changed or removed other sections.
The server keeps the current build's JSON fragments and only hashes for the last eight versions,
never a summary dict. An unknown, evicted or unpatchable base (duplicate ids, missing lists, a
restart) returns the encoded full summary. Deltas themselves are unchanged. A client-side mismatch
reloads in full; an unsupported encoding route or invalid encoding retries ordinary JSON, allowing
code rollback without a data change. Sign-in errors continue through the existing sign-in recovery.
The server still rebuilds the whole projection after each change, because ranking is derived
across all records; only transfer and parsing shrink.

An open page refreshes run statuses through `/api/status` and the versioned summary when the owner
returns, and every five minutes while visible. A shared cooldown prevents duplicate return requests.
`js/summary-refresh.js` rejects replies based on an outdated base, defers changed data while a sheet
or editor is open, and preserves expansion, focus and scroll for background redraws. A pending-update
notice leaves a form's captured revision intact; saves retain the existing stale-edit conflict recovery.
Status-only refreshes still update the compact operational blocks without replacing a form: the
one-line site status on This week and Jobs, and the Dashboard. `/api/dashboard` is read only while the
Dashboard tab is open (on opening, on return and every five visible minutes), one request at a time and
abandoned when the owner leaves the tab; the one-liners use only the loaded summary and `/api/status`.
A failed read is shown with Retry and the time of the last good figures; it waits for the next
cadence rather than retrying every minute. Summary requests carry `X-WO-Refresh: save|background|manual`
so `request_timings` keeps the refresh after a save apart from polls. Its 30-day counts use
`outcomes_at` (migration 020) and the `state_events_recorded` range as the outer loop. Feed totals
read every receipt in the period rather than a row cap. Caretaker checks carry their incident
`subject` (`record_views.caretaker_subject`, the rule the caretaker files cards under), so a check
and its card count once.
Research item failures record their type and code location (`db.failure_location`), never the message.

Dashboard's `decisionsForYou` counts the same answerable, non-excluded open decisions it renders,
including operations. Other faults stay under Repairs and checks. The intake comparison is a separate,
explicit GET `/api/intake-review`, never part of automatic Dashboard polling. `tools/intake_review.py`
streams pending observations, discards saved identities/URLs before retaining at most 2,000 candidates,
then calls the same candidate construction and `discovery_intake.prepare` used by imports. Its optional
comparison uses `work_selection`'s canonical order; no second ranker, new record state or write exists.
It returns up to three capacity exclusions, three policy exclusions and three weakest unchosen
maintained jobs; duplicates, excluded/closed records and active/shortlisted processes are protected.
The UI states the scope, missing evidence and partial coverage, preserves results across polling, and
requires explicit refresh after choices. The rolling admission ledger is never refunded by skipping.

The comparison refuses overlapping requests and shares the existing state-build guard, so an expensive
review cannot run concurrently with a summary rebuild. Canonical scoring stays in a coherent read
snapshot, without a writer lock. Pending-file I/O still scales with retained history; candidate scoring
and the existing workspace context still scale with record count. Failure releases both guards and
returns an explicit Retry state; no sources, timers, stored records or allowances change. Reverting
the code is schema-compatible. Local synthetic timings do not establish hosted memory headroom.


`POST /api/records/:id/outcome` retains `revision`, `kind`, `action_id`, `channel`, optional factual
`note` and `next_step`. Public requests with `sent_message` or `draft_version_id` return 410.
Draft creation/edit/clear, draft-decision answers and weekly-person writes also return 410 without
mutating saved data. Low-level historical writers/readers remain for fixtures, export, restore and
correction compatibility; production callers must use the retired boundaries.

For example, after a contact actually happened:

```json
{"revision":4,"kind":"sent","action_id":"actual-server-action-id","channel":"email","note":"Discussed the remit"}
```

Optional fields stay absent unless the user supplies them. The revision is captured when the form
opens. A stale save is refused; network failures preserve input and in-flight controls prevent
repeat dispatch. Record, interaction and outcome commit together, retaining idempotence/correction.
Use native accessible controls, visible focus, touch targets and text-only rendering of untrusted
content. Do not fetch per-card history on initial load. Missing intelligence stays visibly unavailable.

`tests/ui/opportunity-workflow.mjs` covers canonical order, 10/+10 at 100/1,000 candidates, single
summary/no eager histories, old routes, historical wording, failure/stale saves, retries, Undo and
reload on desktop/iPhone. Broad smoke, incoming-role, job-evidence and status journeys retain their
separate responsibilities. Historical outgoing preview scripts/captures remain evidence, not gates
for current behavior.

## Write locks and fault lines

Ingest, the email import, backup and the caretaker's published receipt take `.writer.lock`, then
SQLite. Research receipts, allowance rows, site saves and `ops/wo-live` take only the SQLite write
lock (`BEGIN IMMEDIATE`). The site waits 15 seconds so a click does not hang. Workers wait 120
seconds (`db.WORKER_LOCK_WAIT`). Acquiring `.writer.lock` logs the wait, releasing it logs the
hold, and a timeout logs before the run stops.

A wait over 200 ms logs `database is locked` with the unit, run id, pid and wait. A write held over
one second logs the caller and the hold time. A workspace build over 500 ms logs the view, the
duration, `cache=miss` and its phase split (`records`, `reads`, `relevance`, `intelligence`,
`selection`, `trim`). None of those lines includes SQL, bind values or record text.
`request_timings.py` keeps the last 50 save, summary-refresh and build timings per kind (builds
with their phases, including `encode`) in the server's memory only, for the Dashboard. A build
compresses nothing: a full summary is compressed when one is first requested;
a restart clears them. `tools/benchmark_save.py` measures the same save round trip on synthetic
fixtures. The same
run id is stored on the crash note (`pid`, `run_id` in `data/crashes/<unit>.json`). An email receipt
copies ingest `phase_seconds`, `seconds`, `import.write_lock_seconds` and `memory`: the service
cgroup's peak (which includes file cache), its anonymous/file split and limit/OOM event counts as
the run finishes, plus the process and largest-child resident peaks (`ingest.memory_evidence`).
Derived workspace state is
read before a write lock, and one research pass builds that state once.
Scan import also prepares historical observation clocks before acquiring its writer transaction.
The existing admission data-version guard rejects a concurrent commit and retries preparation before
any writes. Direct observation-import callers retain the original fallback scan. Global
history validation and clock ordering are preserved; total history scan CPU/I/O remains proportional
to retained observations, while the writer lock no longer covers that preparation in normal scans.

## Design rules

The shared [design standard](../AGENTS.md#design-standard) governs production readiness, dependency
direction, measured capacity, behaviour preservation and recovery.

- **Unknown stays unknown.** Missing pay, budget, eligibility or IR35 is not guessed.
- **Rules explain admission.** Candidate outputs retain the reasons that admitted them.
- **Discovery survives retries.** The dated TSV is saved before its dedupe index.
- **Failures remain source-local.** One unavailable source does not discard other results.
- **Completeness is explicit.** Partial reads cannot masquerade as a full observation.
- **Human and source state remain separate.** Public evidence never overwrites a human decision.
- **Newer facts win.** Timestamps protect current database facts and health from stale imports.
- **Coverage is measured.** Unread channels show unknown rather than a fabricated zero.
- **One door per source kind, one normaliser, one writer.** A fact looks the same whichever route
  it arrived by, and one advert URL or organisation name maps to one record.
- **No more data per item on any tab** (26 October). A change that condenses, rationalises or
  changes data considers the UX first: what can be deprecated, and what can be combined to give
  better information.

There are no model calls, sends, applications, registrations or purchases in scanner execution.
Public APIs are used instead of scraping.

## Standing rules from completed plans

These plans are in `archive/plans/`; their rules still bind.

- **One writer, provenance kept, the owner's edits win;** no unattended `repair`
  (code quality).
- **One mutation service and one normaliser;** `wo quality` separates defects from gaps; agents write
  with `--actor agent` (consolidation).
- **Ranking is derived, never stored,** and every rank is explained; unknown is neutral; weights live
  in `config/relevance.yml` (relevance and quality).
- **The decision loop:** the owner answers on the site; the agent processes, replies or withdraws
  (decisions, D6).
- **Approach evidence:** approved claims only, rates from `rates.md`, and QE work routed, never filtered
  out (problem-first search).
- **Stale adverts rank down, never close:** an advert no source has listed for 14 days ranks lower,
  with the reason shown (stale adverts).
- **Outreach evidence:** problem briefs, immutable draft approval and recorded outcomes; nothing is
  sent automatically (outreach,
  decision workspace).
- **Research reruns** keep the opportunity pilot's
  comparison and evidence discipline.
- **Codex on the VPS:** installation, authentication, spend and activation each need their own
  authority (Codex/VPS).
- **CVs:** `config/cvs.yml` is the registry (search and six CVs).

## How the code fits together

Read these contracts before changing code. The [module table](#modules) identifies each owner;
this section describes the dependency, transaction and behaviour rules those owners preserve.

- `workspace/records.py` is the one mutation service. The HTTP server, `tools/wo.py` and the
  importers all call it; do not write records through another path, and do not write one-off
  scripts for corrections: use `wo --actor agent edit|link|merge|archive|repair`. Each write checks the
  integer revision inside a `BEGIN IMMEDIATE` transaction and raises `Conflict` (HTTP 409) when stale.
  `records.py` is a facade: the code is in the `record_*`, `source_coverage` and `workspace_state`
  modules (table above), each importing only the ones before it in `workspace/test_record_layers.py`.
  The guard checks function-local imports and transitive local dependencies too. Its sole cycle waiver
  is the legacy `states.transition` forwarding API; readers use declarations, not that writer.
  Callers keep using `records.<name>`; a new service function goes in its module and is re-exported
  from `records.py`. Tests patch a name where it is defined (`workspace_state.state`), because the
  modules call each other directly.
- `workspace/record_views.py` owns read projections. `records.state()` batches enrichment inside
  one deferred snapshot; the browser requests `/api/state?view=summary` (12 recent human events,
  no `coverage.runs`, advert bodies or per-record outcome actions) and `/api/records/<id>/timeline`
  when a record opens. The CLI and default `/api/state` keep full history and fields through `state_export`: receipts
  are decoded one at a time into private anonymous temporary files. HTTP caches only file handles;
  concurrent clients have independent offsets and survive eviction. The read snapshot ends before
  transmission. Internal outcomes/week/research/import selection use `state_context` or
  `cached_context`, which retain all operational fields but omit the receipt archive. The existing
  Python `state()`/`cached_state()` APIs still return full dictionaries for compatibility. Relevance
  uses all interaction evidence before browser trimming.
- `tools/record_imports.py` owns scan/research ingestion and identity rules; `tools/wo.py` parses
  commands and keeps compatibility re-exports. Scheduled ingest calls the import service directly.
  Source upserts and organisation identity are implemented once in `record_identity.py`, below both
  ingestion and manual commands. Existing `record_imports` and CLI entry points re-export them.
- `workspace/js/` holds DOM, request and form helpers. `js/record-forms.js` holds the record dialogs
  (add a person, new, edit, state, work state, interaction, link, LinkedIn import). `app.js` composes
  these views with `navigation.js`, `record-detail.js`, `decisions.js` and `record-timeline.js`.
  Navigation owns selection and dirty state; detail/decision views use its methods. Delayed timeline
  reads update only the history section and preserve unsaved form input. The timeline controller
  caches eight recently used completed responses and deduplicates in-flight reads. Clearing it
  invalidates older completions/errors, which cannot repaint or evict newer requests. A revisited
  evicted history is fetched again; each response and its complete rendered history can still be large.
  No module imports `app.js`.
  `server.py` serves every `workspace/js/*.js` (the folder is allowlisted whole), so it holds browser modules only.
- `workspace/decision_intelligence.py` derives outreach briefs from the same record snapshot and
  `config/problem_playbooks.yml`: sourced facts, a hypothesis, bounded help, approved proof and a
  diagnostic question. It separates fit, access, evidence and readiness. Outcomes retain the
  server-derived brief used when they were recorded; sparse observations do not imply conversion odds.
  It also reads stated terms and constraints from each advert, anchors a hypothesis on one remit, and
  gates a decision-maker on a role read from the company's own page within 90 days.
  Historical status is delegated to `approach_history.ApproachHistory`: one pass over decisions and
  outcomes, then constant-time lookups per action instead of rescanning/sorting the archive. It
  retains references to the latest event and earliest recorded route per record/action scope;
  corrected events, legacy record-only events, unknown outcome kinds and stable ties keep their
  existing semantics. The module owns stage/recommendation constants (re-exported by `approaches`)
  and latest-draft selection shared with progress. A dependency guard excludes I/O and application
  orchestration. The index lives within the existing coherent read, with no shared cache or writer
  changes; full historical events remain available to progress and timelines.
  `approaches` likewise indexes organisation, employer and introducer links once per snapshot,
  preserving their original direction and order. It constructs only actions for the current status.
  Ordinary tests guard edge visits and action allocations; the
  [rebuild benchmark](commands.md#focused-development-checks) measures complete projection cost.
- `workspace/approaches.py` derives factual progress, next steps and permitted controls from the
  record snapshot. Personal outcomes do not create elapsed-time follow-ups or no-response states.
  Historical weekly rows remain readable, with no new membership writes. `work_selection` owns
  current This week admission/order; `js/progress.js`, `js/week.js` and `js/labels.js` share controls
  and wording. Existing contract tests check that advertised actions are accepted by the API.
- `config/templates.yml` supplies the on-demand People sending picker; browser preparation/copy
  controls are described above. Historical draft approval and scheduled preparation stay retired
  and templates do not control research admission. `js/message-format.js` renders preserved wording.
  `/notes` locates history and the separate [local reference library](reference-data.md); sending
  occurs in the external channel.
- `workspace/normalise.py` is the one set of normalisation rules (entities, whitespace, factual dates,
  name keys). Every import door and `wo repair` use it. Date fields hold dates or nothing.
- `workspace/relevance.py` derives a ranked band with reasons for every record from
  `config/relevance.yml`. It runs in read projections; flat record exports remain factual, while full state includes the
  derived comparison. It never changes a record. `tools/quality.py` holds the defect and gap checks and the deterministic
  repairs.
- `workspace/states.py` declares every stored enum and allowed transition as literals. `workspace/machines.py` declares derived progress, the actions and sections that follow from it, and decision status; both are listed in [state machines](state-machines.md). `workspace/db.py`
  owns connections, numbered migrations (`workspace/migrations/NNN_*.sql`) and backup/restore via
  `Connection.backup()`. Add a schema change as a new numbered file; `server.py` refuses to start on
  a missing or empty database or with pending migrations.
- Modules are not a package. `tools/wo.py` inserts `tools/` and `workspace/` on `sys.path`.
  `tools/row_shape.py` holds the flat record row and the one daily-scan TSV reader; do not add a
  second reader.
- Imports write normalised state fields only on insert (`IMPORT_INSERT_ONLY` in `record_imports.py`, re-exported by `wo.py`). Edits are
  marked in `record_edits` with their `actor`. Imports never overwrite them, but sourced research may
  fill a field an agent correction left empty. Source facts go to `observations` and `source_health`.
  One advert URL maps to one job, and adverts link to their organisation through `record_imports.link_organisation`.
  Every door that meets an organisation by name uses `record_identity.ensure_organisation`, also
  re-exported as `record_imports.ensure_organisation` for compatibility.
  A merged name follows its active survivor; a plain archive is reported and never recreated.
  Cross-source title/company matches attach only when stated location and engagement agree.
  Research keeps raw evidence but only newer stated facts can update fields; equal UTC instants
  cannot replace them. Merges copy observations to the survivor while retaining the originals;
  `wo quality` audits legacy omissions and `wo repair` restores only their evidence.
- Imports record `state_events` (extended by migration 008 with source scope, run ID and field
  changes) inside their own transactions. The Sources page's daily log that read them was retired on
  19 October; the events remain the record.
- `workspace/server.py` uses a bounded 32-connection listen backlog so parallel module loads can
  survive a brief pause in accepting sockets; the twelve-connection burst regression checks delivery.
  This does not change database locks or the serialized state-build guard. It serves `/api/state`
  and timelines, plus mutation routes. Mutations require a same-origin
  `Origin` header and `application/json`; the Host header must be loopback unless
  `--public-origin` is set.
- Discovery: `scan_boards.py` (boards and aggregator feeds), `scan_tenders.py`, `scan_awards.py`
  (public contract awards as supplier research signals, never open opportunities), `scan_hn.py`,
  `scan_alerts.py`, `scan_news.py` (funding signals) and `scan_companies_house.py` (registry facts for
  recorded company numbers) share `web_read.py` and `pending_store.py`
  (the job scanners also `job_rules.py`), write to `data/pending/`, and reach the database only via
  `wo import-scans`. `wo ingest` (`tools/ingest.py`) provides shared ingestion orchestration: the general daily VPS
  run uses `--keyless` plus approved keyed groups; the separate email worker calls it for the alert
  adapter. Local manual runs act on the development copy. Credentials come
  from the gitignored `.env` locally or encrypted systemd credentials on the VPS; never read or print their values. Config is `config/portals.yml`,
  `channels.yml` and `profile.yml`.
- `tools/refresh_views.py` regenerates `career-directory.csv`, `records/db/work-ops.json` and
  `records/career-brief.md` together from one snapshot, into `--root` (ignored by git; never commit
  them). `--check` rebuilds from the JSON and fails if the CSV differs; `ops/check.sh` runs it on views
  generated into a temporary folder.

Further reading: the sections above (why the scanners are split), `docs/adding-a-source.md`
(the recurring task), `ops/LOCAL.md` (development checkouts, the daily off-host backup, restores),
`ops/OPERATIONS.md` (the hosted setup and where to look when something fails); repair-progress notes
on failure decisions are in `.claude/commands/decisions.md`.

The job/person detail entry point uses `workspace/js/approach-brief.js` to compose current evidence,
contact, CV and application context. Preserved wording is historical context, not a new message
operation. Background evidence and editing remain under **Sources and details**.

## Focused automation (schema 017)

The trusted research worker consumes `opportunities.research_ids` from one shared state build per
pass. It alternates chosen and qualification lanes as described above. A single bounded attempt
lookup supplies both the latest visible outcome and last substantive rotation time, rather than
decoding full receipt archives for each candidate. Exact employer-board reads retain at most 30
selected posting excerpts of 4,000 characters per pass, with one read per board and explicit
posting URL/API/read-time provenance. A readable but unrelated page does not establish remit or
suppress bounded discovery. `research_cache.py` reuses interpretations only against matching context/policy and
fresh source reads. `automation_usage.py` admits serialized CLI invocations with a durable SQLite
reservation; failures are charged conservatively. The socket runner also serializes processes.
Person records may carry only their own contact-route checks through the existing canonical
research importer; job checks retain their current contract. User notes are preserved. A check's
optional `provenance` (`public_read`, `owner_confirmed`, `prior_correspondence`) is part of the
projection key (`evidence_checks.route_class`), so a later failed public read replaces an earlier
public read but never a confirmation from the owner's own records.

Evidence briefs use `workspace/evidence_briefs.py` for the shared read-only projection and
`tools/brief_research.py` for bounded source work and replay validation. Versioned `evidence_brief`
payloads stay in research observations; no migration is required. Projection reads their latest
valid value independently of the recent-review window and combines it with canonical context.
Its batched reads pass the record ids as one JSON list, never one bound value per record, so
loading is not capped by SQLite's bound-parameter limit; interactions are read in id order.
Prepared approaches also survive that window, including an explicit empty approach withdrawal.
The `brief` schema is allowlisted at both ends of the existing socket. `briefs: false` keeps this
research path inert without enabling any message operation. The site shows evidence without a new write API.

`approach_preparation.py` retains historical validators/report readers, but its generation and import
entry points are retired. The model and socket allowlists reject preparation before reserving a call.
Old preparation batches are retained and marked retired without importing drafts or closing decisions.
`research_context.py` owns factual packet filtering independently of historical wording helpers.
`brief_research.apply` derives and validates outside the write transaction, then rechecks affected
record revisions and linked dependencies before atomic canonical import. Alternative findings use the same guard; legacy batches lacking dependency snapshots remain retained for review without import. Human edits, exclusions and retry safety remain.

`caretaker.py` reads fixed service states, loopback health and bounded operational receipts; it
publishes zero-model receipts and deduplicated decisions through canonical decision functions.
`caretaker_processing` and `research_processing` are safe state projections, with no source bodies
or authentication material. Neither worker changes service configuration, repairs data, deploys,
or sends outreach. [Detailed boundaries and recovery](vps-caretaker.md).

### Incoming hiring-process evidence

`records.preview_incoming` and `records.save_incoming` are the shared API/CLI entry points;
`incoming.py` composes the canonical writer, organisation resolver, URL identities, links,
observations and dated state events in one transaction. Migration 018 adds replay receipts and
private documents. A confirmed existing process can enter its actual stage directly; ordinary
transition edges remain unchanged. No outgoing interaction or application is manufactured.
`incoming_documents.py` extracts inert text in a timed, resource-limited subprocess (Linux memory
limit); originals are base64 text in SQLite so full JSON backup/restore remains lossless.
`research_scope.py` shares focus membership with the worker and projects research eligibility
separately from recorded attempts. Active jobs retain selection regardless of discovery rank;
private sources remain explicitly unverified context and cannot become quoted public verification.

### Pending observation memory

The existing JSON observation store and atomic replacement remain unchanged. Serialization streams
through `pending_store.atomic_write_json`, avoiding a full encoded copy alongside the parsed store.
Reads use `observation_reader` to decode root maps one entry at a time using Python's JSON decoder,
without retaining the complete serialized file. Source-status reads discard item values after
parsing; full store reads retain every item and unknown field. Duplicate keys and incomplete JSON
fail closed. Peak temporary decoding storage follows the largest entry, while retained item/source
maps and identity keys still grow. This reader avoids an additional dependency and a data conversion;
the measured decision
records the memory/time tradeoff and recovery.
Alert replay retains only the prior identities it is comparing; ingest passes bounded alert counts
to the email worker without another whole-store read. Source text, lifecycle, checkpoint ordering
and writer locks are retained. Memory and I/O still grow with the pending store; local RSS savings
are not a measurement of hosted cgroup headroom.

## State export capacity

The 23 October capacity ledger records measurements and tests.
Full output size and database storage still grow with history; no receipt is truncated, archived to
another store or deleted. Processing memory is bounded with respect to **receipt count**, not record
count, interaction history or the largest individual receipt. Those remain material scaling limits.

Full HTTP exports reuse one private disk-backed JSON/gzip artifact until committed data, configuration
or the London date changes. Builds share the state build lock; old cache handles close before a new
build, while already-open clients retain their own descriptors. A caller-owned transaction bypasses
shared caches. Temporary file exhaustion fails the export before response headers; the database
remains intact, and a later request can rebuild. Nothing persists these disposable cache files across
restart. The declared VPS services already set `TMPDIR=/srv/work-ops/run`; no service permission or
memory limit changes are needed. Budget disk space for the current raw/gzip pair plus any older
artifacts still held by active readers. OS file caching and overall service memory still need hosted
measurement; local Python/RSS results do not establish the VPS cgroup footprint. Existing cache
invalidation, short write transactions, WAL and revision checks remain in place.

### Bounded research before visible selection

The qualification pool retains thirty promising candidates independently of the ten visible
working places. The existing research scheduler rotates its bounded targets through this pool;
call budgets and short writer transactions are unchanged. Admission retains at most sixty new
records per rolling seven days, counted in the existing atomic ledger. Source/personal records
are never deleted to free slots. Current open-source evidence prevents age alone from expiring
a qualification candidate. Email delivery status exposes the latest actual import deferrals even
after empty polling runs. Reverting the capacity configuration restores the former selection
limits without deleting newly retained records.
