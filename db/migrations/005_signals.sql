-- 005_signals.sql — trading decisions + paper-trading sim tables.
--
-- The signal layer reads news.articles / social.posts / macro.releases and
-- writes decisions here. One row per decision; a decision can supersede an
-- earlier one (typically: a slow LangGraph decision overrides the fast
-- deterministic one fired off the same source event).
--
-- Live and backtest share the same tables — the `mode` column distinguishes
-- them. This lets the same query compute alpha for both, and lets us tune
-- prompts in backtest with the confidence that live behaviour will match.
--
-- "pending → resolved" pattern: decisions start with pending=true. After the
-- horizon elapses, settle.py joins with subsequent prices, fills in
-- raw_return / alpha_return / holding_hours / reflection_md, flips pending=false.

CREATE SCHEMA IF NOT EXISTS signals;

-- ── signals.watchlist ──────────────────────────────────────────────────────
-- Symbol universe the agent operates on. Dispatcher reads this on every
-- event tick to decide whether to bother running the signal logic.
-- Editable at runtime via SQL — no code redeploy needed.
CREATE TABLE IF NOT EXISTS signals.watchlist (
    ticker              TEXT PRIMARY KEY,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    horizon_hours       INTEGER NOT NULL DEFAULT 24, -- default holding period for decisions on this ticker
    max_position_pct    NUMERIC(5,4) NOT NULL DEFAULT 0.05, -- 5% of portfolio
    benchmark_ticker    TEXT NOT NULL DEFAULT 'SPY',  -- for alpha computation
    notes               TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    added_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_watchlist_active ON signals.watchlist (active) WHERE active = TRUE;

DROP TRIGGER IF EXISTS trg_watchlist_updated_at ON signals.watchlist;
CREATE TRIGGER trg_watchlist_updated_at
    BEFORE UPDATE ON signals.watchlist
    FOR EACH ROW EXECUTE FUNCTION signals_meta.touch_updated_at();

-- ── signals.decisions ──────────────────────────────────────────────────────
-- One row per agent decision. `tier` separates the fast deterministic path
-- from the slow LangGraph path. `supersedes` lets a slow decision point
-- back at the fast decision it confirms / inverts / sizes-down.
CREATE TABLE IF NOT EXISTS signals.decisions (
    decision_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker              TEXT NOT NULL,
    tier                TEXT NOT NULL,                -- 'fast' | 'slow'
    mode                TEXT NOT NULL DEFAULT 'live', -- 'live' | 'backtest'
    supersedes          UUID REFERENCES signals.decisions(decision_id),
    -- What triggered this decision. JSONB so we don't add a column per kind.
    -- Shape: {"kind": "article"|"post"|"macro_release"|"manual", "id": "<uuid>"}
    source_event        JSONB NOT NULL,
    rating              TEXT NOT NULL,                -- 'Buy' | 'Overweight' | 'Hold' | 'Underweight' | 'Sell'
    confidence          NUMERIC(4,3) NOT NULL,        -- 0.000 – 1.000
    horizon_hours       INTEGER NOT NULL,             -- intended holding period
    rationale_md        TEXT NOT NULL,                -- analyst-style markdown explanation
    -- Slow-path agents fill this with the bull/bear + risk debate transcripts.
    -- Fast-path decisions store the scoring factors list here instead.
    debate_transcript   JSONB,
    -- Identifies the code+prompt version that produced this row. SHA-256 of
    -- (prompt_text || model_name || tool_versions). Lets backtests group runs.
    agent_hash          TEXT NOT NULL,
    experiment_key      TEXT,                          -- backtest experiment tag
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Settled-state columns (NULL until horizon elapses + settle job runs).
    pending             BOOLEAN NOT NULL DEFAULT TRUE,
    raw_return          NUMERIC(8,5),                 -- realised return over horizon, decimal (0.0123 = +1.23%)
    alpha_return        NUMERIC(8,5),                 -- raw_return - benchmark_return over same window
    holding_hours       NUMERIC(8,2),                 -- actual hours held (may differ from horizon_hours)
    reflection_md       TEXT                           -- 2-4 sentence post-mortem written by reflect.py
);

CREATE INDEX IF NOT EXISTS idx_decisions_ticker_created   ON signals.decisions (ticker, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_created          ON signals.decisions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_pending          ON signals.decisions (pending) WHERE pending = TRUE;
CREATE INDEX IF NOT EXISTS idx_decisions_mode_experiment  ON signals.decisions (mode, experiment_key);
CREATE INDEX IF NOT EXISTS idx_decisions_supersedes       ON signals.decisions (supersedes) WHERE supersedes IS NOT NULL;
-- GIN on source_event for kind/id lookups (e.g. "all decisions from article X")
CREATE INDEX IF NOT EXISTS idx_decisions_source_event     ON signals.decisions USING gin (source_event);

-- ── signals.paper_positions ────────────────────────────────────────────────
-- Open and closed positions from the paper-trading sim. One position per
-- decision that resulted in a fill. Slow decisions that flip a fast position
-- close the existing position and open a new one (so the chain is visible).
CREATE TABLE IF NOT EXISTS signals.paper_positions (
    position_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id         UUID NOT NULL REFERENCES signals.decisions(decision_id),
    ticker              TEXT NOT NULL,
    side                TEXT NOT NULL,                -- 'long' | 'short'
    qty                 NUMERIC(18,6) NOT NULL,
    entry_price         NUMERIC(18,6) NOT NULL,
    entry_at            TIMESTAMPTZ NOT NULL,
    exit_price          NUMERIC(18,6),
    exit_at             TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'open', -- 'open' | 'closed'
    mode                TEXT NOT NULL DEFAULT 'live', -- 'live' | 'backtest'
    realized_pnl        NUMERIC(18,6),                -- filled at close: qty*(exit-entry) for long, inverse for short
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_positions_open       ON signals.paper_positions (ticker, entry_at DESC) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_positions_decision   ON signals.paper_positions (decision_id);
CREATE INDEX IF NOT EXISTS idx_positions_mode       ON signals.paper_positions (mode);

-- ── signals.paper_trades ───────────────────────────────────────────────────
-- Individual fills. Each position has at least an 'open' trade; positions
-- that have been closed have one 'close' trade too. Partial closes (slow
-- agent sizes down) write additional 'close' trades against the same position.
CREATE TABLE IF NOT EXISTS signals.paper_trades (
    trade_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id         UUID NOT NULL REFERENCES signals.paper_positions(position_id),
    kind                TEXT NOT NULL,                -- 'open' | 'close'
    side                TEXT NOT NULL,                -- 'buy' | 'sell' (the direction of THIS fill)
    qty                 NUMERIC(18,6) NOT NULL,
    price               NUMERIC(18,6) NOT NULL,
    executed_at         TIMESTAMPTZ NOT NULL,
    slippage_bps        NUMERIC(6,2),                 -- synthetic slippage applied vs the quote
    mode                TEXT NOT NULL DEFAULT 'live',
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_trades_position      ON signals.paper_trades (position_id, executed_at);
CREATE INDEX IF NOT EXISTS idx_trades_executed      ON signals.paper_trades (executed_at DESC);

-- ── signals.mtm_history ────────────────────────────────────────────────────
-- Periodic marks for open positions. Pruned to the last N marks per position
-- by trader/paper/mtm.py (see _prune_*_history pattern borrowed from AI-Trader).
CREATE TABLE IF NOT EXISTS signals.mtm_history (
    id                  BIGSERIAL PRIMARY KEY,
    position_id         UUID NOT NULL REFERENCES signals.paper_positions(position_id),
    mark_at             TIMESTAMPTZ NOT NULL,
    mark_price          NUMERIC(18,6) NOT NULL,
    unrealized_pnl      NUMERIC(18,6) NOT NULL,
    mode                TEXT NOT NULL DEFAULT 'live'
);

CREATE INDEX IF NOT EXISTS idx_mtm_position_time ON signals.mtm_history (position_id, mark_at DESC);

-- ── signals.benchmark_marks ────────────────────────────────────────────────
-- Stored benchmark (SPY/QQQ/^N225/…) prices, used to compute alpha at settle
-- time. Same row also serves backtest replay so we don't need an external
-- price call mid-run.
CREATE TABLE IF NOT EXISTS signals.benchmark_marks (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL DEFAULT 'SPY',
    mark_at             TIMESTAMPTZ NOT NULL,
    price               NUMERIC(18,6) NOT NULL,
    source              TEXT NOT NULL,                -- 'yfinance' | 'alpha_vantage' | ...
    mode                TEXT NOT NULL DEFAULT 'live',
    UNIQUE (ticker, mark_at, mode)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_ticker_time ON signals.benchmark_marks (ticker, mark_at DESC);

-- ── signals.price_cache ────────────────────────────────────────────────────
-- Per-ticker price points used by HistoricalPriceSource in backtest, and by
-- MTM in live (so we don't re-call yfinance on every mark). Keyed (ticker,
-- price_at) so the same row serves both modes.
CREATE TABLE IF NOT EXISTS signals.price_cache (
    ticker              TEXT NOT NULL,
    price_at            TIMESTAMPTZ NOT NULL,
    price               NUMERIC(18,6) NOT NULL,
    source              TEXT NOT NULL,                -- 'yfinance' | 'alpha_vantage' | 'aotc_db'
    cached_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, price_at)
);

CREATE INDEX IF NOT EXISTS idx_price_cache_ticker_recent ON signals.price_cache (ticker, price_at DESC);

-- ── signals.experiments ────────────────────────────────────────────────────
-- Roll-up rows written by trader/backtest/report.py — one per backtest run.
-- Lets us compare prompt/rule variants without re-reading all decision rows.
CREATE TABLE IF NOT EXISTS signals.experiments (
    experiment_key      TEXT PRIMARY KEY,
    description         TEXT,
    agent_hash          TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    window_from         DATE,
    window_to           DATE,
    tickers             TEXT[] NOT NULL DEFAULT '{}',
    n_decisions         INTEGER,
    hit_rate            NUMERIC(5,4),                 -- fraction of decisions where alpha_return > 0
    avg_alpha           NUMERIC(8,5),
    sharpe              NUMERIC(8,4),
    max_drawdown        NUMERIC(8,5),
    metrics             JSONB NOT NULL DEFAULT '{}'::jsonb, -- the full breakdown
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_experiments_started ON signals.experiments (started_at DESC);
