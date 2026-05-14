-- 007_social_handles.sql — curated social-handle registry.
--
-- Why: the cashtag-driven Twitter ingester only catches posts that tag a
-- watchlist ticker. For event-driven trading on things like a Trump tweet
-- about a war, an Elon tweet about a product, or a Fed governor on rates,
-- we need to ingest the author regardless of cashtag presence, then have
-- the agent infer which sectors / tickers will move.
--
-- This table is the curator-edited list of accounts we always pull, with
-- a per-handle impact_weight that bumps the fast-signal score so the
-- system reacts hard to a known mover even on a tweet of average tone.

CREATE SCHEMA IF NOT EXISTS social;  -- idempotent; created by 004_social.sql

CREATE TABLE IF NOT EXISTS social.handles (
    handle_id             SERIAL PRIMARY KEY,
    platform              TEXT NOT NULL,           -- 'twitter' | 'reddit' | 'mastodon' | ...
    username              TEXT NOT NULL,           -- platform handle without leading @
    user_id               TEXT,                     -- platform-native id (Twitter v2 numeric id, resolved on first lookup)
    display_name          TEXT,
    -- Curator labels:
    category              TEXT,                     -- 'ceo' | 'politician' | 'agency' | 'journalist' | 'macro_pundit' | 'analyst'
    tags                  TEXT[] NOT NULL DEFAULT '{}',  -- e.g. {us_politics, tech, aerospace}
    expected_themes       TEXT[] NOT NULL DEFAULT '{}',  -- hints the LLM uses ('tariffs', 'monetary_policy', 'energy', ...)
    impact_weight         NUMERIC(4,2) NOT NULL DEFAULT 1.0,  -- multiplied into fast-signal score
    -- Polling state:
    active                BOOLEAN NOT NULL DEFAULT TRUE,
    poll_interval_sec     INTEGER NOT NULL DEFAULT 60,
    last_polled_at        TIMESTAMPTZ,
    last_seen_external_id TEXT,                     -- since_id high-water mark, per handle
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (platform, username)
);

CREATE INDEX IF NOT EXISTS idx_handles_active        ON social.handles (active) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_handles_platform_user ON social.handles (platform, username);
CREATE INDEX IF NOT EXISTS idx_handles_category      ON social.handles (category);

DROP TRIGGER IF EXISTS trg_social_handles_updated_at ON social.handles;
CREATE TRIGGER trg_social_handles_updated_at
    BEFORE UPDATE ON social.handles
    FOR EACH ROW EXECUTE FUNCTION signals_meta.touch_updated_at();
