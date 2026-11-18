-- Operational accounting/cache only. Canonical records still use records/import_research.
CREATE TABLE codex_invocations (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, lane TEXT NOT NULL,
 started_at TEXT NOT NULL, day TEXT NOT NULL, week TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'unknown', usage_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX codex_invocations_period ON codex_invocations(week,day,lane);
CREATE TABLE research_cache (
 target TEXT NOT NULL, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
 value_json TEXT NOT NULL, expires_at TEXT NOT NULL,
 PRIMARY KEY(target,kind)
);
