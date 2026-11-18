-- A recruiter's same profile can be submitted to different client companies within one
-- approach. Keep duplicate protection per destination without rewriting existing evidence.
DROP INDEX outcomes_draft_action;
CREATE UNIQUE INDEX outcomes_draft_action ON outcomes(record_id,draft_version_id,kind,
    COALESCE(json_extract(snapshot_json,'$.action_id'),''),
    COALESCE(json_extract(snapshot_json,'$.target.name'),''))
    WHERE draft_version_id IS NOT NULL AND corrected_at IS NULL;
