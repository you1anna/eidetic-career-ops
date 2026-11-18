---
name: verify-changes
description: Verify code changes with repository-owned tests and concrete evidence, including browser journeys, failure diagnostics and stable visual comparisons when UI behaviour changes. Use for implementation validation or investigating a failed check; scale down to links and commands for prose-only edits. Does not authorize deployment or external monitoring.
---

# Verify changes

Use the repository's maintained checks as the preferred execution path; the
requirements determine correctness, even when an existing test disagrees. This
skill chooses evidence; it does not replace the project runner, browser skills or
approval boundaries. Discover available tools rather than assuming a particular
browser runtime is installed.

## Choose evidence that can disprove the change

- Read the repository's instructions and relevant test commands. Preserve dirty
  work and obtain a focused passing baseline before behaviour-preserving edits.
- Connect each changed behaviour to an independently specified expected result.
  For a reproduced bug, show a regression test failing before the repair. Prove
  new guards reject a deliberate violation; do not derive expected values from
  the implementation under test.
- Select the smallest meaningful checks while editing, then the required final
  gate. Test data integrity, conflicts and recovery for writes; UI screenshots
  do not establish these properties. Preserve existing capacity/concurrency tests.
- For prose-only edits, check the diff, local links/anchors and changed commands.
  Do not launch browsers or application suites without a relevant claim to test.

## For rendered UI changes

Identify the affected journey: entry state → action → expected visible result.
Use existing retained browser tests first. Exercise applicable empty/loading,
failed-operation and recovery states, then inspect desktop and mobile rendering.
Inspect console/page errors and failed requests, distinguishing intentional error
cases from unexpected failures. A successful build or screenshot alone is not
interaction evidence. Automated mobile emulation is not real-device acceptance.

If the repository has visual comparisons, run the relevant suite for layout,
style or rendered-content changes. Read [visual checks](references/visual.md) for
baseline review, reproducibility and diagnostic evidence. Keep human inspection
of the actual rendering alongside assertions.

For unresolved slow interactions, layout shifts, network or memory questions,
read [diagnostics](references/diagnostics.md). Use deeper profiling only for a
specific question; do not add an always-running monitoring service to finish QA.

## Keep verification efficient and honest

- Reuse fixtures, launch helpers and the existing runner. Keep data synthetic,
  per-run databases/ports/outputs isolated, and concurrency within project limits.
- Retain bounded failure evidence; collect costly traces on request. Measure
  additional runtime and artifact size before expanding coverage. Prefer a few
  representative states over duplicate screenshots of every page.
- A retry cannot erase the first failure. Diagnose flakiness; never broaden
  tolerances, suppress errors or accept new baselines merely to get green results.
- Use normal scoped permissions when required. Never disable sandboxing, invent
  unavailable tool APIs or switch access routes after an authorization denial.
- Report the actual commands, pass/fail/skip counts, evidence paths and material
  gaps concisely. Distinguish local checks, deployed evidence and user acceptance.
  State when a required check could not run; optional missing diagnostics do not
  prevent other useful checks.

## Leave the workspace tidy

Before handoff, stop the browsers/preview servers owned by the task and remove
its disposable fixtures, evaluation workspaces and resolved diagnostic captures.
Keep committed baselines and useful final test receipts. Retain unresolved failure
evidence with its exact path and cleanup procedure; do not delete it to make a
failed run appear clean. Never sweep arbitrary temporary directories or stop
another session's processes.

When changing a test harness, verify cleanup after success, failure and normal
interruption. Preserve the original failure/cancellation status, release only
owned locks, and document recovery after an uncatchable kill or power loss.
