# Events-Infra Handoff

**Date:** 2026-05-14
**Repo:** AOTC-Signals (`C:\Users\wfl15\AOTC-Signals\`)
**Subfolder:** `events-infra/`

---

## Goal

Build the data infrastructure layer for an event-driven trading R&D platform. The end state: an LLM-powered parser-classifier ingests raw news/macro/social data into a structured PostgreSQL table (`events.classified`), which downstream agents use for backtesting strategies against price reactions.

## What exists in AOTC-Signals already

The repo (initial commit `ef1616b`) has a full plan and codebase for:
- **6 ingesters** (`scripts/ingest/`): RSS (SEC, Fed, Treasury, MarketWatch), Alpha Vantage news+macro, Reddit, StockTwits, Twitter
- **Existing schemas** (`db/migrations/001-007`): `news.*`, `macro.*`, `social.*`, `signals.*`
- **Trader skeleton**: dispatcher, fast signal scorer, slow LangGraph agent, paper trading, backtest harness

**Decision: sentiment scores and LLM debates are dropped** as trading decision factors. The existing `news.articles.sentiment_score`, `debate_transcript`, `slow_agent/`, `fast_signal.py` scoring rules, `reflect.py`, and `social_inference.py` are not part of the events-infra path.

## What was built this session

### Directory structure
```
events-infra/
├── FOLDER.md                       # architecture overview
├── HANDOFF.md                      # this file
├── datalake/
│   └── FOLDER.md                   # runtime folder spec + JSON format contract
├── db/migrations/
│   ├── 001_raw.sql                 # events.raw — datalake index table
│   └── 002_classified.sql          # events.classified — parser-classifier output
└── parser-classifier/
    └── FOLDER.md                   # pipeline contract
```

### Schema: `events.raw` (001_raw.sql)
Datalake index. One row per raw file ingested from any source.

| Column | Purpose |
|--------|---------|
| raw_id (UUID PK) | Row identity |
| source_type | rss, api, social, macro |
| source_channel | sec_edgar_8k, alpha_vantage, reddit_wsb, twitter, ... |
| external_id | Dedup key from source (UNIQUE with source_channel) |
| file_path | Relative to `$DB_BASE/events/raw/` |
| file_hash, file_size_bytes | Integrity |
| published_at | Source publish time |
| ingested_at | When we pulled it |
| classify_status | pending → classified / failed / skipped |
| metadata (JSONB) | Source-specific overflow |

### Schema: `events.classified` (002_classified.sql)
Parser-classifier output. One flat row per classified event. No normalization.

**Column groups:**

**A. Source identity & content**
- `source_channel`, `publish_time`, `headline`, `text_content`

**B. Classification core**
- `is_regular` (BOOLEAN) — scheduled/periodic release vs breaking news. The most important column: scheduled events trade on surprise, irregular events trade on direction.
- `event_type` (TEXT) — standardized vocabulary: earnings_beat, tariff_new, rate_hike, merger_announced, ...
- `inferred_tone` — bullish | bearish | neutral | mixed
- `inferred_magnitude` — major | moderate | minor

**C. Market scope & entity resolution**
- `inferred_impact_markets` (TEXT[]) — US_EQUITY, US_FI, COMMODITY, FX, CRYPTO, EM
- `tickers` (TEXT[]) — affected ticker symbols (GIN-indexed)
- `primary_ticker` — single most affected ticker
- `sectors` (TEXT[]) — semiconductor, banking, energy, ... (GIN-indexed)
- `primary_sector` — single most affected sector
- `countries` (TEXT[]) — US, CN, EU, JP, ...

**D. Scheduled/periodic event fields** (NULL when is_regular=FALSE)
- `indicator_name` — CPI, NFP, FOMC, EARNINGS, PMI
- `scheduled_time` — when the release was expected
- `consensus_value`, `actual_value`, `surprise`, `surprise_z`
- `reporting_period` — "2026-Q1", "2026-04"

**E. Dedup & event chains**
- `dedup_cluster_id` — groups multiple articles about same event
- `cluster_sequence` — order within cluster (1 = first report)
- `related_event_id` (self-FK) — event chains (tariff proposed → signed → retaliation)

**F. Extraction metadata**
- `classified_by`, `classification_confidence`, `classified_at`, `metadata`

### Datalake spec (datalake/FOLDER.md)
Runtime folder at `$DB_BASE/events/raw/`:
```
{source_type}/{source_name}/{yyyy}/{mm}/{dd}/{external_id}.json
```
Each file has `_meta` (source, ingested_at) + `payload` (verbatim API response). Immutable after write.

## Design decisions

1. **Raw → classified, two tables only.** No intermediate articles/entities/facts normalization. News is structurally different from financial statements — one event, one row, rich columns.

2. **Modeling signal columns are NOT in the schema.** Downstream backtest/modeling agents will `ALTER TABLE events.classified ADD COLUMN ...` to append their own features (novelty, urgency, source rank, etc.). The classifier only outputs what it can determine from the text.

3. **Arrays over joins.** Tickers, sectors, countries, markets are `TEXT[]` with GIN indexes, not normalized link tables. Direct `WHERE 'NVDA' = ANY(tickers)` without joins.

4. **`is_regular` is the key partition.** Scheduled releases (is_regular=TRUE) carry consensus/actual/surprise fields. Irregular events (is_regular=FALSE) have those NULL. Different modeling approaches for each.

5. **Dedup via cluster_id.** Multiple articles about the same event share a `dedup_cluster_id`. Backtests count the cluster once; `cluster_sequence=1` identifies the first report.

## What's NOT done yet

| Work item | Description |
|-----------|-------------|
| **Parser-classifier code** | `parser-classifier/` has only FOLDER.md. No Python code yet. Needs: raw JSON reader, text cleaner, regex ticker extractor, LLM classifier prompt, DB writer. |
| **Datalake ingestion** | No code to populate `$DB_BASE/events/raw/` or `events.raw`. The existing `scripts/ingest/` ingesters write to `news.*`/`social.*`/`macro.*`, not to the events schema. Need adapters or rewrites. |
| **Migration runner** | `events-infra/db/migrations/` needs its own `apply_migrations.py` or integration with the existing one in `scripts/`. |
| **Backtest harness** | No backtest code that queries `events.classified` + price data to measure event→reaction alpha. |
| **Downstream column extensions** | Modeling agents need to define and add their own columns to `events.classified`. |
| **Cross-schema joins** | `events.classified.tickers` → `stock_os.securities` join path not formalized. Works via `ANY()` but no FK constraint (arrays can't FK). |
| **Historical backfill** | Need to backfill `events.raw` + `events.classified` from existing `news.articles` / `social.posts` / `macro.releases` data, or from Alpha Vantage historical API. |

## Key references

- **AOTC-Parser** (`~/aotc-parser/`) — the 3-lane pipeline (table/text/vision) is the architectural precedent for how to structure extraction. Events-infra simplifies this to a single flat output.
- **AOTC-DB** (`~/coverage-ingest/`) — `stock_os.securities` is the canonical ticker registry; `events.classified.tickers` should validate against it.
- **Existing ingesters** (`AOTC-Signals/scripts/ingest/`) — working code for RSS, Alpha Vantage, Reddit, StockTwits, Twitter. These can be wrapped to also write to the datalake.
- **AOTC-Signals plan** (`AOTC-Signals-plan.md`) — full architecture doc. Sections 1-3 (context, architecture, phases) still relevant for understanding the ingester layer.

## For parallel agents

Each of these is independent work:

1. **Parser-classifier code** — build the Python pipeline in `parser-classifier/`. Read raw JSON, extract text, call LLM for classification, write to `events.classified`. Use the column definitions in `002_classified.sql` as the contract.

2. **Datalake ingestion adapters** — wire existing `scripts/ingest/` ingesters to also write raw JSON to `$DB_BASE/events/raw/` and catalog in `events.raw`. Or build new ingesters that target the datalake directly.

3. **Historical backfill** — pull historical news via Alpha Vantage backfill or replay existing `news.articles` rows into the events pipeline.

4. **Backtest framework** — query `events.classified` + `signals.price_cache`, compute event→price reaction, aggregate by event_type/ticker/sector.
