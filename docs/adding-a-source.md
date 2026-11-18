# Adding an opportunity source

[Documentation index](README.md) · [Repository home](../README.md)

Use this sequence for a manual adapter. Code or configuration alone is not coverage.

## 1. Identify the channel

Decide whether the source adds a route to work or duplicates an existing route. More employer ATS
boards do not expand agency-contract coverage. Check measured gaps first:

```sh
.venv/bin/python tools/coverage_report.py --config config --data data
```

Add or update the source's entry in `config/channels.yml`, the source registry behind CLI coverage:
`finds` (roles, contracts, approach, people or networks), `method`, `access` (free, key-mac,
key-server, account), `runs` (daily only if the VPS's `wo ingest --keyless` reads it, which for a keyed
source means the owner's decision, an entry in `KEYED` in `tools/ingest.py` and its key in the encrypted
credential store; attended,
'off' or not-built otherwise; quote 'off'), `rules`, `gives`, and `measure` (its `source_health`
providers or scope prefix) so health and yield are measured. List any open decision it waits on
under `decisions`. Do not describe an attended command as scheduled.

## 2. Confirm permitted access

Use a documented public API: unauthenticated first, then a free credential if approved. Do not
scrape inventory that is not offered for programmatic access. `wo fetch <url>` checks what a
candidate endpoint returns. If it exits 2 (challenged, refused or disallowed), that route is closed:
record the channel as `blocked` and look for an API, feed or email alert instead. Store environment-variable names in
`config/portals.yml`, never credential values.

## 3. Define identity and completeness

Choose a stable native ID and source scope before coding. Provider-local IDs without scope are
unsafe. Specify what proves a complete read, what a pagination cap means, which statuses are
explicit closure, and whether absence is authoritative. A moving tender date window is not.

Never close from elapsed age or a closing date alone. Jobs require explicit source closure or two
complete source-scoped absences at least 24 hours apart. Failure, partial access and anomalous empty
results cannot advance lifecycle.

Decide whether absence from the source means anything. A board lists every open role, so it does. A
query feed is a moving search window and an alert email reports only what it contains, so neither
does: pass `absence_authoritative=False` to `observe` and add the scope prefix to
`wo.PRESENCE_ONLY`. A provider-stated expiry (`current.expires`, closed through `expired_on`) is
explicit closure evidence.

## 4. Implement

For another job feed, extend `scan_boards.py`:

1. Build bounded requests from config; read credentials through environment variables. Read
   through `web_read.fetch_json`/`fetch_text`, which retry transient failures and log each request.
   Wrap quota-counted requests in `web_read.single_attempt()`. Put `**web_read.failure(e)` in a
   failed receipt's `detail`, so the failure decision can say what kind of failure it was.
2. Normalize native ID, company, title, location, URL, dates, text, structured pay and raw fields.
3. Use `job_rules.observed_terms()` for engagement, pay, rate, IR35 and workplace.
4. Separate required probe fields from optional facts; missing optional data is not schema failure.
5. Emit a source receipt on success or failure; failures use `complete: false`.

A genuinely different record type gets a sibling such as `scan_tenders.py`, `scan_hn.py` or
`scan_alerts.py`. A source that reports facts about an organisation rather than adverts (for
example funding news, `scan_news.py`) emits `signal` observations, which `wo.import_signal` attaches
to the organisation. Supplier awards use `commercial_signal`, never funding's `signal` kind.
Use source-scoped observations accepted by `wo.py import-scans`; append-once discovery TSVs are
needed only for adapters whose import contract uses them. Awards use observations directly. Add
the adapter to `tools/ingest.py` `default_adapters()` when authorized for the daily run, and map its
scope to its adapter in `failure_tasks.py` so one failing source cannot hide another adapter crash.
Keep moving request windows and read timestamps outside the content identity: unchanged source
facts must deduplicate across daily reads while native identity and changed facts remain distinct.

## 5. Test offline

Cover:

- native identity and scope collision safety;
- eligibility and normalized current facts;
- a refresh clearing facts no longer stated;
- isolated credential, HTTP and parsing failures;
- pagination caps reported `partial`;
- no absence from partial, failed or anomalously empty reads;
- two complete absences at least 24 hours apart before job closure;
- explicit closure and stale imports not rewinding newer SQLite facts or health;
- propose-once retry behavior.

```sh
.venv/bin/python -m unittest discover -s tools -p 'test_*.py'
```

## 6. Probe and run manually

Run the bounded read-only schema probe first. A pass proves only that the current payload shape is
readable; it does not prove useful yield.

```sh
SOURCE_KEY=… .venv/bin/python tools/scan_boards.py --portals config/portals.yml --probe mysource
```

After a pass, enable it as a manual adapter, run deliberately, inspect `last-run.json` and
`observations.json`, then import and inspect SQLite:

```sh
.venv/bin/python tools/wo.py --db data/work-ops.db import-scans data/pending
.venv/bin/python tools/wo.py --db data/work-ops.db state
.venv/bin/python tools/wo.py --db data/work-ops.db import-report
```

Confirm source health, completeness, eligible yield, normalized terms, stale-observation count and
review exceptions. Do not infer coverage from fixture tests, a successful probe, or `enabled: true`.

Then follow [the data pipeline runbook](data-pipeline.md) from step 4: `wo quality`,
`wo --actor agent repair`, close the gaps that matter with a research batch, and regenerate the views.
New organisations the source introduces arrive without a description, so they appear as gaps.

Legacy hub files are retained only for reconciliation. Do not mirror configuration into
`records/hub/` or edit command-center Markdown as a runtime data path.
