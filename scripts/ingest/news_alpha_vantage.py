#!/usr/bin/env python3
"""news_alpha_vantage.py — Alpha Vantage NEWS_SENTIMENT ingester.

Two modes:

  Live (default): poll every poll_interval_sec, time_from = max(last_polled_at,
                  now - lookback). Newest articles arrive within a minute or
                  two of publication (AV-side latency).

  Backfill:       walk a date range in fixed-size windows. Use this to seed
                  history for backtest before going live.

      python scripts/ingest/news_alpha_vantage.py --from 2025-01-01 --to 2026-05-13

Alpha Vantage rate limits are tight: free = 25 calls/day; premium tiers go up
to 75 calls/minute. The ingester sleeps between API calls and surfaces 429s
as HttpRetryError (handled by dbkit/http.py backoff).

We pre-fill enrichment columns from AV's own pre-tagged sentiment + tickers:
the fast path can use these immediately without an LLM call.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import load_dotenv_files  # noqa: E402
from dbkit.http import request_json  # noqa: E402
from scripts.ingest._common import (  # noqa: E402
    get_source_by_name,
    mark_source_polled,
    persist_raw_payload,
    poll_loop,
    setup_logging,
    sha256_id,
    singleton_lock,
    stop_event,
)
from scripts.ingest._normalize import (  # noqa: E402
    dedupe_news_items,
    extract_av_tickers,
    normalize_av_news_item,
)

INGESTER_NAME = "news_alpha_vantage"
SOURCE_NAME = "alpha_vantage_news"  # must match seed row
AV_URL = "https://www.alphavantage.co/query"
AV_PER_PAGE_MAX = 1000  # AV NEWS_SENTIMENT limit param max
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_BACKFILL_WINDOW_HOURS = 24


def _av_call(*, tickers: str | None, topics: str | None,
             time_from: datetime, time_to: datetime | None,
             limit: int, sort: str = "LATEST") -> dict:
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise RuntimeError("ALPHA_VANTAGE_API_KEY not set")

    params: dict = {
        "function": "NEWS_SENTIMENT",
        "apikey": api_key,
        "sort": sort,
        "limit": str(min(limit, AV_PER_PAGE_MAX)),
        "time_from": time_from.strftime("%Y%m%dT%H%M"),
    }
    if time_to is not None:
        params["time_to"] = time_to.strftime("%Y%m%dT%H%M")
    if tickers:
        params["tickers"] = tickers
    if topics:
        params["topics"] = topics

    payload = request_json("GET", AV_URL, params=params, timeout=30, max_attempts=4)

    # AV embeds rate-limit messages in the JSON body (200 OK with an error
    # string). Surface those so the loop backs off.
    if isinstance(payload, dict) and "Note" in payload:
        raise RuntimeError(f"Alpha Vantage rate limited: {payload['Note']}")
    if isinstance(payload, dict) and "Information" in payload and "feed" not in payload:
        raise RuntimeError(f"Alpha Vantage info-only response: {payload['Information']}")
    return payload


def _ingest_window(source_id: int, *, time_from: datetime, time_to: datetime | None,
                   tickers: str | None, topics: str | None, log) -> int:
    """Fetch one window and upsert any new rows. Returns count inserted."""
    payload = _av_call(
        tickers=tickers,
        topics=topics,
        time_from=time_from,
        time_to=time_to,
        limit=AV_PER_PAGE_MAX,
    )
    feed = payload.get("feed") if isinstance(payload, dict) else None
    if not isinstance(feed, list):
        return 0

    normalised = []
    for item in feed:
        if not isinstance(item, dict):
            continue
        norm = normalize_av_news_item(item)
        if norm:
            normalised.append(norm)
    items = dedupe_news_items(normalised)

    inserted = 0
    for item in items:
        external_id = item.get("url") or sha256_id(f"{item['title']}::{item['publisher']}")
        raw_path = persist_raw_payload(SOURCE_NAME, external_id, item)

        row = {
            "source_id": source_id,
            "external_id": external_id,
            "url": item.get("url") or "",
            "title": item["title"],
            "summary": item.get("summary") or None,
            "author": ", ".join(item.get("authors") or []) or None,
            "body_path": raw_path,
            "published_at": item["time_published"],
            "language": None,
            # AV gives us pre-tagged tickers + sentiment_score; capture both so
            # the fast path can act without re-running enrichment.
            "tickers": extract_av_tickers(item),
            "categories": [t["topic"] for t in item.get("topics", [])],
            "sentiment_score": item.get("overall_sentiment_score"),
            "sentiment_model": "alpha_vantage_news_sentiment",
            "metadata": {
                "av_publisher": item.get("publisher"),
                "av_sentiment_label": item.get("overall_sentiment_label"),
                "av_ticker_sentiment": item.get("ticker_sentiment"),
                "av_topics": item.get("topics"),
                "av_banner_image": item.get("banner_image"),
            },
        }
        try:
            pg.upsert("news.articles", row, conflict_on=["source_id", "external_id"])
            inserted += 1
        except Exception:
            log.exception("upsert failed for %s", external_id)
    if inserted:
        log.info("ingested %d articles for window %s -> %s", inserted, time_from, time_to)
    return inserted


def cmd_live(args) -> int:
    log = setup_logging(INGESTER_NAME)
    source = get_source_by_name(SOURCE_NAME)
    if not source:
        log.error("news.sources row '%s' missing; load seeds/news_sources.sql first", SOURCE_NAME)
        return 2

    lookback = timedelta(hours=int(os.environ.get("AV_NEWS_LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS)))
    tickers_env = os.environ.get("AV_NEWS_TICKERS") or None
    topics_env = os.environ.get("AV_NEWS_TOPICS") or "financial_markets,economy_macro,economy_monetary"

    def _tick():
        # Pull from max(last_polled, now-lookback) → now. last_polled grows so
        # subsequent calls request a tighter window — keeps API budget low.
        last = source.get("last_polled_at")
        now = datetime.now(timezone.utc)
        time_from = max(now - lookback, last) if last else (now - lookback)
        _ingest_window(
            source["id"],
            time_from=time_from,
            time_to=None,
            tickers=tickers_env,
            topics=topics_env if not tickers_env else None,
            log=log,
        )
        # Refresh in-memory source so we use the latest last_polled_at next tick.
        mark_source_polled(source["id"])
        refreshed = get_source_by_name(SOURCE_NAME)
        if refreshed:
            source.update(refreshed)

    with singleton_lock(INGESTER_NAME):
        poll_loop(
            name=INGESTER_NAME,
            tick_fn=_tick,
            tick_interval_sec=max(15.0, float(source.get("poll_interval_sec") or 60)),
        )
    return 0


def cmd_backfill(args) -> int:
    log = setup_logging(INGESTER_NAME)
    source = get_source_by_name(SOURCE_NAME)
    if not source:
        log.error("news.sources row '%s' missing; load seeds/news_sources.sql first", SOURCE_NAME)
        return 2

    start = datetime.fromisoformat(args.from_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.to_date).replace(tzinfo=timezone.utc)
    if start >= end:
        log.error("--from must be earlier than --to")
        return 2

    window_hours = int(args.window_hours)
    tickers = args.tickers or os.environ.get("AV_NEWS_TICKERS") or None
    topics = args.topics or os.environ.get("AV_NEWS_TOPICS") or "financial_markets,economy_macro,economy_monetary"

    with singleton_lock(INGESTER_NAME + ".backfill"):
        cursor = start
        total = 0
        while cursor < end and not stop_event.is_set():
            window_end = min(cursor + timedelta(hours=window_hours), end)
            try:
                total += _ingest_window(
                    source["id"],
                    time_from=cursor,
                    time_to=window_end,
                    tickers=tickers,
                    topics=topics if not tickers else None,
                    log=log,
                )
            except Exception:
                log.exception("backfill window %s -> %s failed; skipping", cursor, window_end)
            cursor = window_end
        log.info("backfill complete: %d articles across %s -> %s", total, start, end)
    return 0


def main() -> int:
    load_dotenv_files()
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    live = sub.add_parser("live", help="continuous polling (default)")
    bf = sub.add_parser("backfill", help="walk a historical date range")
    bf.add_argument("--from", dest="from_date", required=True, help="ISO date, e.g. 2025-01-01")
    bf.add_argument("--to", dest="to_date", required=True, help="ISO date, e.g. 2026-05-13")
    bf.add_argument("--window-hours", default=DEFAULT_BACKFILL_WINDOW_HOURS,
                    help=f"window size per AV call (default {DEFAULT_BACKFILL_WINDOW_HOURS}h)")
    bf.add_argument("--tickers", help="comma-separated tickers; overrides topics")
    bf.add_argument("--topics", help="AV topics; default is macro/markets")

    args = ap.parse_args()
    if args.cmd == "backfill":
        return cmd_backfill(args)
    return cmd_live(args)


if __name__ == "__main__":
    sys.exit(main())
