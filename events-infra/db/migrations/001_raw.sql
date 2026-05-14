-- 001_raw.sql
-- Datalake index. One row per raw file ingested from any source.
-- The raw JSON on disk is immutable; this table is the lookup catalog.

BEGIN;

CREATE SCHEMA IF NOT EXISTS events;

CREATE TABLE events.raw (
    raw_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- source identity
    source_type     TEXT NOT NULL,           -- rss | api | social | macro
    source_channel  TEXT NOT NULL,           -- sec_edgar_8k | fed_press | alpha_vantage | reddit_wsb | twitter | ...
    external_id     TEXT NOT NULL,           -- dedup key from source (feed GUID, post ID, article ID)

    -- datalake pointer
    file_path       TEXT NOT NULL,           -- relative to $DB_BASE/events/raw/
    file_hash       TEXT,                    -- SHA-256 of raw JSON
    file_size_bytes INTEGER,

    -- timestamps
    published_at    TIMESTAMPTZ,            -- when source says it was published (NULL if absent)
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- pipeline state
    classify_status TEXT NOT NULL DEFAULT 'pending',  -- pending | classified | failed | skipped
    classified_at   TIMESTAMPTZ,

    -- overflow
    metadata        JSONB NOT NULL DEFAULT '{}',

    UNIQUE (source_channel, external_id)
);

CREATE INDEX idx_raw_classify_pending ON events.raw (classify_status) WHERE classify_status = 'pending';
CREATE INDEX idx_raw_source ON events.raw (source_type, source_channel);
CREATE INDEX idx_raw_published ON events.raw (published_at);

COMMIT;
