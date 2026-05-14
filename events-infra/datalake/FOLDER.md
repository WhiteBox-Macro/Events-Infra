# datalake/

Specification for the raw message datalake. At runtime, files live at `$DB_BASE/events/raw/` (not in the repo).

## Runtime folder layout

```
$DB_BASE/events/raw/
├── rss/
│   ├── sec_edgar_8k/{yyyy}/{mm}/{dd}/{guid}.json
│   ├── sec_edgar_all/{yyyy}/{mm}/{dd}/{guid}.json
│   ├── fed_press_monetary/{yyyy}/{mm}/{dd}/{guid}.json
│   ├── fed_press_all/{yyyy}/{mm}/{dd}/{guid}.json
│   ├── treasury_press/{yyyy}/{mm}/{dd}/{guid}.json
│   └── marketwatch/{yyyy}/{mm}/{dd}/{guid}.json
├── api/
│   ├── alpha_vantage_news/{yyyy}/{mm}/{dd}/{batch_ts}.json
│   └── polygon_news/{yyyy}/{mm}/{dd}/{article_id}.json
├── social/
│   ├── reddit/{subreddit}/{yyyy}/{mm}/{dd}/{post_id}.json
│   ├── stocktwits/{yyyy}/{mm}/{dd}/{message_id}.json
│   └── twitter/{yyyy}/{mm}/{dd}/{tweet_id}.json
└── macro/
    └── alpha_vantage/{yyyy}/{mm}/{dd}/{indicator_code}_{period_start}.json
```

## File format

Every file is a JSON object with two top-level keys:

```json
{
  "_meta": {
    "source_name": "sec_edgar_8k",
    "source_type": "rss",
    "external_id": "urn:tag:sec.gov,2026:0001234567-26-000042",
    "ingested_at": "2026-05-14T08:30:00Z",
    "ingester_version": "1.0.0"
  },
  "payload": {
    // ... raw API/feed response, verbatim
  }
}
```

## Rules

1. **Immutable.** Never modify a file after write. Corrections go in a new file.
2. **Idempotent.** Re-ingesting the same external_id overwrites the same path (same content).
3. **Complete.** The `payload` contains the full API response, not a subset.
4. **Date-partitioned.** `{yyyy}/{mm}/{dd}` is the ingestion date (UTC), not the published date.
5. **Indexed.** Every file gets a corresponding `events.raw_messages` row with `file_path` pointing here.
