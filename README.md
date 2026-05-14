# AOTC-Signals

News / macro / social ingestion + derived signals. Sister repo to
[AOTC-DB](../AOTC-DB) (earnings-call ingestion). Shares the same Postgres
instance; lives in separate schemas (`news`, `macro`, `social`, `signals`).

## Why a separate repo

AOTC-DB produces quarterly facts (earnings calls, transcripts, filings).
This repo produces continuous signals (news, macro indicators, sentiment).
Different cadences, different dependencies, independent CI/deploy.

A third service (`aotc-trader`, future) reads from both and makes trading
decisions.

## Setup

```bash
cp .env.example .env
# Edit DATABASE_URL if your Postgres isn't on localhost
# Edit DB_BASE to wherever you want raw payloads stored

pip install -e ".[dev]"

# Apply migrations (creates signals_meta, news schemas)
python scripts/apply_migrations.py

# Verify it works — connects to DB, queries securities, queries news.sources
python scripts/hello_db.py
```

## Layout

```
aotc-signals/
├── dbkit/                  Postgres pool + filesystem helpers (copied from AOTC-DB)
├── db/migrations/          Numbered SQL migrations (signals_meta.applied_migrations tracks state)
├── scripts/
│   ├── apply_migrations.py Migration runner
│   ├── hello_db.py         Connection verifier
│   ├── ingest/             News/macro/social ingesters (RSS, APIs, social feeds)
│   ├── enrichment/         Ticker tagging, sentiment scoring, embeddings
│   └── aggregation/        Roll-ups → signals.* tables
└── tests/
```

## Schemas

| Schema | Purpose |
|--------|---------|
| `signals_meta` | Migration tracking + shared helper functions for this repo |
| `news` | Articles + sources (RSS, APIs, scrapes) |
| `macro` | Macro indicators + economic releases (future) |
| `social` | Twitter/Reddit/StockTwits posts (future) |
| `signals` | Derived per-ticker scores, regime flags (future) |

Cross-joins to AOTC-DB tables (`stock_os.securities`, `ir.events`, etc.)
work natively — same database, different schemas.
