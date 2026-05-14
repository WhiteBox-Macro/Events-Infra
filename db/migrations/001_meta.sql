-- 001_meta.sql — bootstrap migration tracking + shared helpers for AOTC-Signals.
--
-- Why a separate schema: this repo's tooling needs to coexist cleanly with
-- AOTC-DB's tables in the same Postgres. `signals_meta` is reserved for
-- repo-internal plumbing (migration state, helper functions). The actual
-- domain schemas (news, macro, social, signals) are created in later
-- migrations.

CREATE SCHEMA IF NOT EXISTS signals_meta;

-- Applied-migration ledger. apply_migrations.py reads + writes this.
CREATE TABLE IF NOT EXISTS signals_meta.applied_migrations (
    filename     TEXT PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum     TEXT,
    applied_by   TEXT
);

-- Reusable updated_at trigger function. AOTC-DB has its own copy in
-- public.update_updated_at(); we keep ours under signals_meta to avoid
-- depending on AOTC-DB's init scripts having run first.
CREATE OR REPLACE FUNCTION signals_meta.touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
