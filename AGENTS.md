# Agent instructions

The entry point for every coding agent; Claude Code imports this file from CLAUDE.md. Before
changing anything, read [CLAUDE.md](CLAUDE.md): the standing rules (approval boundary, decisions on
the site, live-data gateway, deployment, record invariants). Claude-specific hooks and shortcuts are
labelled as such.

Reference material loads only when a task needs it: [docs/commands.md](docs/commands.md),
[docs/architecture.md](docs/architecture.md) and [docs/configuration.md](docs/configuration.md).

This is a public snapshot of a private, single-user repository. Its plans and history stay private;
references to `plans/` or `archive/` in comments name documents that are not included here.

## User documentation

[README.md](README.md) is the product entry point; [USER-GUIDE.md](USER-GUIDE.md) owns first use,
the weekly workflow and user recovery. [docs/README.md](docs/README.md) is the task-oriented index;
[documentation format](docs/documentation-style.md) records the structure and verification
checklist. Keep setup in ops/LOCAL.md, commands in docs/commands.md and configuration in
docs/configuration.md. Interface captures must come from synthetic fixtures.

## Development workflow

- For code changes and verification work, use the repository's
  [verify-changes skill](ops/skills/verify-changes/SKILL.md). Include its documented visual
  comparisons when layout, styling or rendered content changes; use its lighter checks for
  prose-only edits.
- Clear, small, reversible changes do not need separate brainstorming or approval. Plan ambiguous
  or cross-cutting work. Prefer test-first for reproducible bugs and consequential behaviour.
  Visual work, configuration and behaviour-preserving refactors need suitable verification, not
  forced failing unit tests.
- Every plan addresses scalability/capacity, concurrency and data integrity, code quality and
  reuse, verification, observability and recovery in proportion to its risk. Inspect and preserve
  existing mitigations before proposing replacements; longer lock timeouts do not replace short
  transactions.
- One owner per concern ([code map](docs/architecture.md#how-the-code-fits-together)),
  dependencies in one direction (guarded by `workspace/test_record_layers.py`) and modules split in
  place, without a folder restructure.
- For attention, ranking or UI changes, follow
  [weekly attention and database safeguards](docs/architecture.md#weekly-attention-view): keep
  selection, presentation and recorded state separate. No more data per item on any tab:
  deprecate or combine before adding ([design rules](docs/architecture.md#design-rules)).
- Retained behaviour changes need automated coverage or an explicit gap and alternative
  verification. Show every new guard, invariant or regression test failing against a deliberate
  violation before relying on it. Pin expected values independently: a test that reads its
  expectation from the table or function under test is circular.
- For state, scoring or intelligence changes, use the
  [synthetic rebuild comparison](docs/commands.md#focused-development-checks): keep a baseline,
  measure cold and invalidated rebuilds plus peak memory at several sizes, and compare outputs.
- For prose-only documentation changes, check the diff, local links/anchors and any changed
  commands; application suites are not required.
- Preserve unrelated work. Run relevant checks on the final state and report actual outcomes,
  skipped checks and remaining limitations.

## Design standard

- One owner per concern: each responsibility, fact, setting or service belongs to one module,
  document or unit. Other components call or link to it instead of copying it.
- Dependencies point from CLI/HTTP/UI/schedulers to application logic, then domain rules and
  storage. Keep I/O at the edges and important boundaries under tests that fail when crossed.
- Prefer small, cohesive modules and explicit interfaces. Split for a concrete change or a measured
  cost, not speculative flexibility.
- Refactors preserve behaviour: obtain a passing baseline, move code verbatim where practical,
  prove new guards reject violations, and run the full relevant checks on the final state.
- Measure scaling costs: query growth, retained history, payload size, peak memory, concurrent
  requests and lock duration where relevant.
- Declare infrastructure in version-controlled configuration and scripts, with least privilege
  and documented recovery. Approval boundaries still govern infrastructure changes.

## Shared commands

`/deploy` follows [docs/deploy.md](docs/deploy.md) and `/caretaker` follows
[docs/caretaker.md](docs/caretaker.md), in Codex as a project instruction and in Claude Code as a
slash shortcut. Creating or editing a command does not invoke it. A push is not a deployment.
