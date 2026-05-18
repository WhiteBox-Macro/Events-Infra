# timeline.py

**Purpose:** Loads parquet bar data and classified events from PostgreSQL, merges them into a single sorted tick stream with bars-before-events ordering.

## Key Functions

- **`_load_parquet_bars(ticker, parquet_dir)`** — loads all .parquet files from `{parquet_dir}/{ticker}/` or `{ticker}/1m/`, concatenates, sorts by timestamp, returns (DataFrame, timestamps array)
- **`_load_events()`** — queries `events.classified` table from PostgreSQL via dbkit.pg
- **`_bar_stream(ticker, df)`** — generator yielding (ts, PRIORITY_BAR, ticker, idx, row) tuples
- **`_event_stream(events)`** — generator yielding (ts, PRIORITY_EVENT, "event", idx, ev) tuples

## Key Class: TimelineMerger

- **Constructor** — loads parquet bars for each ticker, loads events, applies start/end date filters, computes total tick count
- **`iter_grouped()`** — yields `(timestamp, [ticks])` groups in chronological order using `heapq.merge`. Within each timestamp: bars first (PRIORITY_BAR=0), then events (PRIORITY_EVENT=1), bars sorted by ticker
- **`_make_bar_tick(ticker, idx, row)`** — constructs BarTick from DataFrame row
- **`_make_event_tick(ev)`** — constructs EventTick from DB row dict. Reads the unified columns (event_category, event_outcome, ticker_impacts JSONB, sector, primary_ticker, tone, magnitude, confidence) plus the scheduled-release block (indicator_name, consensus_value, actual_value, surprise, reporting_period). Defensive JSONB parsing for `ticker_impacts` (tolerates both dict-decoded and string forms).

## Inputs/Outputs

- **Input:** BacktestConfig (tickers, parquet_dir, start_date, end_date)
- **Output:** iterator of (datetime, list[Tick]) groups
- **Stored state:** `bar_dfs` dict (ticker -> DataFrame), `bar_timestamps`, `events` list

## Dependencies

- pandas, numpy, heapq
- tick.py (BarTick, EventTick)
- dbkit.pg (PostgreSQL queries)

## Gotchas

- Parquet dir supports two layouts: `{ticker}/*.parquet` and `{ticker}/1m/*.parquet`
- Date filtering uses `__import__("datetime").timedelta` inline (quirky but works)
- All timestamps forced to UTC; naive timestamps get `.replace(tzinfo=timezone.utc)`
- Events loaded in full from DB then filtered in Python (no SQL WHERE on dates)
