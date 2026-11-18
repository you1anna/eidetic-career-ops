-- Columns and a table only the retired Markdown hub import wrote. related_ids/related_names duplicated
-- the links table (the CSV derives both from links); followups.due and legacy_followup_dates held the
-- retired due-date workflow. The git tag pre-cleanup-2026-09 keeps their last values.
ALTER TABLE records DROP COLUMN related_ids;
ALTER TABLE records DROP COLUMN related_names;
ALTER TABLE followups DROP COLUMN due;
DROP TABLE legacy_followup_dates;
