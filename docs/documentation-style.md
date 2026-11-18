# Documentation format and ownership

[Documentation index](README.md) · [Repository home](../README.md)

The documentation uses a product README, task-oriented guides and a separate technical reference.
It stays in Markdown beside the code, with stable relative links and a single owner for each topic.

## Keep one owner for each topic

| Content | Authoritative location |
| --- | --- |
| Product purpose and first navigation | [Root README](../README.md) |
| First use, weekly workflow and user recovery | [User guide](../USER-GUIDE.md) |
| Navigation by task | [Docs index](README.md) |
| Development prerequisites, preview and test setup | [Local development](../ops/LOCAL.md) |
| Hosting, deployment, pause and recovery | [Operations](../ops/OPERATIONS.md) |
| CLI syntax | [Commands](commands.md) |
| Private reference data, message/CV lookup and storage boundaries | [Reference-data map](reference-data.md) |
| Settings and ownership | [Configuration](configuration.md) |
| System explanation and module contracts | [System map](system-map.md), [architecture](architecture.md) |
| Current queue and release status | Plan index |
| Dated local, deployed and scheduled-run evidence | The linked implementation/release ledger |

Link to an owner rather than copying its instructions into another guide. Keep existing URLs and
headings stable when reorganizing. New pages should link back to the documentation index and to
the next useful task.

## Write and verify a guide

1. Start with the reader's outcome and prerequisites. Use the actual control names or commands.
2. Explain the resulting state and how the reader can verify it. State automation and model limits.
3. Give the recovery path for a failed or partial result. A restart or old receipt is not recovery.
4. Check file links and heading anchors. Rehearse changed commands against synthetic data when
   possible; run the relevant browser journey when documenting a changed interaction.
5. Label screenshots as synthetic fixtures. Keep personal records and authentication material out
   of captures, examples and committed evidence.
6. Record local tests, deployed behavior, scheduled execution and unverified human usability
   separately. Update release status from observed results, never from a successful build alone.

## Keep current guidance separate from history

The plan index holds concise current status and links to dated ledgers. Replace superseded status;
move unique release evidence to the owning ledger or `archive/` instead of appending another narrative.
Archive snapshots describe their date and never override the current queue or runbooks. Keep existing
anchors when shortening a guide; repair moved links. For deliberately retired material, name its Git
revision/path instead of restoring obsolete instructions just to satisfy a link.

Delete duplicated prose and disposable generated artifacts only after checking their owner and
recovery path. Local disk hygiene belongs in [local development](../ops/LOCAL.md#repository-hygiene).
Databases, raw inputs, evidence and backups require a retention decision, not an age-based guess.

## Comparable projects reviewed

The dated 18 October comparison that informed this structure is retained in Git:
`git show 9fc95b7:docs/documentation-style.md`. Popularity counts are not maintenance guidance or
proof of reader usability; the ownership and verification rules above are the current standard.
