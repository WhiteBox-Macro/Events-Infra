-- 002_news.sql — news ingestion schema (sources + articles).
--
-- Two-table minimum to start:
--   news.sources   — registry of feeds we poll
--   news.articles  — one row per ingested article; full body lives on disk
--                    (body_path), DB holds metadata + extraction outputs.
--
-- Enrichment columns (tickers, sectors, sentiment_score, embedding) are
-- nullable and filled by downstream enrichers in scripts/enrichment/.
-- This separation lets ingestion and enrichment evolve independently.

CREATE SCHEMA IF NOT EXISTS news;

-- ── news.sources ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news.sources (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,        -- e.g. 'reuters_business', 'fed_press_releases'
    publisher           TEXT,                         -- e.g. 'Reuters', 'Federal Reserve'
    feed_type           TEXT NOT NULL,                -- 'rss' | 'atom' | 'api' | 'scrape'
    feed_url            TEXT NOT NULL,
    category            TEXT,                         -- 'business' | 'macro' | 'regulatory' | 'political' | 'social'
    language            TEXT DEFAULT 'en',
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    poll_interval_sec   INTEGER NOT NULL DEFAULT 60,
    last_polled_at      TIMESTAMPTZ,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sources_active     ON news.sources (active) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_sources_category   ON news.sources (category);

DROP TRIGGER IF EXISTS trg_news_sources_updated_at ON news.sources;
CREATE TRIGGER trg_news_sources_updated_at
    BEFORE UPDATE ON news.sources
    FOR EACH ROW EXECUTE FUNCTION signals_meta.touch_updated_at();

-- ── news.articles ───────────────────────────────────────────────────────────
-- One row per article. UNIQUE(source_id, external_id) is the dedup key for
-- pollers — feed-provided GUID or hash of URL when feed has no GUID.
CREATE TABLE IF NOT EXISTS news.articles (
    article_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           INTEGER NOT NULL REFERENCES news.sources(id),
    external_id         TEXT NOT NULL,                -- feed GUID or sha256(url)
    url                 TEXT NOT NULL,
    title               TEXT NOT NULL,
    summary             TEXT,                         -- short description from feed
    author              TEXT,
    body_path           TEXT,                         -- on-disk path to full body, NULL until fetched
    body_fetched_at     TIMESTAMPTZ,
    published_at        TIMESTAMPTZ,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    language            TEXT,
    -- Enrichment (filled by downstream scripts; NULL until processed)
    tickers             TEXT[] NOT NULL DEFAULT '{}', -- extracted ticker mentions
    sectors             TEXT[] NOT NULL DEFAULT '{}', -- extracted sector tags
    categories          TEXT[] NOT NULL DEFAULT '{}', -- topic labels
    sentiment_score     REAL,                          -- -1.0 (bearish) → +1.0 (bullish)
    sentiment_model     TEXT,                          -- which model produced it
    -- Escape hatch — mirrors AOTC-DB convention of JSONB metadata everywhere
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_articles_published   ON news.articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_ingested    ON news.articles (ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source      ON news.articles (source_id);
CREATE INDEX IF NOT EXISTS idx_articles_tickers     ON news.articles USING gin (tickers);
CREATE INDEX IF NOT EXISTS idx_articles_sectors     ON news.articles USING gin (sectors);
CREATE INDEX IF NOT EXISTS idx_articles_title_fts   ON news.articles USING gin (to_tsvector('english', title));

-- ── Seed sources (commented out — uncomment + edit before running, or
--    insert via a separate script. Kept here as a worked example.) ──
-- INSERT INTO news.sources (name, publisher, feed_type, feed_url, category) VALUES
--   ('reuters_business',  'Reuters',             'rss', 'https://feeds.reuters.com/reuters/businessNews',                     'business'),
--   ('fed_press',         'Federal Reserve',     'rss', 'https://www.federalreserve.gov/feeds/press_all.xml',                 'macro'),
--   ('treasury_press',    'US Treasury',         'rss', 'https://home.treasury.gov/news/press-releases/feed',                 'macro'),
--   ('sec_press',         'SEC',                 'rss', 'https://www.sec.gov/news/pressreleases.rss',                          'regulatory')
-- ON CONFLICT (name) DO NOTHING;
