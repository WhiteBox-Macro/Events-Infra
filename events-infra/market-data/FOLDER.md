# market-data/

Market price/response data for backtesting event→reaction alpha.

## Storage layout

```
$DB_BASE/events/market_data/
├── {TICKER}/
│   ├── 1m/
│   │   └── {TICKER}_1m_{YYYY-MM}.parquet
│   ├── 1h/
│   │   └── {TICKER}_1h_{YYYY-MM}.parquet
│   └── 1d/
│       └── {TICKER}_1d.parquet
```

## Data sources

| Source | Intervals | History | Cost |
|--------|-----------|---------|------|
| yfinance | 1m (7d), 1h (730d), 1d (full) | Limited by Yahoo API | Free |
| Polygon.io | 1m+ full history | Full | $99/mo |
| IBKR API | 1s+ | Full with account | Account required |

## Parquet schema

All intervals share the same column layout:
- `timestamp` (datetime64[ns, UTC])
- `open`, `high`, `low`, `close` (float64)
- `volume` (int64)
- `ticker` (string)
- `interval` (string)
