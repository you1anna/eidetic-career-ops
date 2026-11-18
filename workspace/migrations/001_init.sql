-- work-ops canonical store. One row per career record, keyed by the existing IDs
-- (CP-###, AG-###, CA-YYYYMMDD-###/PL-<slug>, Job###/SCAN-<sha1>, CE-###, CS-###, CO-<slug>).
-- The first block of columns is the external CSV contract (COLUMNS in tools/export_directory.py),
-- stored verbatim so the export is a straight read. Everything after it is state and provenance.

CREATE TABLE records (
    id                 TEXT PRIMARY KEY,
    type               TEXT NOT NULL,
    name               TEXT NOT NULL DEFAULT '',
    organisation       TEXT NOT NULL DEFAULT '',
    role               TEXT NOT NULL DEFAULT '',
    subtype            TEXT NOT NULL DEFAULT '',
    engagement         TEXT NOT NULL DEFAULT '',
    location           TEXT NOT NULL DEFAULT '',
    workplace          TEXT NOT NULL DEFAULT '',
    url                TEXT NOT NULL DEFAULT '',
    contact            TEXT NOT NULL DEFAULT '',
    pay                TEXT NOT NULL DEFAULT '',
    pay_min            TEXT NOT NULL DEFAULT '',
    pay_max            TEXT NOT NULL DEFAULT '',
    pay_currency       TEXT NOT NULL DEFAULT '',
    pay_period         TEXT NOT NULL DEFAULT '',
    ir35               TEXT NOT NULL DEFAULT '',
    date               TEXT NOT NULL DEFAULT '',
    last_touch         TEXT NOT NULL DEFAULT '',
    next_action        TEXT NOT NULL DEFAULT '',
    verification       TEXT NOT NULL DEFAULT '',
    related_ids        TEXT NOT NULL DEFAULT '',
    notes              TEXT NOT NULL DEFAULT '',
    source_file        TEXT NOT NULL DEFAULT '',
    verification_level TEXT NOT NULL DEFAULT '',
    record_url         TEXT NOT NULL DEFAULT '',
    detail_url         TEXT NOT NULL DEFAULT '',
    apply_url          TEXT NOT NULL DEFAULT '',
    evidence_urls      TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    email              TEXT NOT NULL DEFAULT '',
    phone              TEXT NOT NULL DEFAULT '',
    related_names      TEXT NOT NULL DEFAULT '',
    -- the exact original status cell; the CSV `status` column is written back from this
    status_raw         TEXT NOT NULL DEFAULT '',
    -- declared state, NULL wherever the source does not say (workspace/states.py)
    status             TEXT,
    stage              TEXT,
    sub_state          TEXT,
    owner              TEXT,
    blocked_on         TEXT,
    kind               TEXT,
    relationship       TEXT,
    triage_reason      TEXT,
    fields_json        TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    archived_at        TEXT
);
CREATE INDEX records_type ON records (type);

CREATE TABLE links (
    from_id  TEXT NOT NULL,
    to_id    TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'related',
    PRIMARY KEY (from_id, to_id, relation),
    FOREIGN KEY (from_id) REFERENCES records (id) ON DELETE CASCADE
);
CREATE INDEX links_to ON links (to_id);

-- One follow-up per record. `status_raw` keeps the hub's combined cell ("Open — OWNER") so the
-- workspace and the CSV read back exactly what was written. `source` marks precedence:
-- wo (a WO: row or a UI save) overrides pipeline, which overrides report.
CREATE TABLE followups (
    record_id  TEXT PRIMARY KEY,
    action     TEXT NOT NULL DEFAULT '',
    owner      TEXT,
    due        TEXT NOT NULL DEFAULT '',
    status     TEXT,
    status_raw TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'wo',
    seq        INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE interactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    channel      TEXT NOT NULL,
    direction    TEXT NOT NULL,
    person_id    TEXT,
    org_id       TEXT,
    record_id    TEXT,
    summary      TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT,
    source       TEXT NOT NULL DEFAULT 'manual',
    created_at   TEXT NOT NULL
);
CREATE INDEX interactions_person ON interactions (person_id);
CREATE INDEX interactions_record ON interactions (record_id);

CREATE TABLE state_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    from_value TEXT,
    to_value   TEXT,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT 'agent',
    note       TEXT
);
CREATE INDEX state_events_entity ON state_events (entity_id);

-- Nothing becomes a record until the owner confirms it.
CREATE TABLE review_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolution  TEXT
);

CREATE TABLE scan_runs (
    run_id       TEXT PRIMARY KEY,
    date         TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE drafts (
    record_id TEXT PRIMARY KEY,
    title     TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE import_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    command    TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    report_json TEXT NOT NULL
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE schema_version (
    version   INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
