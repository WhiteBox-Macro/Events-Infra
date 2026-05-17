# events-infra/

Two-layer architecture: **raw → classified**. Downstream agents extend `events.classified` with their own columns via ALTER TABLE.

## Layers

### 1. Datalake → `events.raw`
Raw messages exactly as received. Append-only JSON on disk at `$DB_BASE/events/raw/`. Immutable. Indexed in PostgreSQL via `events.raw`.

### 2. Parser-Classifier → `events.classified`
Single flat table. One row per classified event. Parser-classifier reads raw JSON, extracts text, classifies event type / tone / magnitude / markets / tickers, writes one structured row. Downstream modeling/backtest agents append their own columns later.

## `events.classified` column groups

| Group | Columns | What it captures |
|-------|---------|-----------------|
| Source | source_channel, publish_time, headline, text_content | Identity + content |
| Classification | is_regular, event_type, inferred_tone, inferred_magnitude | What happened |
| Market scope | inferred_impact_markets[], tickers[], sectors[], countries[] | Who's affected |
| Scheduled fields | indicator_name, consensus_value, actual_value, surprise, surprise_z | For is_regular=TRUE only |
| Dedup & chains | dedup_cluster_id, cluster_sequence, related_event_id | Avoid double-counting |
| Structural tags | event_category, sub_category, sector_impact[], ticker_impact_weights JSONB, tags_version, tags_classified_at | 14-label taxonomy + per-ticker impact weights consumed by backtest strategy (migration 003) |
| Metadata | classified_by, classification_confidence, metadata | Provenance |

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
| `scripts/backfill_structural_tags.py` | One-shot: copy structural tags from `events_classified_cache.json` into the new PG columns |
| `scripts/ingest_x_twitterapi_io.py` | TwitterAPI.io bulk-search ingester (locked SOP) |
| `scripts/ingest_x_archive.py` | Official X API archive (ID backfill) |
| `scripts/catalog_tpio_datalake.py` | Catalog TPIO datalake JSONs into `events.raw` |
