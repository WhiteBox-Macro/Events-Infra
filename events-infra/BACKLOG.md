# Events-Infra Backlog

Tracking deferred work, known limitations, and adversarial-review findings that didn't get fixed in-session. Ordered by priority — fix things at the top first.

Severity tags:
- **C** Critical — runtime crash / data loss / security
- **I** Important — silent-failure mode or design gap that bites real users
- **M** Minor — cosmetic / doc / observability

---

## Round 3 adversarial review findings (2026-05-18) — RESOLVED 2026-05-22

All three Round-3 Important findings closed; Round 3 convergence-blockers cleared. Commit: `3308f85`.

### B-1 · [I] `tilt_unit` CHECK constraint rejects its own documented upper bound — RESOLVED

- **Resolution:** Migration `010_tilt_unit_bounds_fix.sql` (Option 1) — drops + re-adds the constraint with bound `<= 0.1001`. Applied to prod PG 2026-05-22 17:24 UTC.
- **Verification:** `INSERT ... tilt_unit=0.10` previously failed; after migration the same row inserts cleanly and reads back as `0.1`. Empirical repro/fix cycle in session log.

### B-2 · [I] `backtest/FOLDER.md` doesn't list `gate_params.py` — RESOLVED

- **Resolution:** `backtest/FOLDER.md` was already updated in commit `f459b1f` (unified-classification refactor) — it lists both `gate_params.py` AND `portfolio_allocator.py`. VPS held a stale copy; rsynced 2026-05-22.
- **Verification:** Diff against deployed VPS file now matches local repo.

### B-3 · [I] `compute_tilt` vs `decide_trade` `min_obs` semantic asymmetry — RESOLVED

- **Resolution:** Option A (gate-with-skip). When `params` is provided AND `stats is not None` AND `stats.count < min_obs`, `compute_tilt` now returns 0 — matches `decide_trade` discrete-mode semantics. Legacy `params=None` callers keep the tone-only fallback for backward-compat. Docstring + `portfolio_allocator.key.md` updated.
- **Verification:** 4-case scenario test passed (legacy soft gate preserved, B-3 hard gate kicks in, above-gate trades fire, params+no-stats still tone-fallback).

---

## Deferred from approved plan (`~/.claude/plans/frolicking-percolating-minsky.md`)

Each is its own follow-on session per the plan's "What's Deferred & Why" table.

### P-1 · B2 evaluator harness `backtest/evaluate.py::evaluate_gate()`

- **Goal:** Callable an agent can use: `evaluate_gate(category, ticker, params, train_cutoff, folds=5) -> {sharpe, hit_rate, n_trades, total_bps, fold_returns, ic_t_stat}`. Strict embargo, 75th-percentile selection (per `_feedback/feedback_p75_hyperparameter_selection.md`), no peeking past `train_cutoff`.
- **Prereq:** none — unblocks B4
- **Effort:** ~250 LOC + tests + own session (worth dedicated review)

### P-2 · A2.1 SEC EDGAR 8-K ingester

- **Goal:** `scripts/ingest_sec_edgar_8k.py` following the `ingest_x_twitterapi_io.py` skeleton; populates `events.raw` from `$DB_BASE/events/raw/rss/sec_edgar_8k/`. Per-source `extract_mechanical()` update in `parser-classifier/extract.py` to recognize 8-K filings.
- **Effort:** ~300 LOC + per-source validation

### P-3 · B4 refit-time auto-tuner

- **Goal:** Hook in [replay_driver.py::_maybe_refit()](backtest/dashboard/replay_driver.py): find cells with `n>=20 AND 0.45<hit_rate<0.55` (high-confusion), spawn a subagent per cell with `evaluate_gate` contract, accept proposals where `delta_sharpe>0.3 AND p<0.05`. Writes `signals.gate_params` with `status='proposed'` — manual confirm before active.
- **Prereq:** P-1 (B2 evaluator harness must land first)
- **Effort:** ~200 LOC + a `signals.gate_params` promotion CLI

### P-4 · A2.2 Fed press RSS ingester

- **Effort:** ~300 LOC

### P-5 · A2.3 BLS / BEA macro release ingester

- **Important:** This is where the locked-positive surprise-direction signal lives (per [_feedback/feedback_p75_hyperparameter_selection.md](~/.claude/projects/C--Users-wfl15/memory/_feedback/) — +10bps/H=1, 83% hit rate). Wire `side_rule='surprise_direction'` rows in `signals.gate_params` for these categories as soon as data flows.
- **Effort:** ~300 LOC

### P-6 · A3 source-quality columns + scoring

- **Prereq:** P-1 (need evaluator to validate that scores actually predict trade quality)
- **Effort:** ~100 LOC migration + classifier update

---

## Known limitations from .key.md gotchas (lower priority)

### L-1 · [M] `params.holding_bars` is read but not enforced by runner

- **Where:** `backtest/strategies/sonnet_event_strategy.key.md` Gotchas
- **What:** GateParams has `holding_bars` field that `decide_trade` reads, but the runner's scheduled-exit countdown enforces `HOLDING_BARS=15` globally. Per-(cat, ticker) holding_bars is wired through the API but doesn't yet affect actual trade duration.
- **Fix:** Plumb `params.holding_bars` from order metadata into `OrderManager.schedule_exit()`.

### L-2 · [M] `cache_only=True` fallback synthesizes equal-weight `ticker_impact_weights`

- **Where:** [backtest/strategies/sonnet_event_strategy.py](backtest/strategies/sonnet_event_strategy.py) (both `on_event` and `compute_tilts`)
- **What:** When cache misses and `cache_only=True`, the strategy fabricates `{t: 0.5 for t in self.tickers}` — every ticker gets equal weight. Now that PG has `ticker_impact_weights` (post-backfill), this fallback could SELECT from `events.classified` first. Plan Step 4 hinted at it but session scope didn't include it.
- **Fix:** Add `_pg_lookup_tags(event_id)` helper, call before the synthetic fallback.

