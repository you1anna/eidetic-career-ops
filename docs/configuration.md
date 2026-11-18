# Configuration ownership

[Documentation index](README.md) · [Repository home](../README.md)

For site use, start with the [user guide](../USER-GUIDE.md). This reference is for
maintainers changing the rules, registries or running services. Each setting has
one owner; a plan or runbook does not override executable configuration.

| Canonical location | What reads it or follows it |
| --- | --- |
| [.python-version](../.python-version), [requirements.txt](../requirements.txt), [requirements-dev.txt](../requirements-dev.txt) | Python version; PyYAML/pypdf runtime pins; pytest/xdist for development only. The VPS installs runtime dependencies. |
| [tests/ui/package.json](../tests/ui/package.json), [package-lock.json](../tests/ui/package-lock.json) | Browser-test dependencies, installed with `npm ci --prefix tests/ui`; not VPS runtime dependencies. |
| [ops/email-intake/wrangler.vps.jsonc](../ops/email-intake/wrangler.vps.jsonc), [src/buffered.ts](../ops/email-intake/src/buffered.ts), [migrations](../ops/email-intake/migrations/) | Production Cloudflare Email Worker + D1 buffer, bounded pull/ack protocol and Worker secret binding. [Email runbook](linkedin-alerts.md) owns routing and activation evidence. `wrangler.jsonc` / `src/index.ts` are the separate historical direct-POST pilot. |
| [ops/vps/wo-email.service](../ops/vps/wo-email.service), [wo-email.timer](../ops/vps/wo-email.timer) | Dedicated 15-minute server collection; scoped `WO_EMAIL_BUFFER_TOKEN`, private spool, parser/import receipts. `config/portals.yml` owns matching rules; the sender registry below owns admission. |
| [config/email-sources.json](../config/email-sources.json) | Exact email senders and source labels, shared by the buffered Worker and Python intake/projections. Gmail forwarding must be activated separately; configuration is not delivery proof. |
| [config/research.yml](../config/research.yml) | Research activation, per-pass calls and all aggregate/lane invocation allowances. The existing [service](../ops/vps/wo-research.service) and [timer](../ops/vps/wo-research.timer) own resource limits and schedules. [Runbook and activation boundary](vps-research.md). |
| [config/profile.yml](../config/profile.yml) | `workspace/profile_rules.py` reads candidate identity, former-employer restrictions, private-topic exclusions and employer history for imports/mail matching. Also the personal strategy/narrative reference; scanner parameters and relevance weights remain separately owned. |
| [config/relevance.yml](../config/relevance.yml) | `workspace/relevance.py`: record fit and reasons; `discovery_capacity` owns the 10 visible places, 30 research candidates, 60 automatic additions per seven days and unresolved qualification age (14 days), consumed by shared selection and import. Separate CV routing and explicit employer exclusions never change relevance weights. |
| [config/templates.yml](../config/templates.yml) | Canonical wording/checks for the on-demand People sending picker and local library pack. Browser preparation is temporary; historical draft writes and scheduled preparation remain retired. |
| [config/cvs.yml](../config/cvs.yml) | Six CV titles, Google Docs links and draft/ready state. Per-job manual choices live in the database and can reset to the suggestion. |
| [config/problem_playbooks.yml](../config/problem_playbooks.yml) | `workspace/decision_intelligence.py`: bounded hypotheses, proof, questions, freshness limits and transparent Outreach priority weights. |
| [config/outreach.yml](../config/outreach.yml) | `workspace/approaches.py` and `workspace/outreach_routes.py`: historical weekly settings, work-process intervals, contact verification and dated route evidence. Routes refine the shared brief without adding a separate lifecycle. |
| [config/portals.yml](../config/portals.yml) | `tools/scan_*.py` and ingest: enabled sources, search parameters and candidate filters. `search_queries` and `seniority_boost` are attended references, not automated search or scoring settings. Collection limits and pacing belong to the collection policy below; credentials remain outside Git. |
| [config/collection.yml](../config/collection.yml) | Provider daily ceilings, scheduled targets, pacing, full-intake discovery allocations, registry refresh, background days and observed account allowances. One validated loader serves dispatch, diagnostics and the Dashboard. |
| [config/channels.yml](../config/channels.yml) | Source registry for CLI coverage and the coverage report: declared routes, access and status. It neither enables sources nor schedules reads; health comes from receipts. |
| [ops/vps/](../ops/vps/), [ops/local/](../ops/local/) | Service units and scripts control deployed invocation and backups. [ops/OPERATIONS.md](../ops/OPERATIONS.md) is the hosted runbook; [ops/LOCAL.md](../ops/LOCAL.md) covers the Mac and development setup. |

