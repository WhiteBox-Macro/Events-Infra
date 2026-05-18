-- 008_unified_classification.sql
-- Schema changes for the unified single-Sonnet classifier (replaces the
-- prior Haiku-per-row + Sonnet-batched two-stage pipeline).
--
-- Additive only — safe alongside existing data. Renames + drops of the
-- superseded columns ship as a separate migration (009) once the refactored
-- classifier has populated the new columns.
--
-- Recovers the previously-dropped event_outcome field (root cause of the
-- 2026-05-18 drift incident: build_classified_row never mapped it).

BEGIN;

ALTER TABLE events.classified
    ADD COLUMN event_outcome      TEXT,
    ADD COLUMN ticker_impacts     JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN sector             TEXT,
    ADD COLUMN classifier_version SMALLINT NOT NULL DEFAULT 1,
    ADD COLUMN raw_classification JSONB;

CREATE INDEX idx_classified_event_outcome ON events.classified (event_outcome)
    WHERE event_outcome IS NOT NULL;

CREATE INDEX idx_classified_sector ON events.classified (sector)
    WHERE sector IS NOT NULL;

COMMENT ON COLUMN events.classified.event_outcome IS
    'Sub-classification within event_type: beat|miss|inline|hike|cut|hold|new|change|removed|raise|maintain|null.';
COMMENT ON COLUMN events.classified.ticker_impacts IS
    'JSONB array, max 3 entries from target universe: [{"ticker":"NVDA","weight":1.0,"role":"primary"}]. Roles: primary|sector_spillover|broad_market.';
COMMENT ON COLUMN events.classified.sector IS
    'Single dominant sector or NULL (broad market). Replaces sectors[]/primary_sector/sector_impact[].';
COMMENT ON COLUMN events.classified.classifier_version IS
    'Bumps when the unified prompt changes. v=2 = unified Sonnet 4.6.';
COMMENT ON COLUMN events.classified.raw_classification IS
    'Full LLM output verbatim. Enables re-extraction without re-classifying if columns drift.';

COMMIT;
