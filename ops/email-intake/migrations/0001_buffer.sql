CREATE TABLE IF NOT EXISTS mail (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_id TEXT NOT NULL UNIQUE,
  received_at TEXT NOT NULL,
  envelope_from TEXT NOT NULL,
  raw_base64 TEXT,
  bytes INTEGER NOT NULL,
  acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS pending_mail ON mail(acknowledged_at, sequence);
