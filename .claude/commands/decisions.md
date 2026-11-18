---
description: Process the owner's answered decisions from the work-ops site
---

The owner has answered decisions on https://work.example.com. Process them now, before other work.
$ARGUMENTS

This needs a local session. A cloud session has no route to the VPS: say so and stop.

CLAUDE.md also sends every agent here for the mechanics. Read that way, without `/decisions`,
apply these steps only to the answers relevant to your current task; only `/decisions` selects the
whole queue.

1. `ops/wo-live decision list` (answered first). For anything unclear,
   `ops/wo-live decision show <id>` and `ops/wo-live show <record-id>`.
2. Read its current revision from `decision list` or `decision show`. Before a consequential
   approved action, claim the exact answered revision: `ops/wo-live decision claim <id> "<work being started>" --revision N`.
   A claim freezes that answer until you reply or complete it; re-read after each change.
   If execution cannot proceed, use `decision block <id> "<reason>" --revision N`.
   For each answered decision, do what its declared answer says (`workspace/states.py`
   `DECISION_KINDS`), through `ops/wo-live` (always `--actor agent`): `transition`, `edit`,
   `followup`, `draft`, `link`, `archive`. Read the owner's free-text note too: it may add facts
   (for example, what he entered on a platform).
3. Close each one through `ops/wo-live`: `decision done <id> "<what was done>" --revision N`,
   `decision reply <id> "<question>" --revision N` when you need more from him, or
   `decision withdraw <id> "<why>" --revision N` when the facts changed. Use the latest revision;
   a stale answer must be re-read, never silently retried. Draft approval binds immutable wording;
   replacing a note requires a new approval.
4. `done` and `withdraw` end the record's waiting step, so give it a new next step:
   `followup <id> --action … --status … --reason … --waiting-on …` (waiting and blocked need both).
5. A `failure` answer: **Fix it** means find the cause and fix it (a code change is committed,
   pushed and deployed with `ops/vps/deploy.sh --local-approved`); record progress with
   `ops/wo-live decision note <id> "<progress>" --stage investigating|fix_attempted|awaiting_verification|blocked
   --owner agent|owner|provider --next-step …`, which never changes the answer and takes no
   `--revision` but increments it, so re-read before `done`. Only a later complete `ok`/`empty`
   receipt proves recovery, not the note or a passing test (`tools/failure_tasks.py`).
   **Leave it for now** means `done`. **Stop this source** means `enabled: false` in
   `config/portals.yml`, deploy, then `done`.

`outcome <record> <kind> --revision N [--note …]` records only an event the owner reports; it sends
nothing. A build decision answered before the 9 October approval boundary keeps its history and is
completed once its acceptance evidence exists.

Never answer a decision yourself. Report one line per decision: what it asks (its number after the
words), the answer and what you did.
