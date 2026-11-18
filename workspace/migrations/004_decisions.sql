-- Decisions the owner answers on the site (plans/2026-09-25-decisions-signals-consultancies.md, D6).
-- The agent asks with a declared set of choices (workspace/states.py DECISION_KINDS); the owner answers;
-- the agent processes the answer later and records the outcome. An answer changes no record.
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,                  -- the record id, or workspace:<slug> for a workspace-level decision
    record_id TEXT REFERENCES records(id),  -- NULL for a workspace-level decision
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    recommendation TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    evidence_urls TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',    -- open, answered, done, withdrawn
    answer TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    answered_at TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL DEFAULT '',
    thread_json TEXT NOT NULL DEFAULT '[]', -- every answer and every reply, oldest first
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
-- One live decision per subject and kind; subject is never NULL, so this holds for workspace decisions too.
CREATE UNIQUE INDEX decisions_one_live ON decisions(subject, kind) WHERE status IN ('open', 'answered');
CREATE INDEX decisions_by_status ON decisions(status);
