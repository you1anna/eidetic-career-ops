-- Attended private evidence. Original documents are never part of public sources or state exports.
CREATE TABLE incoming_receipts (
 request_id TEXT PRIMARY KEY, payload_sha TEXT NOT NULL, identity_sha TEXT NOT NULL,
 record_id TEXT NOT NULL REFERENCES records(id), payload_json TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE INDEX incoming_identity ON incoming_receipts(identity_sha);
CREATE TABLE record_documents (
 id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES records(id), filename TEXT NOT NULL,
 kind TEXT NOT NULL, mime TEXT NOT NULL, sha TEXT NOT NULL, content_base64 TEXT NOT NULL,
 extracted_text TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(record_id,sha)
);
