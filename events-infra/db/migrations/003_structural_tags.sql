-- 003_structural_tags.sql
-- Promote the backtest-side "structural" tags out of the JSON cache and into
-- events.classified as native columns. The strategy reads them at runtime;
-- agents query them via SQL.
--
-- Adds:
--   event_category          - 14-label fixed taxonomy (from preclassify schema)
--   sub_category            - free-form specific tag (e.g. "q3_earnings")
--   sector_impact           - ordered list of impacted sectors (most -> least)
--   ticker_impact_weights   - {ticker: 0.0-1.0} JSONB
--   tags_version            - schema version of the structural tags
--   tags_classified_at      - when these tags were written (NULL until backfilled)
--
-- Backward-compatible: all new columns are NULLable or have DEFAULTs.
-- Existing 3,602 classified rows survive untouched; backfill is a separate
-- step (scripts/backfill_structural_tags.py).

BEGIN;

ALTER TABLE events.classified
    ADD COLUMN event_category          TEXT,
    ADD COLUMN sub_category            TEXT,
    ADD COLUMN sector_impact           TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN ticker_impact_weights   JSONB  NOT NULL DEFAULT '{}',
    ADD COLUMN tags_version            SMALLINT NOT NULL DEFAULT 1,
    ADD COLUMN tags_classified_at      TIMESTAMPTZ;

CREATE INDEX idx_classified_event_category
    ON events.classified (event_category)
    WHERE event_category IS NOT NULL;

CREATE INDEX idx_classified_ticker_weights
    ON events.classified USING gin (ticker_impact_weights);

COMMENT ON COLUMN events.classified.event_category IS
    '14-label structural taxonomy from backtest preclassify. NULL = not yet tagged.';
COMMENT ON COLUMN events.classified.sub_category IS
    'Free-form specific tag (e.g. q3_earnings, tariff_escalation).';
COMMENT ON COLUMN events.classified.sector_impact IS
    'Ordered list of impacted sectors, most-to-least.';
COMMENT ON COLUMN events.classified.ticker_impact_weights IS
    'JSONB {ticker: 0.0-1.0}. 1.0=directly about company, 0.5-0.8=sector spillover, 0.2-0.4=broad market.';

COMMIT;
