-- 009_classified_rename_cleanup.sql
-- Rename "inferred_*" / "classification_confidence" columns to their
-- post-unified names; drop columns superseded by ticker_impacts (JSONB),
-- sector (single TEXT), and primary_ticker (kept).
--
-- Must run AFTER migration 008 (additive new columns) AND AFTER the
-- refactored parser-classifier has populated the new columns for every row
-- expected to survive.
--
-- See plan: frolicking-percolating-minsky.md (2026-05-18 unified
-- classification refactor).

BEGIN;

-- Renames: drop the "inferred_" prefix; align with prompt output names
ALTER TABLE events.classified RENAME COLUMN inferred_tone             TO tone;
ALTER TABLE events.classified RENAME COLUMN inferred_magnitude        TO magnitude;
ALTER TABLE events.classified RENAME COLUMN classification_confidence TO confidence;
ALTER TABLE events.classified RENAME COLUMN inferred_impact_markets   TO impact_markets;

-- Drop columns superseded by the unified shape:
--   tickers / sectors / primary_sector / ticker_impact_weights / sector_impact
--     -> all collapse into ticker_impacts (JSONB) + sector (single TEXT)
--        + primary_ticker (kept, can be any ticker in or out of universe)
--   tags_version / tags_classified_at
--     -> artifacts of the two-stage pipeline; replaced by classifier_version
--        + classified_at
--   sub_category
--     -> information now lives in event_type (30-label fine taxonomy)
--        + event_outcome (sub-classification)
--
-- KEPT: event_category (impact-table key for strategy), primary_ticker
-- (objective truth), event_type, event_outcome, is_regular, indicator_name,
-- consensus_value, actual_value, surprise, surprise_z, reporting_period,
-- dedup_cluster_id, cluster_sequence, related_event_id, countries,
-- classified_at, classified_by, classifier_version, raw_classification,
-- metadata (now ops-only).

ALTER TABLE events.classified
    DROP COLUMN tickers,
    DROP COLUMN sectors,
    DROP COLUMN primary_sector,
    DROP COLUMN ticker_impact_weights,
    DROP COLUMN sector_impact,
    DROP COLUMN tags_version,
    DROP COLUMN tags_classified_at,
    DROP COLUMN sub_category;

COMMIT;