Shared qualification in `tools/job_rules.py` checks software remit and explicit engagement evidence
for scanner rows and stored adverts. Conflicting terms stay visible, fixed-term and permanent terms
remain distinct, and unknown facts stay unknown. Edit the owning configuration or rule rather than
adding another set of weights, filters or scheduling instructions to a runbook.

Private local sources and generated catalogues are mapped in [reference data](reference-data.md);
they are not runtime configuration or a second live database.

## Agency discovery and search balance

`config/portals.yml` owns the shared Adzuna/Reed phrases: engineering leadership, AI products,
integration/productivity/delivery and quality leadership. The opening phrases alternate those
priorities so a provider call cap does not spend every call on one family. The six remote phrases
cover leadership, AI and integration/productivity. Both permanent and contract roles remain eligible;
the provider call limits, pacing, page counts and detail budget still apply. Feed names are retained
because they form part of source identity; their historical labels are not a description of every query.

Adzuna rotates all 21 first-page searches across days; configuration order cannot permanently
exclude the London phrases. Current-work searches with recorded query provenance get priority.
Until a job has that provenance, rotation cannot promise its refresh. Reed rotates searches and
shares its provider budget with detail reads. [Scanner behaviour](scanners.md#configuration)
owns partial-result retention and reporting.

Agency qualification is attended research through the existing importer. People uses
`workspace/js/people.js` to project explicit recruiter kinds, agency consultant links and dated
`desk_summary` evidence into its filter and bounded agency list. The existing read projection in
`workspace/record_views.py` reserves one of its three research slots for an agency's latest explicit
desk assessment, including withdrawals; later unrelated reviews cannot hide or renew it.
It creates no workflow state or
scheduled research eligibility. [People controls](../USER-GUIDE.md#choose-your-people) describe use;
[the data pipeline](data-pipeline.md) owns source intake and correction.

## Weekly outreach pilot

The five-person pilot is superseded. `plan` settings in `config/outreach.yml` are retained only for
historical weekly storage/restore helpers; HTTP/CLI weekly-person writes are retired. The current
shared projection is `workspace/work_selection.py`: discovery sorts by suitability, evidence date,
engagement preference and stable identity. A stronger permanent role can outrank a weaker contract;
unstated engagement stays unknown. This week initially shows three Best opportunities, expanding
to ten. [Selection contract](architecture.md#weekly-attention-view) owns the detailed rules.

## Focused automation (migration 017)

`config/research.yml` selects `engine: codex_cli`, `enabled: true`, `focused: true` and
`max_calls: 6`. CLI configuration rejects a higher per-pass maximum. The `allowances` block in
that same file owns research 12/day and 84/week; all automation 16/day and 100/week; incident 4/4,
question 6/6, review 1/1 and evaluation 5/5 (day/week). The retained preparation sublimit is 15/week
inside research, although preparation is disabled. `workspace/allowances.py` validates/reads these
values; `tools/automation_usage.py` reserves them in the existing invocation ledger. London days
and Monday weeks, restart/crash accounting and every effective allowance are unchanged.


`prepare: false` records retirement. Even a legacy `prepare: true` cannot dispatch message
preparation or replay a draft. `briefs: true` maintains work evidence for the shared active/shortlisted
and top-ten discovery set. Legacy `focused: false` cannot broaden that selection. Existing schedules,
research caps and source policies remain. Personal route history is retained without enrolling
personal outreach. [Research behavior](vps-research.md#what-it-does).

`/srv/work-ops/data/research.pause` pauses new research work; `caretaker.pause` pauses caretaker
checks. These are explicit operator files, not site buttons. A new caretaker timer is installed
without activation; routine deployments preserve its existing enabled state. See [recovery](../ops/OPERATIONS.md#focused-research-and-caretaker-recovery).
Caches are implementation details: seven-day interpretation/discovery reuse and 24-hour blocked-read
cooldown, invalidated by context/source/policy changes. They do not extend evidence freshness.

The historical one-time research acceptance used `ops/vps/wo-research-acceptance.timer`: an absolute
19 October 2026, 10:05 Europe/London calendar entry. It completed on that date and has no next weekly run. The retained service
can write only the acceptance-report directory; the database is mounted read-only. It adds no model
allocation and cannot activate research. [Report delivery and recovery](vps-research.md#one-time-monday-acceptance).

## Collection policy

Edit [config/collection.yml](../config/collection.yml), review its diff, run the focused collection
tests and the normal strict gate, then use the [release workflow](deploy.md). There is no website
settings form or environment override for these limits. `ops/wo-live collection-policy --json`
shows the deployed file/version, effective policy, production authority and current usage without
making a provider request. A local `wo collection-policy --json` reports local accounting as unknown
unless a synthetic store exists; it does not create a second production allowance.

Effective defaults (the YAML file is authoritative):

| Provider | Hard ceiling / London day | Scheduled target / day | Discovery and refresh |
| --- | --- | --- | --- |
| Adzuna | 10 | 6 | First pages; at most two discovery searches when intake is full; 4.1s minimum spacing |
| Reed | 10 | 6 | At most four searches plus two details across all runs; two discovery searches when full; 2.6s spacing |
| Each employer-board provider | 10 | 6 | Due current work first, then fair employer rotation; up to two discovery slots when full |
| Companies House | 10 | 9 | Three company bundles/day; refresh every 30 days or once after becoming relevant to chosen work; 0.35s spacing |
| HN, each procurement provider, UKTN | 10 | 6 | Daily when replenishment is useful; Tuesday/Friday when full; tender and award reads share a provider |

Scheduled targets count retries and all runs in the day. Research/attended reads can spend the
remaining allowance up to ten. Failed, crashed and uncertain reservations remain spent. A provider
is shared across all employers/endpoints, not allocated per board or feed. These limits cover this
site's dispatchers, not unrelated applications using the same account. Email polling/infrastructure,
Codex invocation allowances and systemd schedules are unchanged.

Intake is full when the existing 30-candidate qualification pool is full **or** the shared rolling
60-addition allowance is exhausted. Collection does not own those numbers: they remain in
[relevance configuration](../config/relevance.yml), alongside ten visible opportunities. One snapshot
per ingest supplies those counters, current-work source identities and query provenance to scanners.
Maintenance becomes due after 24 hours; discovery retains up to two due slots per provider.

For example, reducing Adzuna's scheduled target to four leaves its ten-call ceiling unchanged.
Reducing a ceiling also requires its scheduled target and suballocations to fit. To pause a provider,
set its daily limit, scheduled target and applicable allocations to zero; retain its pacing setting.
Unknown keys, missing providers and contradictory allocations reject the policy. A policy error
blocks covered dispatch visibly; the rest of the site and existing records remain available.
Historical account allowances are observations, not permission to increase collection.

The 48-hour vacancy-confirmation window, conservative closure, explicit expiry, revision checks,
security rules and cache storage bounds remain code invariants. A scheduling skip never changes
an observation or last-check time. Recent exact canonical listings may be reused with their original
timestamps. No second archive of full API responses is introduced. Recovery and the cutover-day
hold are documented in [operations](../ops/OPERATIONS.md#collection-accounting-and-recovery).
