#!/usr/bin/env python3
"""ingest_x_twitterapi_io.py — Historical X ingester via TwitterAPI.io.

Uses the third-party TwitterAPI.io service ($0.15/1000 tweets vs $5/1000 official).
Stores in a separate datalake subfolder for comparison.

Usage:
    python ingest_x_twitterapi_io.py --handle tradfi
    python ingest_x_twitterapi_io.py --handle tradfi --since 2024-10-01 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit.constants import DB_BASE, load_dotenv_files  # noqa: E402
from dbkit.http import HttpRetryError, request_json  # noqa: E402

log = logging.getLogger("ingest_x_twitterapiio")

API_BASE = "https://api.twitterapi.io"
SEARCH_PATH = "/twitter/tweet/advanced_search"

DATALAKE_ROOT = DB_BASE / "events" / "raw" / "social" / "twitter_twitterapiio"

REQUEST_INTERVAL_SEC = 0.3  # 20 QPS allowed, stay well under


def build_query(handle: str, *, since_ts: int | None, until_ts: int | None) -> str:
    parts = [f"from:{handle}", "-filter:replies", "-filter:retweets"]
    if since_ts:
        parts.append(f"since_time:{since_ts}")
    if until_ts:
        parts.append(f"until_time:{until_ts}")
    return " ".join(parts)


def parse_tweet_time(created_at: str) -> datetime:
    return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")


def datalake_path(tweet_id: str, published_at: datetime) -> Path:
    return DATALAKE_ROOT / published_at.strftime("%Y/%m/%d") / f"{tweet_id}.json"


def write_datalake_file(tweet_id: str, published_at: datetime, tweet: dict) -> tuple[Path, str, int]:
    path = datalake_path(tweet_id, published_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "_meta": {
            "source_name": "twitter_twitterapiio",
            "source_type": "social",
            "external_id": tweet_id,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "ingester_version": "1.0.0",
        },
        "payload": tweet,
    }
    content = json.dumps(envelope, ensure_ascii=False, default=str)
    path.write_text(content, encoding="utf-8")
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    return path, file_hash, len(content.encode())


def fetch_page(api_key: str, query: str) -> dict:
    headers = {"x-api-key": api_key}
    params = {"queryType": "Latest", "query": query}
    return request_json(
        "GET", API_BASE + SEARCH_PATH,
        params=params, headers=headers, timeout=30,
        max_attempts=5, base_delay=2.0, max_delay=30.0,
    )


def date_to_unix(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("TWITTERAPIIO_API_KEY", "").strip()
    if not api_key:
        log.error("TWITTERAPIIO_API_KEY not set")
        return 1

    since_ts = date_to_unix(args.since) if args.since else None
    until_ts = date_to_unix(args.until) if args.until else None

    log.info("handle: @%s, since: %s, until: %s", args.handle, args.since, args.until or "now")

    total_tweets = 0
    total_pages = 0
    seen_ids = set()
    oldest_seen = None
    newest_seen = None

    while True:
        query = build_query(args.handle, since_ts=since_ts, until_ts=until_ts)

        try:
            resp = fetch_page(api_key, query)
        except (HttpRetryError, Exception) as e:
            log.error("fetch failed: %s", e)
            break

        tweets = resp.get("tweets") or []

        if not tweets:
            log.info("no more results")
            break

        total_pages += 1
        earliest_ts_this_page = None
        page_new = 0

        for tweet in tweets:
            tweet_id = tweet.get("id")
            if not tweet_id or tweet_id in seen_ids:
                continue
            if tweet.get("isRetweet"):
                continue

            created_at = tweet.get("createdAt")
            if not created_at:
                continue

            published_at = parse_tweet_time(created_at)
            tweet_ts = int(published_at.timestamp())

            if since_ts and tweet_ts < since_ts:
                continue

            seen_ids.add(tweet_id)

            if oldest_seen is None or published_at < oldest_seen:
                oldest_seen = published_at
            if newest_seen is None or published_at > newest_seen:
                newest_seen = published_at

            if earliest_ts_this_page is None or tweet_ts < earliest_ts_this_page:
                earliest_ts_this_page = tweet_ts

            if args.dry_run:
                text_preview = (tweet.get("text") or "")[:80]
                log.info("[DRY] %s  %s  %s", published_at.strftime("%Y-%m-%d"), tweet_id, text_preview)
            else:
                write_datalake_file(tweet_id, published_at, tweet)

            page_new += 1
            total_tweets += 1

        log.info("page %d: %d new tweets (total: %d, range: %s -> %s)",
                 total_pages, page_new,  total_tweets,
                 oldest_seen.strftime("%Y-%m-%d") if oldest_seen else "?",
                 newest_seen.strftime("%Y-%m-%d") if newest_seen else "?")

        if page_new == 0 or earliest_ts_this_page is None:
            log.info("no new tweets on this page, stopping")
            break

        until_ts = earliest_ts_this_page - 1

        if since_ts and until_ts <= since_ts:
            log.info("reached since boundary")
            break

        time.sleep(REQUEST_INTERVAL_SEC)

    est_cost = total_tweets * 0.00015
    log.info("done: %d tweets across %d pages, est. cost: $%.4f",
             total_tweets, total_pages, est_cost)
    if args.dry_run:
        log.info("[DRY RUN — nothing written to disk]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="X historical ingester via TwitterAPI.io")
    parser.add_argument("--handle", required=True, help="X handle (without @)")
    parser.add_argument("--since", default="2024-10-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--until", default=None, help="Resume from date YYYY-MM-DD (slide backwards from here)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    env = load_dotenv_files()
    for k, v in env.items():
        os.environ.setdefault(k, v)

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
