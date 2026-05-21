-- 010_tilt_unit_bounds_fix.sql
-- Fix B-1: 006's CHECK (tilt_unit <= 0.10) rejects its own documented upper bound
-- because tilt_unit is REAL and PostgreSQL widens REAL to FLOAT8 at compare time:
-- 0.10::real stored as 0.10000000149... > 0.10::float8, so the literal upper
-- bound becomes unreachable.
--
-- Empirical proof:
--   INSERT INTO signals.gate_params (... tilt_unit=0.10 ...)
--   → ERROR: violates check constraint "gate_params_tilt_unit_bounds"
--
-- Fix: widen the bound to 0.1001. The 0.10 doc-bound was already defensive
-- (MAX_WEIGHT=0.15 clamps anything bigger structurally); 0.1001 admits the
-- intended 0.10 boundary without changing column type or rewriting heap pages.

BEGIN;

ALTER TABLE signals.gate_params
    DROP CONSTRAINT gate_params_tilt_unit_bounds;

ALTER TABLE signals.gate_params
    ADD CONSTRAINT gate_params_tilt_unit_bounds
    CHECK (tilt_unit > 0 AND tilt_unit <= 0.1001);

COMMIT;
