-- "Ask Claude…" on a card: the owner's question about a record and the agent's answer.
-- the owner asks on the site; an agent answers through wo at its next session. Neither changes the record.
CREATE TABLE questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES records(id),
    question TEXT NOT NULL,
    asked_at TEXT NOT NULL,
    asked_by TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    answered_at TEXT NOT NULL DEFAULT '',
    answered_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX questions_record ON questions(record_id,asked_at);
