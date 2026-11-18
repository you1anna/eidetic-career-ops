# Find and maintain reference data

[Documentation index](README.md) · [Configuration owners](configuration.md)

This repository supports the owner's private search for worthwhile work, relationship tracking and
attended preparation of application materials. The hosted workspace and the local message library
have different owners. The public career/ops pitch and demo describe the product opportunity;
they do not own private career facts or prove a multi-user service exists.

## Where to look first

| Question | Authoritative starting point |
| --- | --- |
| What is the current direction, queue or released version? | Plan index; older dated entries are evidence as of their date. |
| What happened with a person, role or application? | Hosted record and its history through `ops/wo-live`; [command reference](commands.md). A local database is a dated development copy. |
| What wording, cover letter or tailored CV have we used before? | Private local `message-library/README.md`, then `message-library/catalog.json`; filter before opening matching files. |
| What template does People offer? | [config/templates.yml](../config/templates.yml), consumed on demand by People. The local library's `pack` entries are generated copies. |
| Which general CV is canonical? | [config/cvs.yml](../config/cvs.yml) owns the six Google Docs and readiness. The local catalogue identifies attachment/edit files, tailored variants and extraction limits; a newer local file does not establish approval. |
| What experience supports a claim? | Career evidence and its linked evidence. September materials retain historical claims; current positioning belongs in [config/profile.yml](../config/profile.yml). Resolve conflicting facts from their dated sources before reuse. |
| What is currently in Gmail, or was actually sent? | Gmail for current mailbox state; library entries are snapshots. Site outcomes record reported events and do not synchronize mail. |
| What determines eligibility, fit, collection or research? | [Configuration ownership](configuration.md), then the named executable config and reader. Old rates, modes and plans do not override them. |
| What belongs in the public copy? | [Public export runbook](public-export.md); exporting is separate from pushing this private repository. |

## Message library: sources and generated views

The private `message-library/` folder exists only on machines where it has been supplied. A clone,
cloud chat or VPS checkout must not assume it is present or recreate it from private Git history.
If absent, report that limitation and use only the available canonical references; do not create
a second message pool. Paths in its catalogue are relative to the library unless absolute.

Read `messages`, `cvs`, `snippets`, `by_person` and `by_role` selectively. Check `status`, `series`,
`blocking`, `readiness`, `use`, dates and source provenance before proposing reuse. `built` dates the
catalogue build, not a fresh Gmail read or approval. `current` selects a local general-CV export;
`variant` preserves a tailored example. Neither means it was submitted. Approval and submission
must remain separate facts.

Add requested application material under
`message-library/source/applications/<date>-<company>/`, with its Word/text exports and
`records.json` manifest. Follow the existing manifest shape; retain provenance and explicit status.
Do not hand-edit generated `catalog.json`, `INDEX.md`, `README.md`, or the generated `templates/`,
`people/`, `sent/`, `snippets/` and `cvs/` directories. The existing local `build.py` owns rebuilding;
[commands](commands.md#local-message-library) owns invocation and safety checks.

The builder reads the saved `source/gmail-career-drafts.json`, dated correspondence under `data/`,
legacy `gmail-drafts/`, configured templates/CVs and local CV/checkpoint files. It does not query
Gmail, send anything, update live records or make all those sources current. Preserve the inputs
and check the paths in `build.py` before rebuilding on another machine. September CVs stay frozen.

## Tracking and recovery

- Git tracks guidance, application code and declared configuration. The root allowlist excludes
  `message-library/`, `gmail-drafts/`, `data/` and generated personal exports. Keep personal application
  files and catalogue content out of commits and fixtures; do not force-add them to solve backup.
- The hosted database owns recorded contact/application events, revisions and historical saved
  wording. Preparing or approving a local document does not record a send or application there.
  Record an actual event through the existing UI or command only when evidenced and authorized.
- Repository VPS/off-host backup scripts cover the documented site stores, not this local library,
  its builder or external CV originals. **Local-library backup/restore is unverified.** A Git clone
  plus a site database restore cannot reconstruct it. Preserve these local inputs; confirm a separate
  private backup and restore before any deletion or migration. No backup service is activated here.
- Rebuild output directories are replaced in place, without an atomic swap or concurrency lock.
  Use one builder at a time, avoid edits/reads during a rebuild and preserve a private copy first.
  After failure, retain the sources and diagnose before rebuilding; validate catalogue paths before
  using results. This is an attended local catalogue, not a synchronized or concurrent message store.

## Historical reference boundaries

`archive/`, `provenance/`, September rates/modes and dated research retain historical evidence.
Their old deadlines, routing instructions and command-center paths do not govern current work.
Do not silently update historical messages to make them look newly approved. Keep current rules
in their owner above and link to historical evidence when needed.
