-- Integration receipts commit with evidence, never independently of it. Caller is supplied by
-- the trusted adapter, not by a finding. Keep receipts for as long as clients may retry requests.
CREATE TABLE research_receipts (
    caller TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_sha TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (caller, request_id)
);
INSERT INTO meta(key,value) VALUES('research_bridge_generation',lower(hex(randomblob(16))));
