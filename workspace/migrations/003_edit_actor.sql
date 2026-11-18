-- Record who made each protected edit. Imports still never overwrite a marked field; the actor keeps
-- the owner's decisions distinguishable from agent corrections.
ALTER TABLE record_edits ADD COLUMN actor TEXT NOT NULL DEFAULT 'owner';
-- Agent corrections made on 23 September 2026, before this column existed (plans/2026-09-23-relevance-and-quality.md).
UPDATE record_edits SET actor = 'agent'
 WHERE record_id IN ('CA-20260913-007','CA-20260913-010','Job005','Job007','CP-708437f6e973','CP-75c45fb46127');
