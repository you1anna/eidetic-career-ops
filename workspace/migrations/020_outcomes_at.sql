-- Outcomes by date, for the Dashboard's last-30-days counts: the existing outcomes_record index
-- starts with record_id, so a date-only filter scanned every outcome ever recorded.
CREATE INDEX outcomes_at ON outcomes(at, id);
