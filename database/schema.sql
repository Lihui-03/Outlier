-- Generic key-value cache for nba_api responses. We serialize DataFrames
-- to JSON (records-orient) so the cache stays simple and debuggable.

CREATE TABLE IF NOT EXISTS cache_entries (
    namespace   TEXT NOT NULL,    -- e.g. 'gamelog', 'players', 'defense'
    cache_key   TEXT NOT NULL,    -- unique within namespace, e.g. player_id + season
    payload     TEXT NOT NULL,    -- JSON-serialized content
    created_at  INTEGER NOT NULL, -- unix seconds
    PRIMARY KEY (namespace, cache_key)
);

CREATE INDEX IF NOT EXISTS idx_cache_created_at ON cache_entries (created_at);
