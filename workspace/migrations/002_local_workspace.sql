ALTER TABLE records ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE records ADD COLUMN working_state TEXT NOT NULL DEFAULT 'needs_review';
ALTER TABLE records ADD COLUMN status_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE records ADD COLUMN waiting_on TEXT NOT NULL DEFAULT '';
ALTER TABLE records ADD COLUMN known_context TEXT NOT NULL DEFAULT '';
ALTER TABLE records ADD COLUMN source_lifecycle TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE records ADD COLUMN published_deadline TEXT NOT NULL DEFAULT '';
ALTER TABLE links ADD COLUMN notes TEXT NOT NULL DEFAULT '';
ALTER TABLE followups ADD COLUMN reason TEXT NOT NULL DEFAULT '';
ALTER TABLE followups ADD COLUMN waiting_on TEXT NOT NULL DEFAULT '';
ALTER TABLE review_queue ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE review_queue ADD COLUMN dedupe_key TEXT;
CREATE UNIQUE INDEX review_dedupe ON review_queue(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE TABLE legacy_followup_dates (
    record_id TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL, archived_at TEXT NOT NULL,
    PRIMARY KEY(record_id, value, source)
);
INSERT OR IGNORE INTO legacy_followup_dates
    SELECT record_id, due, source, updated_at FROM followups WHERE due NOT IN ('', 'None');
UPDATE followups SET due = '', status = CASE WHEN status = 'done' THEN 'done' ELSE 'needs_review' END;
UPDATE records SET working_state = COALESCE((SELECT status FROM followups WHERE record_id=records.id), 'needs_review');
CREATE TABLE record_edits (
    record_id TEXT NOT NULL REFERENCES records(id), field TEXT NOT NULL,
    PRIMARY KEY(record_id, field)
);
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL REFERENCES records(id),
    source TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL,
    UNIQUE(record_id, source, observed_at, payload_json)
);
CREATE TABLE source_records (
    source_scope TEXT NOT NULL, source_id TEXT NOT NULL, record_id TEXT NOT NULL REFERENCES records(id),
    PRIMARY KEY(source_scope, source_id)
);
CREATE TABLE source_health (
    scope TEXT PRIMARY KEY, provider TEXT NOT NULL, state TEXT NOT NULL, observed_at TEXT NOT NULL,
    last_success TEXT, item_count INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
);
