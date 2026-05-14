#!/usr/bin/env python3
"""ingest_x_archive.py — Full-archive X (Twitter) historical ingester.

Pulls all tweets matching a query from the full-archive search endpoint
(/2/tweets/search/all), writes each as a raw JSON file to the datalake,
and catalogs in events.raw.

Usage:
    python ingest_x_archive.py --handle tradfi
    python ingest_x_archive.py --handle tradfi --since 2018-01-01 --until 2026-05-14
    python ingest_x_archive.py --handle tradfi --dry-run
    python ingest_x_archive.py --handle tradfi --include-replies --include-retweets

Requires TWITTER_BEARER_TOKEN in .env (pay-per-usage plan with full-archive access).
Costs $0.005 per tweet returned.
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

from dbkit import pg  # noqa: E402
from dbkit.constants import DB_BASE, load_dotenv_files  # noqa: E402
from dbkit.http import HttpRetryError, request_json  # noqa: E402

log = logging.getLogger("ingest_x_archive")

API_BASE = "https://api.twitter.com"
SEARCH_ALL_PATH = "/2/tweets/search/all"
MAX_RESULTS_PER_PAGE = 100
TWEET_FIELDS = "created_at,author_id,public_metrics,lang,in_reply_to_user_id,conversation_id,entities"
USER_FIELDS = "username,name,verified,public_metrics"
EXPANSIONS = "author_id"

DATALAKE_ROOT = DB_BASE / "events" / "raw" / "social" / "twitter"

# Rate limit: 300 req/15min = 1 req/3sec. Stay safely under.
REQUEST_INTERVAL_SEC = 3.5


def build_query(handle: str, *, include_replies: bool, include_retweets: bool) -> str:
    parts = [f"from:{handle}"]
    if not include_retweets:
        parts.append("-is:retweet")
    if not include_replies:
        parts.append("-is:reply")
    parts.append("lang:en")
    return " ".join(parts)


def datalake_path(tweet_id: str, published_at: datetime) -> Path:
    return DATALAKE_ROOT / published_at.strftime("%Y/%m/%d") / f"{tweet_id}.json"


def write_datalake_file(tweet_id: str, published_at: datetime, tweet: dict) -> tuple[Path, str, int]:
    path = datalake_path(tweet_id, published_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "_meta": {
            "source_name": "twitter",
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


def catalog_in_events_raw(tweet_id: str, published_at: datetime, file_path: Path,
                          file_hash: str, file_size: int, tweet: dict) -> None:
    rel_path = str(file_path.relative_to(DB_BASE / "events" / "raw"))
    metrics = tweet.get("public_metrics") or {}
    pg.upsert(
        "events.raw",
        {
            "source_type": "social",
            "source_channel": "twitter",
            "external_id": tweet_id,
            "file_path": rel_path,
            "file_hash": file_hash,
            "file_size_bytes": file_size,
            "published_at": published_at,
            "metadata": json.dumps({
                "author_id": tweet.get("author_id"),
                "lang": tweet.get("lang"),
                "like_count": metrics.get("like_count"),
                "retweet_count": metrics.get("retweet_count"),
                "reply_count": metrics.get("reply_count"),
                "quote_count": metrics.get("quote_count"),
                "impression_count": metrics.get("impression_count"),
            }),
        },
        conflict_on=["source_channel", "external_id"],
    )


def parse_tweet_time(created_at: str) -> datetime:
    return datetime.fromisoformat(created_at.replace("Z", "+00:00"))


def fetch_page(bearer: str, query: str, *, start_time: str | None,
               end_time: str | None, next_token: str | None) -> dict:
    params = {
        "query": query,
        "max_results": str(MAX_RESULTS_PER_PAGE),
        "tweet.fields": TWEET_FIELDS,
        "expansions": EXPANSIONS,
        "user.fields": USER_FIELDS,
    }
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    if next_token:
        params["next_token"] = next_token

    headers = {"Authorization": f"Bearer {bearer}"}
    return request_json(
        "GET", API_BASE + SEARCH_ALL_PATH,
        params=params, headers=headers, timeout=30,
        max_attempts=5, base_delay=4.0, max_delay=60.0,
    )


def run(args: argparse.Namespace) -> int:
    bearer = os.environ.get("TWITTER_BEARER_TOKEN", "").strip()
    if not bearer:
        log.error("TWITTER_BEARER_TOKEN not set")
        return 1

    query = build_query(
        args.handle,
        include_replies=args.include_replies,
        include_retweets=args.include_retweets,
    )
    log.info("query: %s", query)
    log.info("range: %s → %s", args.since, args.until)

    start_time = f"{args.since}T00:00:00Z" if args.since else None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.until and args.until >= today:
        end_time = None
    else:
        end_time = f"{args.until}T23:59:59Z" if args.until else None

    total_tweets = 0
    total_pages = 0
    next_token = None
    oldest_seen = None
    newest_seen = None

    while True:
        try:
            resp = fetch_page(bearer, query, start_time=start_time,
                              end_time=end_time, next_token=next_token)
        except (HttpRetryError, Exception) as e:
            log.error("fetch failed after retries: %s", e)
            break

        tweets = resp.get("data") or []
        meta = resp.get("meta") or {}
        result_count = meta.get("result_count", 0)

        if not tweets:
            log.info("no more results")
            break

        total_pages += 1
        users_list = (resp.get("includes") or {}).get("users") or []
        users_by_id = {u["id"]: u for u in users_list if u.get("id")}

        for tweet in tweets:
            tweet_id = tweet.get("id")
            created_at = tweet.get("created_at")
            if not tweet_id or not created_at:
                continue

            published_at = parse_tweet_time(created_at)
            if oldest_seen is None or published_at < oldest_seen:
                oldest_seen = published_at
            if newest_seen is None or published_at > newest_seen:
                newest_seen = published_at

            author = users_by_id.get(tweet.get("author_id")) or {}
            tweet["_resolved_author"] = {
                "username": author.get("username"),
                "name": author.get("name"),
                "verified": author.get("verified"),
                "followers": (author.get("public_metrics") or {}).get("followers_count"),
            }

            if args.dry_run:
                text_preview = (tweet.get("text") or "")[:80]
                log.info("[DRY] %s  %s  %s", published_at.strftime("%Y-%m-%d"), tweet_id, text_preview)
            else:
                path, file_hash, file_size = write_datalake_file(tweet_id, published_at, tweet)
                catalog_in_events_raw(tweet_id, published_at, path, file_hash, file_size, tweet)

            total_tweets += 1

        log.info("page %d: %d tweets (total: %d, range: %s → %s)",
                 total_pages, result_count, total_tweets,
                 oldest_seen.strftime("%Y-%m-%d") if oldest_seen else "?",
                 newest_seen.strftime("%Y-%m-%d") if newest_seen else "?")

        next_token = meta.get("next_token")
        if not next_token:
            log.info("pagination complete")
            break

        time.sleep(REQUEST_INTERVAL_SEC)

    est_cost = total_tweets * 0.005
    log.info("done: %d tweets across %d pages, est. cost: $%.2f",
             total_tweets, total_pages, est_cost)
    if args.dry_run:
        log.info("[DRY RUN — nothing written to disk or DB]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="X full-archive historical ingester")
    parser.add_argument("--handle", required=True, help="X handle to ingest (without @)")
    parser.add_argument("--since", default="2018-01-01", help="Start date YYYY-MM-DD (default: 2018-01-01)")
    parser.add_argument("--until", default=None, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--include-replies", action="store_true", help="Include replies (excluded by default)")
    parser.add_argument("--include-retweets", action="store_true", help="Include retweets (excluded by default)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and log but don't write to disk/DB")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.until:
        args.until = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
