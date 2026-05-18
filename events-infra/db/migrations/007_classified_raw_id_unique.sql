-- 007_classified_raw_id_unique.sql
-- Add UNIQUE constraint on events.classified.raw_id.
--
-- Discovered as schema drift on 2026-05-18: the local PG had this index
-- (added out-of-band during initial classifier development) but the VPS PG
-- did not. The classifier uses `INSERT ... ON CONFLICT (raw_id) DO UPDATE`,
-- which requires a unique-or-exclusion constraint on the conflict target;
-- without this index, every classification attempt fails with
-- `InvalidColumnReference: there is no unique or exclusion constraint
-- matching the ON CONFLICT specification`.
--
-- IF NOT EXISTS makes this safe to apply on already-fixed systems (local)
-- without errors. Migration runner records both sides identically.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS idx_classified_raw_id
    ON events.classified (raw_id);

COMMIT;
