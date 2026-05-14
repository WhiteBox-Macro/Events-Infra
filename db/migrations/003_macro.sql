-- 003_macro.sql — macro indicator releases (CPI, NFP, Fed funds, GDP, ...).
--
-- Two tables, mirroring news/:
--   macro.indicators — registry of series we track (one row per CPI, UNRATE, etc.)
--   macro.releases   — one row per published value for an indicator and period.
--
-- Macro events drive every ticker, so we deliberately do NOT tag releases with
-- tickers here — that mapping lives in the dispatcher / signal layer where it
-- can depend on regime, sector exposure, etc.
--
-- "Surprise" is the value-vs-consensus delta in standardised form; downstream
-- signals key off this rather than the raw value, since markets price the
-- consensus before release.

CREATE SCHEMA IF NOT EXISTS macro;

-- ── macro.indicators ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS macro.indicators (
    id              SERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,         -- e.g. 'CPI', 'UNRATE', 'FEDFUNDS', 'GDPC1', 'PAYEMS'
    name            TEXT NOT NULL,                -- human-readable label
    source          TEXT NOT NULL,                -- 'fred' | 'alpha_vantage' | 'treasury' | 'bls' | 'bea'
    source_series   TEXT,                          -- vendor-native series id (FRED 'CPIAUCSL', etc.)
    units           TEXT,                          -- 'index_1982_84_100', 'percent', 'thousands_of_persons', ...
    frequency       TEXT,                          -- 'daily' | 'weekly' | 'monthly' | 'quarterly'
    importance      SMALLINT NOT NULL DEFAULT 3,   -- 1 (low) – 5 (top-tier market mover, e.g. NFP, CPI)
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_indicators_active     ON macro.indicators (active) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_indicators_importance ON macro.indicators (importance DESC);

DROP TRIGGER IF EXISTS trg_macro_indicators_updated_at ON macro.indicators;
CREATE TRIGGER trg_macro_indicators_updated_at
    BEFORE UPDATE ON macro.indicators
    FOR EACH ROW EXECUTE FUNCTION signals_meta.touch_updated_at();

-- ── macro.releases ──────────────────────────────────────────────────────────
-- One row per (indicator, period_start). released_at is the public timestamp,
-- which is what matters for trading — period_start is what the value refers to.
CREATE TABLE IF NOT EXISTS macro.releases (
    release_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    indicator_id    INTEGER NOT NULL REFERENCES macro.indicators(id),
    period_start    DATE NOT NULL,                -- e.g. 2026-04-01 for April CPI
    period_end      DATE,                          -- usually period_start + frequency
    value           NUMERIC,                       -- reported value (may revise on later release)
    prior_value     NUMERIC,                       -- value before this release, for delta calc
    consensus       NUMERIC,                       -- street consensus expectation, if known
    surprise        NUMERIC,                       -- (value - consensus); also store standardised when available
    surprise_z      NUMERIC,                       -- z-scored surprise (value-consensus)/stdev(historical_surprise)
    released_at     TIMESTAMPTZ NOT NULL,         -- when this value became public
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_revision     BOOLEAN NOT NULL DEFAULT FALSE, -- TRUE when this row supersedes an earlier release for same period
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (indicator_id, period_start, is_revision, released_at)
);

CREATE INDEX IF NOT EXISTS idx_releases_released_at    ON macro.releases (released_at DESC);
CREATE INDEX IF NOT EXISTS idx_releases_indicator      ON macro.releases (indicator_id, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_releases_ingested       ON macro.releases (ingested_at DESC);

-- ── Seed indicators (commented; uncomment + edit, or insert via script) ──
-- INSERT INTO macro.indicators (code, name, source, source_series, units, frequency, importance) VALUES
--   ('CPI',      'CPI, All Urban Consumers',           'fred', 'CPIAUCSL', 'index_1982_84_100', 'monthly',   5),
--   ('CORECPI',  'Core CPI (ex food & energy)',         'fred', 'CPILFESL', 'index_1982_84_100', 'monthly',   5),
--   ('UNRATE',   'Unemployment Rate',                   'fred', 'UNRATE',   'percent',           'monthly',   5),
--   ('PAYEMS',   'Nonfarm Payrolls',                    'fred', 'PAYEMS',   'thousands',         'monthly',   5),
--   ('FEDFUNDS', 'Effective Federal Funds Rate',        'fred', 'FEDFUNDS', 'percent',           'monthly',   5),
--   ('GDPC1',    'Real GDP',                            'fred', 'GDPC1',    'billions_chained',  'quarterly', 4),
--   ('RSAFS',    'Retail Sales',                        'fred', 'RSAFS',    'millions',          'monthly',   4),
--   ('ICSA',     'Initial Jobless Claims',              'fred', 'ICSA',     'persons',           'weekly',    4)
-- ON CONFLICT (code) DO NOTHING;
