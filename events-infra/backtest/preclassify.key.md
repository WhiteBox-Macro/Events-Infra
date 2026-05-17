# preclassify.py

**Purpose:** Batch pre-classify events using an LLM before backtest runtime. Splits events into N batches, sends each as a single LLM call for consistent labeling. Outputs `events_classified_cache.json`.

## Key Functions

- **`fetch_events(start_date, end_date)`** — queries `events.classified` for headlines, filters by date range
- **`format_event_block(events)`** — formats events as `ID=... | date | headline` lines for the LLM prompt
- **`classify_batch(client, events, tickers, model, batch_num, total_batches)`** — sends one batch to LLM, parses JSON array response, returns list of classification dicts
- **`load_existing_cache()`** — loads existing cache file (resume-safe)
- **`main()`** — CLI: fetches events, skips already-cached, batches uncached, classifies, saves after each batch

## Inputs/Outputs

- **CLI args:** --start, --end, --tickers, --batches (default 5), --model, -v
- **Output:** `events_classified_cache.json` next to `backtest/` dir (one level up)
- **DB:** reads from `events.classified` via dbkit.pg

## Classification Schema

Each event gets: `event_category` (12 standard labels), `sub_category` (specific), `affected_tickers` (subset of target tickers).

## Dependencies

- anthropic SDK (LLM calls)
- dbkit.pg, dbkit.constants (DB + env)

## Gotchas

- Saves cache after EACH batch (resume-safe if interrupted)
- 2-second sleep between batches
- LLM endpoint defaults to `http://192.168.1.10:9210` (Rin proxy)
- Cache keyed by string event_id
- Prompt instructs LLM to use CONSISTENT category labels across batches
