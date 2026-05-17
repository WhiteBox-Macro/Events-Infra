-- 004_gate_params.sql
-- Per-(event_category, ticker) decision-gate parameters. Replaces the
-- module-level constants in sonnet_event_strategy.py with a writable
-- surface that agents can tune.
--
-- decide_trade() reads here via GateParamsRegistry.lookup(category, ticker).
-- Falls back to (category, "BROAD") then to module GLOBAL_DEFAULTS.
--
-- Workflow:
--   - Manual / default rows start with status='active'.
--   - Agent-proposed rows start with status='proposed' and require a manual
--     status flip to 'active' before they affect trading (CLAUDE.md
--     "actions visible to others" rule).
--   - Underperforming gates are flipped to status='retired' (also acts as
--     a blacklist for the strategy).

BEGIN;

CREATE SCHEMA IF NOT EXISTS signals;

CREATE TABLE signals.gate_params (
    gate_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_category      TEXT NOT NULL,
    ticker              TEXT NOT NULL,           -- "BROAD" for category-level fallback rows

    -- Decision-gate knobs (mirror sonnet_event_strategy module constants)
    min_obs             SMALLINT NOT NULL DEFAULT 3,
    min_hit_rate        REAL     NOT NULL DEFAULT 0.55,
    min_avg_bps         REAL     NOT NULL DEFAULT 2.0,
    holding_bars        SMALLINT NOT NULL DEFAULT 15,
    side_rule           TEXT     NOT NULL DEFAULT 'tone_reliable',
    tilt_unit           REAL     NOT NULL DEFAULT 0.01,

    -- Lifecycle / provenance
    version             INTEGER  NOT NULL DEFAULT 1,
    fitted_at           TIMESTAMPTZ,
    train_cutoff        DATE,
    status              TEXT     NOT NULL DEFAULT 'active',
    fitted_by           TEXT,                                     -- 'manual' | agent name | 'default'
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (event_category, ticker, version),
    CHECK (status IN ('active', 'proposed', 'retired')),
    CHECK (side_rule IN ('tone_reliable', 'contrarian', 'surprise_direction', 'sector_spillover'))
);

CREATE INDEX idx_gate_params_lookup
    ON signals.gate_params (event_category, ticker, status);

COMMENT ON TABLE signals.gate_params IS
    'Per-(category, ticker) decision-gate parameters. decide_trade() reads here; falls back to GLOBAL_DEFAULTS if no active row.';
COMMENT ON COLUMN signals.gate_params.ticker IS
    '"BROAD" = category-level row used as fallback when no ticker-specific row exists.';
COMMENT ON COLUMN signals.gate_params.side_rule IS
    'tone_reliable | contrarian | surprise_direction | sector_spillover';
COMMENT ON COLUMN signals.gate_params.status IS
    'active = used by strategy; proposed = awaits manual flip; retired = blacklisted.';

COMMIT;