### L-3 · [M] Strategy doesn't reload registry on dashboard seek

- **Where:** [backtest/strategies/sonnet_event_strategy.py::reset()](backtest/strategies/sonnet_event_strategy.py)
- **What:** `reset()` clears `_blacklisted`, `_pending`, etc. — but NOT `gate_registry`. On dashboard "seek to 0%" the registry reflects PG state at startup, not current state. If the user edits `signals.gate_params` mid-session and seeks back, the strategy uses stale registry until the next refit.
- **Fix:** Add `if self.gate_registry: self.gate_registry.reload()` to `reset()`.

### L-4 · [M] No CHECK bounds on `min_obs`, `min_hit_rate`, `min_avg_bps` in `signals.gate_params`

- **Where:** [db/migrations/004_gate_params.sql](db/migrations/004_gate_params.sql)
- **What:** Agent could insert `min_obs=-5` or `min_hit_rate=1.5` or `min_avg_bps=-100`. The latter effectively disables the threshold gate (since the code compares `abs(stats.mean*10000) < min_avg_bps` and negative bps always passes). Defense-in-depth gap.
- **Fix:** Migration that adds:
  ```sql
  CHECK (min_obs >= 0 AND min_obs <= 1000)
  CHECK (min_hit_rate >= 0 AND min_hit_rate <= 1)
  CHECK (min_avg_bps >= 0)
  ```

### L-5 · [M] `sector_spillover` WARNING fires per (event × ticker) in rebalance mode

- **Where:** [backtest/portfolio_allocator.py::compute_tilt](backtest/portfolio_allocator.py), [backtest/strategies/sonnet_event_strategy.py::decide_trade](backtest/strategies/sonnet_event_strategy.py)
- **What:** Loud-fail is currently WARNING. For a 14-ticker rebalance event with a sector_spillover row on a catch-all category, that's 14 WARNING lines per event. Over 60 days × ~30 events/day, ~25k warnings per replay.
- **Fix:** Downgrade to DEBUG with a one-time INFO on first occurrence per process. Use a module-level set to deduplicate (cat, ticker) keys.

### L-6 · [RESOLVED 2026-05-18] VPS `.venv` data-stack deps installed

- **Was:** `/opt/react-cloud/.venv` had only `psycopg2-binary` + `anthropic`.
- **Now installed:** `numpy 2.4.5`, `pandas 3.0.3`, `pyarrow 24.0.0`, `websockets 16.0` (`aiohttp` not needed — not imported anywhere in events-infra).
- **Verified:** Full dashboard import chain loads cleanly on VPS — `config`, `tick`, `engine`, `timeline`, `order_manager`, `portfolio_allocator`, `gate_params`, `sonnet_event_strategy`, `replay_driver`. `SonnetEventStrategy(cache_only=True)` instantiates with 219 cache entries. `decide_trade` smoke returns expected legacy result.
- **Not yet started:** dashboard server not launched on VPS. See L-7 (parquet data) before any real run.

### L-7 · [M] VPS missing parquet market-data for backtest replay

- **Where:** `/opt/react-cloud/events-infra/market-data/` has only the `fetch_*.py` scripts; no `1m-parquet/` subdirectory.
- **What:** Deploy excluded `events-infra/market-data/1m-parquet/` and `1m-parquet.zip` (~46MB locally, but represents 34 tickers × months of 1-min bars). The dashboard `TimelineMerger` would fail to find any bar data and emit zero ticks.
- **Decision needed:** Either (a) sync parquet over (one-shot tar+ssh, similar to deploy command but without exclude); or (b) have the VPS fetch its own data via `market-data/fetch_yf.py` or `fetch_ibkr.py` (requires `yfinance` and/or `ib_insync` in `.venv` — not currently installed); or (c) keep VPS classification-only and run the dashboard on local only.
- **No fix shipped this session per user instruction ("don't start backtesting yet").**

### L-8 · [RESOLVED 2026-05-18] classify.py dual-write to legacy columns removed post-009

- **Was:** During the migration window `build_classified_row` wrote BOTH new (`event_outcome`, `ticker_impacts`, `sector`, …) AND legacy (`inferred_tone`, `tickers`, `sectors`, `inferred_impact_markets`) columns.
- **Resolved:** Migration 009 applied on both VPS and local. `build_classified_row` now writes only post-009 names (`tone`, `magnitude`, `confidence`, `impact_markets`). Smoke-tested via dashboard replay (37 events through strategy → 19 decisions, 0 errors).

### L-9 · [RESOLVED 2026-05-18] event_outcome silently dropped — root cause of the drift incident

- **Root cause:** `parser-classifier/classify.py::build_classified_row` mapped the LLM dict to PG columns but never included `event_outcome`. The Haiku prompt asked for it, the LLM produced it, the mapper dropped it.
- **Resolution:** Migration 008 added the `event_outcome TEXT` column. The unified prompt continues to emit it. The rewritten `build_classified_row` now stores it. Pilot run (50 events): 3 events with non-null event_outcome — the data we were losing is back. Full run (3,602 events) in progress.

---

## How to drain this backlog

1. **B-1, B-2, B-3** are convergence-blocking — fix in the next session before any new work.
2. After B-* lands, decide between starting **P-1** (unblocks more agent-side work) or **P-2 / P-5** (more event sources). Per user-confirmed plan, both work-streams run in parallel.
3. **L-*** items are quality-of-life — pick up opportunistically when touching the relevant file.
