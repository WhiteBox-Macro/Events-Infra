#!/usr/bin/env python3
"""hello_db.py — smoke test for the AOTC-Signals ↔ AOTC-DB shared Postgres.

Verifies:
  1. DATABASE_URL is set and reachable
  2. We can read from AOTC-DB's stock_os.securities (cross-repo join works)
  3. signals_meta.applied_migrations exists (this repo's migrations ran)
  4. news.sources exists and is queryable

Run after `python scripts/apply_migrations.py`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbkit import pg
from dbkit.constants import load_dotenv_files


def check_database_url() -> bool:
    if not os.environ.get("DATABASE_URL"):
        print("FAIL  DATABASE_URL not set. Copy .env.example to .env and edit.")
        return False
    print(f"OK    DATABASE_URL set")
    return True


def check_securities() -> bool:
    try:
        rows = pg.execute(
            "SELECT ticker, mic, name, market_cap_usd "
            "FROM stock_os.securities "
            "WHERE ticker IS NOT NULL "
            "ORDER BY market_cap_usd DESC NULLS LAST "
            "LIMIT 5"
        )
    except Exception as e:
        print(f"FAIL  Could not read stock_os.securities: {e}")
        print("      (Is AOTC-DB's Postgres running? Have its init scripts run?)")
        return False
    if not rows:
        print("WARN  stock_os.securities is empty — AOTC-DB hasn't been seeded yet")
        return True
    print(f"OK    Read {len(rows)} rows from stock_os.securities. Top 5 by market cap:")
    for r in rows:
        mcap_b = (r["market_cap_usd"] or 0) / 1e11  # cents → billions
        print(f"        {r['ticker']:<8} {r['mic'] or '-':<6} ${mcap_b:>6.1f}B  {r['name']}")
    return True


def check_migrations_applied() -> bool:
    try:
        rows = pg.execute(
            "SELECT filename, applied_at FROM signals_meta.applied_migrations "
            "ORDER BY filename"
        )
    except Exception as e:
        print(f"FAIL  signals_meta.applied_migrations missing: {e}")
        print("      Run: python scripts/apply_migrations.py")
        return False
    print(f"OK    {len(rows)} migration(s) applied:")
    for r in rows:
        print(f"        {r['filename']}  ({r['applied_at']:%Y-%m-%d %H:%M})")
    return True


def check_news_schema() -> bool:
    try:
        n_sources = pg.count("news.sources")
        n_articles = pg.count("news.articles")
    except Exception as e:
        print(f"FAIL  news schema not queryable: {e}")
        return False
    print(f"OK    news.sources has {n_sources} row(s), news.articles has {n_articles} row(s)")
    return True


def main() -> int:
    load_dotenv_files()
    print("AOTC-Signals — DB connectivity check\n")
    checks = [
        ("DATABASE_URL",       check_database_url),
        ("stock_os.securities (AOTC-DB)", check_securities),
        ("signals_meta.applied_migrations", check_migrations_applied),
        ("news schema",        check_news_schema),
    ]
    ok = True
    for name, fn in checks:
        print(f"\n[{name}]")
        if not fn():
            ok = False
    print("\n" + ("All checks passed." if ok else "Some checks failed — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
