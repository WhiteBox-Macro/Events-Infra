-- 004_social.sql — social-media posts (Reddit, StockTwits, X/Twitter, …).
--
-- One table, mirroring news.articles. The `source` column tells us which
-- platform the post came from. Engagement metrics live alongside content so
-- the signal layer can weight a high-follower X post differently from a
-- WallStreetBets reply.
--
-- 'twitter' covers X (formerly Twitter) — the platform was renamed but
-- vendor APIs and our ingester both still use the 'twitter' identifier.
-- X requires a paid API tier ($100/mo Basic for ~10k posts/mo; $5k/mo Pro
-- for ~1M posts/mo). The ingester is gated on TWITTER_API_KEY being set.

CREATE SCHEMA IF NOT EXISTS social;

-- ── social.posts ────────────────────────────────────────────────────────────
-- One row per post. UNIQUE(source, external_id) is the dedup key. Body is
-- usually small enough to keep in the column (StockTwits caps ~1k chars,
-- tweets ~280; Reddit selftext can be long — we still inline up to a few KB
-- and skip the disk-spill pattern used for full news bodies).
CREATE TABLE IF NOT EXISTS social.posts (
    post_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source              TEXT NOT NULL,                -- 'reddit' | 'stocktwits' | 'twitter' | 'mastodon'
    external_id         TEXT NOT NULL,                -- platform-native post id (tweet id, reddit fullname, stocktwits id)
    author              TEXT,                          -- handle / username
    author_followers    INTEGER,                       -- captured at post time; influence weighting input
    parent_id           TEXT,                          -- thread-parent for replies (NULL for top-level)
    channel             TEXT,                          -- subreddit name, stocktwits cashtag, X list/topic
    title               TEXT,                          -- Reddit has titles; tweets/stocktwits usually don't
    body                TEXT NOT NULL,                 -- post text (no disk spill — social posts are short)
    url                 TEXT,                          -- canonical link to the post
    posted_at           TIMESTAMPTZ NOT NULL,         -- when the user published it
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    language            TEXT,
    -- Engagement (snapshot at ingest time; refreshed by enrichment if we re-fetch)
    score               INTEGER,                       -- Reddit upvotes, StockTwits likes
    comments            INTEGER,                       -- reply count
    reposts             INTEGER,                       -- retweets / shares
    -- Enrichment columns (mirror news.articles; filled by downstream scripts)
    tickers             TEXT[] NOT NULL DEFAULT '{}', -- cashtags + extracted mentions
    sectors             TEXT[] NOT NULL DEFAULT '{}',
    -- StockTwits ships user-labelled sentiment ('Bullish'/'Bearish'); for X/Reddit
    -- this column is filled by our own classifier.
    sentiment_label     TEXT,                          -- 'bullish' | 'bearish' | 'neutral' | NULL
    sentiment_score     REAL,                          -- -1.0 (bearish) → +1.0 (bullish)
    sentiment_model     TEXT,                          -- 'user_label' | 'haiku-v1' | …
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_posted        ON social.posts (posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_ingested      ON social.posts (ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_source        ON social.posts (source);
CREATE INDEX IF NOT EXISTS idx_posts_channel       ON social.posts (channel);
CREATE INDEX IF NOT EXISTS idx_posts_tickers       ON social.posts USING gin (tickers);
CREATE INDEX IF NOT EXISTS idx_posts_body_fts      ON social.posts USING gin (to_tsvector('english', body));

-- High-influence X accounts move single names in seconds. A partial index over
-- tickers for posts above an influence threshold lets the dispatcher filter
-- cheaply when the firehose is busy.
CREATE INDEX IF NOT EXISTS idx_posts_influencer_tickers
    ON social.posts USING gin (tickers)
    WHERE author_followers IS NOT NULL AND author_followers >= 50000;
