-- Keep import provenance with the same history used by human and agent mutations.
ALTER TABLE state_events ADD COLUMN source_scope TEXT NOT NULL DEFAULT '';
ALTER TABLE state_events ADD COLUMN run_id TEXT NOT NULL DEFAULT '';
ALTER TABLE state_events ADD COLUMN changes_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE state_events ADD COLUMN recorded_at TEXT;
CREATE INDEX state_events_recorded ON state_events(recorded_at, id);
CREATE INDEX state_events_run ON state_events(run_id);
INSERT INTO meta(key,value) VALUES('activity_tracking_since',strftime('%Y-%m-%dT%H:%M:%SZ','now'));
