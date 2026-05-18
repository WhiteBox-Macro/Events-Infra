# classify.py

**Purpose:** Coordinator + per-row classifier worker pool. Claims pending rows from `events.raw` via `FOR UPDATE SKIP LOCKED`, reads the raw JSON file, calls the unified Sonnet prompt, cross-checks against mechanical extraction, writes the structured row to `events.classified`.

Post-2026-05-18 refactor: single Sonnet 4.6 call per event populates EVERY column (including the previously-dropped `event_outcome` and the new `ticker_impacts` JSONB).

## Key Functions

- `claim_pending_row(source_channel="twitter_twitterapiio")` — atomic FOR UPDATE SKIP LOCKED claim.
- `read_raw_file(file_path)` — reads JSON from `$DB_BASE/events/raw/{file_path}`.
- `_clamp_ticker_impacts(impacts, target_tickers)` — **authoritative enforcer** of the ticker_impacts contract: max 3 entries, universe-only, valid role enum, weight clamped to [0, 1]. The prompt advises; this function enforces.
- `_filter_markets(markets)` — drops impact_market values not in the 8-value enum.
- `build_classified_row(raw_row, llm, mechanical, target_tickers)` — maps the unified LLM output to the events.classified row dict. Populates BOTH new columns (`event_outcome`, `ticker_impacts`, `sector`, `classifier_version=2`, `raw_classification`) AND legacy columns (`inferred_tone`, `inferred_magnitude`, `classification_confidence`, `tickers`, `sectors`, `inferred_impact_markets`) for migration-window compatibility. Migration 009 then renames/drops the legacy ones.
- `mark_status(raw_id, status, error=None)` — UPDATE events.raw status (`pending|processing|classified|failed|skipped|skip_dup`).
- `classify_one(raw_row, stats, target_tickers, model=DEFAULT_MODEL)` — full per-row pipeline (read → extract → classify → cross-check → reclassify on discrepancy → upsert → mark).
- `worker_loop(worker_id, stats, stop, target_tickers, model)` — single worker; loops claiming until queue empty or stop event set.
- `run_parallel(num_workers=6, retry_failed=False, target_tickers=None, model=DEFAULT_MODEL) -> dict` — orchestrator. Returns final stats dict + model + universe size.

## Key Constants

- `RAW_ROOT = DB_BASE / "events" / "raw"` — datalake root for JSON files.
- `DEFAULT_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-sonnet-4-6")` — override via env or CLI.
- `LLM_TIMEOUT_SEC = 60` — per-call timeout (currently not actively enforced; passed for future use).

## Inputs/Outputs

- **Input:** `events.raw` rows with `classify_status='pending'` + JSON files under `$DB_BASE/events/raw/`.
- **Output:** `events.classified` rows (one per raw row); `events.raw.classify_status` updated to `classified` or `failed`.

## Dependencies

- `dbkit.pg` (execute, upsert, transaction), `dbkit.constants.DB_BASE`
- `extract.py` (mechanical extraction + discrepancy check)
- `prompt.py` (unified Sonnet prompt)

## Gotchas

- `_clamp_ticker_impacts` drops out-of-universe tickers SILENTLY. The LLM's full picture (incl. out-of-universe) is preserved in `raw_classification JSONB` for future re-extraction.
- `find_discrepancies` only flags genuine hallucinations now (primary_ticker not in source text) or empty LLM output despite cashtags. Pre-fix, it spuriously triggered ~70% of the time because it checked `llm_result["tickers"]` which the new prompt doesn't emit.
- `pg.upsert("events.classified", row, conflict_on=["raw_id"])` requires the UNIQUE index on `events.classified.raw_id` (migration 007).
- Dual-column writes (`inferred_tone` + new shape) are intentional during the migration window. Migration 009 renames `inferred_*` columns; classify.py keeps writing the same logical values.
