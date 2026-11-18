-- How the owner reaches each person on his list (archive/plans/2026-10-14-outreach-focus.md, Release 2).
-- Values are declared in workspace/states.py ENUMS["person"] and checked by records.edit, so a new
-- channel or kind of ask is a one-line change there, not another migration. Blank means not stated.
ALTER TABLE records ADD COLUMN preferred_channel TEXT DEFAULT NULL;
ALTER TABLE records ADD COLUMN preferred_ask TEXT DEFAULT NULL;
-- the owner's own order: 'first' (contact first) or 'next'. Blank means the person is not on his list.
ALTER TABLE records ADD COLUMN priority TEXT DEFAULT NULL;
