#!/usr/bin/env python3
"""Backfill events.classified structural-tag columns from the existing
events_classified_cache.json. One-shot tool to close the gap between the
pre-migration cache and the post-migration PG columns.

Idempotent: WHERE event_category IS NULL — re-runs skip already-backfilled
rows.

Usage:
    python backfill_structural_tags.py             # dry-run (default)
    python backfill_structural_tags.py --apply     # actually write
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import load_dotenv_files  # noqa: E402

log = logging.getLogger("backfill_structural_tags")

CACHE_PATH = (Path(__file__).resolve().parent.parent
              / "backtest" / "events_classified_cache.json")


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        log.error("cache file missing: %s", CACHE_PATH)
        sys.exit(1)
    with open(CACHE_PATH, encoding="utf-8") as f:
        return json.load(f)


def update_one(event_id: str, tag: dict) -> bool:
    """UPDATE one row only if event_category is still NULL (idempotent).

    Bypasses pg.execute() because it auto-wraps Python lists with Json(),
    which breaks TEXT[] columns. Uses a raw cursor so psycopg2 adapts
    list -> TEXT[] natively.
    """
    with pg.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events.classified
                SET event_category        = %s,
                    sub_category          = %s,
                    sector_impact         = %s,
                    ticker_impact_weights = %s::jsonb,
                    tags_classified_at    = now()
                WHERE event_id = %s
                  AND event_category IS NULL
                RETURNING event_id
                """,
                (
                    tag.get("event_category"),
                    tag.get("sub_category"),
                    list(tag.get("sector_impact") or []),
                    json.dumps(tag.get("ticker_impact_weights") or {}),
                    event_id,
                ),
            )
            return cur.rowcount > 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill structural tags from JSON cache.")
    ap.add_argument("--apply", action="store_true", help="Actually write (default: dry-run)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    load_dotenv_files(str(REPO_ROOT / ".env"))

    cache = load_cache()
    log.info("cache has %d entries", len(cache))

    # How many rows in PG are still NULL?
    missing_rows = pg.execute(
        "SELECT COUNT(*) AS n FROM events.classified WHERE event_category IS NULL"
    )
    missing_count = missing_rows[0]["n"] if missing_rows else 0
    log.info("events.classified rows with NULL event_category: %d", missing_count)

    # How many of those have a cache entry?
    cached_eids = set(cache.keys())
    cached_rows = pg.execute(
        "SELECT event_id FROM events.classified WHERE event_category IS NULL"
    )
    cached_db_eids = {str(r["event_id"]) for r in cached_rows}
    overlap = cached_eids & cached_db_eids
    log.info("of which %d have a cache entry available to backfill", len(overlap))

    if not args.apply:
        log.info("dry-run: would update %d rows. Re-run with --apply to write.", len(overlap))
        return 0

    updated = 0
    skipped = 0
    for eid in overlap:
        tag = cache[eid]
        if update_one(eid, tag):
            updated += 1
        else:
            skipped += 1

    log.info("done. updated=%d skipped=%d", updated, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
