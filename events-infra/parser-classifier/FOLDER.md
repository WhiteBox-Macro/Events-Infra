# parser-classifier/

Single-step pipeline: raw datalake JSON → `events.classified` row.

Combines mechanical parsing (text extraction, ticker regex, dedup) and LLM classification (event type, tone, magnitude, market scope, entity inference) in one pass.

## Pipeline contract

```
$DB_BASE/events/raw/{source}/{date}/{id}.json
        │
        ▼
events.raw (catalog entry: file pointer, dedup key)
        │
        ▼  parser-classifier
events.classified (one row: source + classification + scope + dedup)
        │
        ▼  downstream agents (separate work)
ALTER TABLE events.classified ADD COLUMN ... (modeling signals, features, scores)
```
