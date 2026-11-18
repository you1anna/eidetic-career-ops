-- Current human commitments are scoped to the approach, independently of weekly membership.
-- Immutable contact snapshots and state_events keep their history and correction evidence.
CREATE TABLE approach_next_steps (
 record_id TEXT NOT NULL REFERENCES records(id),
 action_id TEXT NOT NULL,
 step_json TEXT NOT NULL,
 context_json TEXT NOT NULL,
 revision INTEGER NOT NULL,
 updated_at TEXT NOT NULL,
 actor TEXT NOT NULL,
 PRIMARY KEY(record_id, action_id)
);
