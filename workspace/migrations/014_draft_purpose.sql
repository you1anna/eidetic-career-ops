-- A draft is either an approval note (the existing decision) or a working message on a card.
-- Existing rows stay approval notes. Versions stay immutable; purpose is set when the version is saved.
ALTER TABLE drafts ADD COLUMN purpose TEXT NOT NULL DEFAULT 'approval';
ALTER TABLE draft_versions ADD COLUMN purpose TEXT NOT NULL DEFAULT 'approval';
