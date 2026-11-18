-- One durable ledger for model spend, including reservations for ambiguous/crashed requests.
CREATE TABLE research_requests (
    request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reserved_microusd INTEGER NOT NULL CHECK(reserved_microusd > 0),
    charged_microusd INTEGER CHECK(charged_microusd >= 0),
    status TEXT NOT NULL DEFAULT 'reserved' CHECK(status IN ('reserved','settled','overrun')),
    usage_json TEXT
);
CREATE INDEX research_requests_created ON research_requests(created_at);
