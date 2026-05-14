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
| Metadata | classified_by, classification_confidence, metadata | Provenance |
