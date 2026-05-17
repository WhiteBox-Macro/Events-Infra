# backfill_structural_tags.py

**Purpose:** One-shot backfill: copies the structural tags from `backtest/events_classified_cache.json` into the new `events.classified` columns added by migration 003. Closes the gap between pre-migration cache state and post-migration PG state.

## Key Functions

- `load_cache()` — reads `backtest/events_classified_cache.json`. Exits if missing.
- `update_one(event_id, tag)` — UPDATE on a single row, guarded by `WHERE event_category IS NULL` (idempotent). Returns True if a row was updated.
- `main()` — CLI: counts NULL rows in PG, intersects with cache event_ids, reports overlap. Dry-run by default.

## CLI

```
python events-infra/scripts/backfill_structural_tags.py             # dry-run
python events-infra/scripts/backfill_structural_tags.py --apply     # write
```

## Dependencies

- `dbkit.pg`, `dbkit.constants.load_dotenv_files`

## Gotchas

- Cache path is hardcoded to `events-infra/backtest/events_classified_cache.json` relative to repo
- WHERE clause makes re-runs safe — already-backfilled rows are skipped
- Logs progress; safe to interrupt mid-run (each UPDATE runs in its own `pg.transaction()` block via a raw cursor — necessary because `pg.execute` auto-wraps Python lists with `Json()`, which breaks the `TEXT[]` `sector_impact` column)
- A "would update 219" dry-run that becomes "updated 219" on `--apply` is the expected happy path for the current cache state
