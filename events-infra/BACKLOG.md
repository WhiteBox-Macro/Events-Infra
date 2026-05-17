# Events-Infra Backlog

Tracking deferred work, known limitations, and adversarial-review findings that didn't get fixed in-session. Ordered by priority — fix things at the top first.

Severity tags:
- **C** Critical — runtime crash / data loss / security
- **I** Important — silent-failure mode or design gap that bites real users
- **M** Minor — cosmetic / doc / observability

---

## Round 3 adversarial review findings (2026-05-18, NOT yet fixed)

Three Important findings surfaced after Round 2 fixes landed. Loop did NOT converge; these are the convergence-blocker items.

### B-1 · [I] `tilt_unit` CHECK constraint rejects its own documented upper bound

- **Where:** [db/migrations/006_tilt_unit_check.sql:17](db/migrations/006_tilt_unit_check.sql)
- **What:** `signals.gate_params.tilt_unit REAL` + `CHECK (tilt_unit <= 0.10)`. REAL widens at compare time: `0.10::real` stored as `0.10000000149...`, exceeds literal `0.10::float8`. **Empirically verified:** `INSERT ... tilt_unit=0.10` returns `ERROR: new row for relation "gate_params" violates check constraint "gate_params_tilt_unit_bounds"`.
- **Fix options** (pick one):
  1. New migration `007_tilt_unit_bounds_fix.sql` widens bound: `DROP CONSTRAINT ... ADD CONSTRAINT ... CHECK (tilt_unit > 0 AND tilt_unit <= 0.1001)` — simplest, semantically equivalent (the 0.10 doc-bound was already defensive)
  2. Cast both sides to `real`: `CHECK (tilt_unit::real > 0::real AND tilt_unit::real <= 0.10::real)` — preserves exact 0.10 boundary
  3. Change column type to `NUMERIC(5,4)` — decimal-exact, cleanest, requires ALTER COLUMN TYPE
- **Effort:** ~10 min migration + verification

### B-2 · [I] `backtest/FOLDER.md` doesn't list `gate_params.py`

- **Where:** [backtest/FOLDER.md](backtest/FOLDER.md) Files table
- **What:** Read Gate violation per `~/.claude/CLAUDE.md`. Anyone navigating `backtest/` via FOLDER.md will miss `gate_params.py` entirely — including `GateParams`, `GateParamsRegistry`, `GLOBAL_DEFAULTS`, `BROAD_TICKER` sentinel that the strategy now depends on. (`portfolio_allocator.py` is also missing from the table — pre-existing drift, address while editing.)
- **Fix:** Add row: `| gate_params.py | GateParams dataclass + GateParamsRegistry: per-(category, ticker) decision-gate parameters loaded from signals.gate_params |`. Also add `portfolio_allocator.py` row while there. Mention new `signals` schema + migrations 004/006 in the Data Flow section.
- **Effort:** ~5 min

### B-3 · [I] `compute_tilt` vs `decide_trade` `min_obs` semantic asymmetry

- **Where:** [backtest/portfolio_allocator.py:95-110](backtest/portfolio_allocator.py) vs [backtest/strategies/sonnet_event_strategy.py:240-241](backtest/strategies/sonnet_event_strategy.py)
- **What:** `decide_trade` (discrete) treats `stats.count < params.min_obs` as a hard gate (returns `None, "insufficient obs"`). `compute_tilt` (rebalance) treats it as a soft gate: falls through to tone-only direction with full `direction * llm_weight * tilt_unit`. After R2 wired GateParams into `compute_tilts`, an agent setting `min_obs=10` expects under-10-obs events to skip in BOTH modes — but rebalance keeps trading on tone alone.
- **Decision needed:**
  - Option A — gate-with-skip when params is non-None: `compute_tilt` returns 0 if `stats is not None AND stats.count < params.min_obs`. Preserves backward-compat for `params=None` legacy callers. Matches discrete-mode semantics under agent control.
  - Option B — keep current behavior, rename the param to `min_obs_for_contrarian` (since it now only gates the contrarian branch), update docs.
  - My recommendation: A (less surprising to agents).
- **Effort:** ~20 min code + docs + a smoke test row in the next regression set

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

---

## How to drain this backlog

1. **B-1, B-2, B-3** are convergence-blocking — fix in the next session before any new work.
2. After B-* lands, decide between starting **P-1** (unblocks more agent-side work) or **P-2 / P-5** (more event sources). Per user-confirmed plan, both work-streams run in parallel.
3. **L-*** items are quality-of-life — pick up opportunistically when touching the relevant file.
