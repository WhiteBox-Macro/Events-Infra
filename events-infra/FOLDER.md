# events-infra/

Two-layer architecture: **raw → classified**. Downstream agents extend `events.classified` with their own columns via ALTER TABLE.

## Layers

### 1. Datalake → `events.raw`
Raw messages exactly as received. Append-only JSON on disk at `$DB_BASE/events/raw/`. Immutable. Indexed in PostgreSQL via `events.raw`.

### 2. Parser-Classifier → `events.classified`
Single flat table. One row per classified event. Parser-classifier reads raw JSON, extracts text, classifies event type / tone / magnitude / markets / tickers, writes one structured row. Downstream modeling/backtest agents append their own columns later.

## `events.classified` column groups (post-migration-009 unified shape)

| Group | Columns | What it captures |
|-------|---------|-----------------|
| Source | source_channel, publish_time, headline, text_content | Identity + content |
| Taxonomy | event_category (14-label bucket), event_type (30-label fine), event_outcome (sub: beat/miss/hike/cut/...), is_regular | What happened |
| Sentiment | tone, magnitude, confidence | LLM-reported text sentiment + self-assessed quality |
| Affected entities | primary_ticker (any), ticker_impacts JSONB (universe-only, max 3, with role), sector (single nullable), impact_markets[] (8-value enum), countries[] | Who's affected |
| Scheduled-release block | indicator_name, consensus_value, actual_value, surprise, surprise_z, reporting_period | For is_regular=TRUE only |
| Dedup & chains | dedup_cluster_id, cluster_sequence, related_event_id | Avoid double-counting |
| Provenance | classified_by (e.g. "sonnet-4.6/unified-v2"), classifier_version, classified_at, raw_classification JSONB, metadata | Audit + future re-extraction without re-classifying |

Migration history: 002 created the table; 003 added structural tags; 007 added UNIQUE on raw_id; 008 added the unified columns (event_outcome, ticker_impacts, sector, classifier_version, raw_classification); 009 renamed inferred_* → bare names + dropped the superseded multi-column entity surface (tickers/primary_ticker (kept!)/sectors/primary_sector/ticker_impact_weights/sector_impact/sub_category/tags_*).

## Adjacent Schemas

| Schema | Tables | Purpose |
|--------|--------|---------|
| `events` | `raw`, `classified` | Pipeline 1 output (see above) |
| `signals` | `gate_params` | Per-(event_category, ticker) decision-gate parameters consumed by `backtest/sonnet_event_strategy.py::decide_trade` via `GateParamsRegistry` (migration 004) |
| `public` | `schema_migrations` | Tracks applied migrations (version, applied_at, checksum) — populated by `scripts/apply_migrations.py` |

## Top-Level Scripts

| Script | Role |
|--------|------|
| `scripts/apply_migrations.py` | Apply pending `.sql` files under `db/migrations/` with checksum drift protection |
| `scripts/ingest_x_twitterapi_io.py` | TwitterAPI.io bulk-search ingester (locked SOP) |
| `scripts/ingest_x_archive.py` | Official X API archive (ID backfill) |
| `scripts/catalog_tpio_datalake.py` | Catalog TPIO datalake JSONs into `events.raw` |

Retired in 2026-05-18 unified-classification refactor (no longer in tree):
- `scripts/backfill_structural_tags.py` (cache → PG bridge, obsolete now that classify.py writes the full structured object directly)
- `backtest/preclassify.py` (Stage-2 batched Sonnet pass, obsolete — Stage 1's prompt now produces structural tags too)
- `backtest/events_classified_cache.json` (JSON cache, obsolete — strategy reads from PG directly)
