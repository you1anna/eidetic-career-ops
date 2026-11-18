-- Preserve each new draft exactly. Imported legacy text is only the snapshot visible at migration;
-- it cannot prove which text an earlier approval referred to.
CREATE TABLE draft_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES records(id),
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    legacy_snapshot INTEGER NOT NULL DEFAULT 0 CHECK(legacy_snapshot IN (0,1))
);
CREATE INDEX draft_versions_record ON draft_versions(record_id,id);
CREATE TRIGGER draft_versions_immutable BEFORE UPDATE ON draft_versions
BEGIN
    SELECT RAISE(ABORT,'Draft versions are immutable; save a new version');
END;
ALTER TABLE drafts ADD COLUMN version_id INTEGER REFERENCES draft_versions(id);
INSERT INTO draft_versions(record_id,title,text,created_at,actor,legacy_snapshot)
    SELECT record_id,title,text,strftime('%Y-%m-%dT%H:%M:%SZ','now'),'migration',1 FROM drafts;
UPDATE drafts SET version_id=(SELECT id FROM draft_versions WHERE draft_versions.record_id=drafts.record_id);

ALTER TABLE decisions ADD COLUMN draft_version_id INTEGER REFERENCES draft_versions(id);
ALTER TABLE decisions ADD COLUMN draft_binding TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN execution_status TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN answer_revision INTEGER;
ALTER TABLE decisions ADD COLUMN claimed_answer_revision INTEGER;
ALTER TABLE decisions ADD COLUMN claimed_by TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN claimed_at TEXT NOT NULL DEFAULT '';
UPDATE decisions SET draft_version_id=(SELECT version_id FROM drafts WHERE drafts.record_id=decisions.record_id),
    draft_binding=CASE WHEN EXISTS(SELECT 1 FROM drafts WHERE drafts.record_id=decisions.record_id) THEN 'legacy' ELSE 'missing' END
    WHERE kind='draft';
UPDATE decisions SET execution_status=CASE status WHEN 'answered' THEN 'queued' WHEN 'done' THEN 'completed' ELSE '' END,
    answer_revision=CASE WHEN answer<>'' THEN revision END;

CREATE TABLE outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES records(id),
    kind TEXT NOT NULL CHECK(kind IN ('sent','replied','introduction','conversation','proposal','won','no_fit','no_budget','permanent_only')),
    at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    draft_version_id INTEGER REFERENCES draft_versions(id),
    actor TEXT NOT NULL,
    interaction_id INTEGER REFERENCES interactions(id),
    snapshot_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TRIGGER outcomes_snapshot_immutable BEFORE UPDATE OF snapshot_json ON outcomes
BEGIN
    SELECT RAISE(ABORT,'Outcome provenance is immutable');
END;
CREATE INDEX outcomes_record ON outcomes(record_id,at,id);
-- A person may use the same note in different company contexts. Only repeated clicks within
-- the same recommended action (or the same unscoped manual action) are one factual event.
CREATE UNIQUE INDEX outcomes_draft_action ON outcomes(record_id,draft_version_id,kind,
    COALESCE(json_extract(snapshot_json,'$.action_id'),'')) WHERE draft_version_id IS NOT NULL;
