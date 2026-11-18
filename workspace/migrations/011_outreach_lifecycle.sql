-- Outreach focus (archive/plans/2026-10-14-outreach-focus.md). Follow-ups, parking, referrals, CV
-- submission and handover are recorded facts; follow-up timing itself is derived, never stored.
-- A mistaken entry is marked corrected and stays in the history; nothing is deleted.
CREATE TABLE outcomes_v11 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES records(id),
    kind TEXT NOT NULL CHECK(kind IN ('sent','followed_up','replied','introduction','referral','submitted',
        'conversation','proposal','won','no_fit','no_budget','permanent_only','no_response','handed_over')),
    at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    draft_version_id INTEGER REFERENCES draft_versions(id),
    actor TEXT NOT NULL,
    interaction_id INTEGER REFERENCES interactions(id),
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    corrected_at TEXT,
    corrected_by TEXT,
    correction_note TEXT NOT NULL DEFAULT ''
);
INSERT INTO outcomes_v11(id,record_id,kind,at,note,draft_version_id,actor,interaction_id,snapshot_json)
    SELECT id,record_id,kind,at,note,draft_version_id,actor,interaction_id,snapshot_json FROM outcomes;
DROP TABLE outcomes;
ALTER TABLE outcomes_v11 RENAME TO outcomes;
CREATE TRIGGER outcomes_snapshot_immutable BEFORE UPDATE OF snapshot_json ON outcomes
BEGIN
    SELECT RAISE(ABORT,'Outcome provenance is immutable');
END;
CREATE INDEX outcomes_record ON outcomes(record_id,at,id);
-- As before: repeated clicks on the same note within one approach are one event. A corrected
-- entry no longer counts, so the fact can be recorded again.
CREATE UNIQUE INDEX outcomes_draft_action ON outcomes(record_id,draft_version_id,kind,
    COALESCE(json_extract(snapshot_json,'$.action_id'),'')) WHERE draft_version_id IS NOT NULL AND corrected_at IS NULL;

-- The week's list: fixed when first composed, changed only by the owner's deferral or an explained
-- replacement. Monday of the week in Europe/London.
CREATE TABLE week_items (
    week TEXT NOT NULL,
    action_id TEXT NOT NULL,
    record_id TEXT NOT NULL REFERENCES records(id),
    slot TEXT NOT NULL CHECK(slot IN ('known','contract','prospect')),
    position INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('planned','deferred')),
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    PRIMARY KEY (week, action_id)
);
CREATE INDEX week_items_week ON week_items(week,position);
