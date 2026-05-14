-- 002_classified.sql
-- Output of the parser-classifier pipeline. One row per classified event.
-- Flat, column-rich, no normalization. Designed for direct consumption
-- by backtesting and modeling agents.
--
-- Downstream agents append their own columns (modeling signals, features,
-- scores) via ALTER TABLE — this migration covers only what the
-- parser-classifier itself produces.
--
-- Column groups:
--   A. Source identity & content
--   B. Classification core
--   C. Market scope & entity resolution
--   D. Scheduled/periodic event fields
--   E. Deduplication & event chains
--   F. Extraction metadata

BEGIN;

CREATE TABLE events.classified (
    event_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_id                  UUID NOT NULL REFERENCES events.raw(raw_id),

    -- ═══ A. SOURCE IDENTITY & CONTENT ════════════════════════════════
    source_channel          TEXT NOT NULL,           -- sec_edgar | fed_rss | alpha_vantage | reddit | twitter | stocktwits | ...
    publish_time            TIMESTAMPTZ NOT NULL,    -- when the content was published
    headline                TEXT,                    -- one-line summary (article title, tweet first line, release title)
    text_content            TEXT,                    -- full cleaned text (HTML stripped, normalized)

    -- ═══ B. CLASSIFICATION CORE ══════════════════════════════════════
    -- is_regular: TRUE = scheduled periodic release (NFP, CPI, FOMC, earnings date)
    --             FALSE = breaking/irregular news (tariff tweet, merger rumor, geopolitical shock)
    is_regular              BOOLEAN NOT NULL,

    -- Free-form but standardized vocabulary. Classifier prompted to use consistent labels.
    -- Examples: earnings_beat, earnings_miss, guidance_raise, guidance_cut,
    --           tariff_new, tariff_change, sanction, rate_hike, rate_cut, rate_hold,
    --           merger_announced, buyback, restructuring, ceo_departure,
    --           conflict_escalation, diplomacy, election_result,
    --           nfp_release, cpi_release, gdp_release, pmi_release
    event_type              TEXT NOT NULL,

    inferred_tone           TEXT NOT NULL,           -- bullish | bearish | neutral | mixed
    inferred_magnitude      TEXT NOT NULL,           -- major | moderate | minor

    -- ═══ C. MARKET SCOPE & ENTITY RESOLUTION ════════════════════════
    -- Which markets move on this event? Array for multi-market events.
    -- Vocabulary: US_EQUITY, US_FI, EU_EQUITY, EU_FI, COMMODITY, FX, CRYPTO, EM
    inferred_impact_markets TEXT[] NOT NULL DEFAULT '{}',

    -- Affected tickers, sectors, countries — arrays, not normalized tables.
    -- Parser fills tickers via regex + stock_os.securities lookup;
    -- classifier fills sectors/countries via LLM inference.
    tickers                 TEXT[] NOT NULL DEFAULT '{}',
    primary_ticker          TEXT,                    -- single most affected ticker (nullable)
    sectors                 TEXT[] NOT NULL DEFAULT '{}',   -- semiconductor, banking, energy, pharma, ...
    primary_sector          TEXT,                    -- single most affected sector (nullable)
    countries               TEXT[] NOT NULL DEFAULT '{}',   -- US, CN, EU, JP, ...

    -- ═══ D. SCHEDULED/PERIODIC EVENT FIELDS ══════════════════════════
    -- Only populated when is_regular = TRUE. NULL for irregular events.
    -- For backtesting: the signal is surprise = actual - consensus, not the level.
    indicator_name          TEXT,                    -- CPI, NFP, FOMC, EARNINGS, PMI, ...
    scheduled_time          TIMESTAMPTZ,            -- when the release was expected (may differ from publish_time)
    consensus_value         NUMERIC,                -- market consensus / estimate before release
    actual_value            NUMERIC,                -- actual released value
    surprise                NUMERIC,                -- actual - consensus (raw)
    surprise_z              REAL,                   -- z-scored surprise (normalized across history)
    reporting_period        TEXT,                    -- "2026-Q1", "2026-04", "2026" — what period the data covers

    -- ═══ E. DEDUPLICATION & EVENT CHAINS ═════════════════════════════
    -- Same underlying event reported by Reuters, CNBC, Reddit, Twitter → one cluster.
    -- Backtest should count the cluster once, not N times.
    dedup_cluster_id        UUID,                   -- groups articles about same event
    cluster_sequence        SMALLINT,               -- order within cluster (1 = first report)

    -- Event chains: "tariff proposed" → "tariff signed" → "retaliatory tariff"
    related_event_id        UUID REFERENCES events.classified(event_id),

    -- ═══ F. EXTRACTION METADATA ══════════════════════════════════════
    classified_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    classified_by           TEXT NOT NULL,           -- model name + version (e.g., "haiku-4.5/v1.2")
    classification_confidence REAL,                  -- overall confidence 0-1
    metadata                JSONB NOT NULL DEFAULT '{}'   -- overflow for source-specific fields
);

-- ── Indexes ──────────────────────────────────────────────────────────

CREATE INDEX idx_classified_publish_time ON events.classified (publish_time);
CREATE INDEX idx_classified_event_type ON events.classified (event_type);
CREATE INDEX idx_classified_is_regular ON events.classified (is_regular);
CREATE INDEX idx_classified_tickers ON events.classified USING gin (tickers);
CREATE INDEX idx_classified_primary_ticker ON events.classified (primary_ticker) WHERE primary_ticker IS NOT NULL;
CREATE INDEX idx_classified_markets ON events.classified USING gin (inferred_impact_markets);
CREATE INDEX idx_classified_sectors ON events.classified USING gin (sectors);
CREATE INDEX idx_classified_dedup_cluster ON events.classified (dedup_cluster_id) WHERE dedup_cluster_id IS NOT NULL;
CREATE INDEX idx_classified_tone ON events.classified (inferred_tone);
CREATE INDEX idx_classified_related ON events.classified (related_event_id) WHERE related_event_id IS NOT NULL;

COMMIT;
