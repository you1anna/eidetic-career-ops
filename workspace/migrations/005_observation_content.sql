-- Scanner observations are kept once per content (db.observation_sha): the daily run re-sees most
-- adverts unchanged, and a row per sighting had doubled the table in four days. The old UNIQUE
-- constraint indexed the whole payload; the content key replaces it. Other sources keep one row per
-- sighting, exactly as before. The latest sighting of each scanner content is kept, so the import's
-- last-seen watermark is unchanged; lifecycle changes are already in state_events.
CREATE TABLE observations_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES records(id),
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha TEXT NOT NULL
);
INSERT INTO observations_new (id, record_id, source, observed_at, payload_json, payload_sha)
    SELECT id, record_id, source, observed_at, payload_json, wo_observation_sha(source, payload_json)
    FROM observations;
DELETE FROM observations_new
 WHERE (source GLOB 'board:*' OR source GLOB 'feed:*' OR source GLOB 'hn:*' OR source GLOB 'tender:*')
   AND id NOT IN (
       SELECT id FROM (
           SELECT id, ROW_NUMBER() OVER (PARTITION BY record_id, source, payload_sha
                                         ORDER BY observed_at DESC, id DESC) AS n
             FROM observations_new
            WHERE (source GLOB 'board:*' OR source GLOB 'feed:*' OR source GLOB 'hn:*' OR source GLOB 'tender:*'))
        WHERE n = 1);
DROP TABLE observations;
ALTER TABLE observations_new RENAME TO observations;
CREATE UNIQUE INDEX observations_content ON observations (record_id, source, payload_sha)
    WHERE (source GLOB 'board:*' OR source GLOB 'feed:*' OR source GLOB 'hn:*' OR source GLOB 'tender:*');
CREATE UNIQUE INDEX observations_sighting ON observations (record_id, source, observed_at, payload_sha)
    WHERE NOT (source GLOB 'board:*' OR source GLOB 'feed:*' OR source GLOB 'hn:*' OR source GLOB 'tender:*');
CREATE INDEX observations_record ON observations (record_id, source);
