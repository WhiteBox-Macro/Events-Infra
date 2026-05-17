-- 006_tilt_unit_check.sql
-- Defense-in-depth CHECK constraint on signals.gate_params.tilt_unit.
-- Without bounds, an agent-proposed or fat-fingered row with tilt_unit=10
-- would yield 1000% tilts. PortfolioAllocator's target weights are clamped
-- to [MIN_WEIGHT, MAX_WEIGHT], but internal self.tilts accumulate unbounded
-- with only 0.997/bar decay — recovery takes hundreds of bars.
--
-- Bound: 0 < tilt_unit <= 0.10. The MAX_WEIGHT=0.15 ceiling means a single-
-- event tilt of 0.10 from base weight 1/14=0.071 lands at 0.171 -> clamps
-- to 0.15. Anything bigger is structurally absorbed and almost certainly a
-- mistake.

BEGIN;

ALTER TABLE signals.gate_params
    ADD CONSTRAINT gate_params_tilt_unit_bounds
    CHECK (tilt_unit > 0 AND tilt_unit <= 0.10);

COMMIT;
