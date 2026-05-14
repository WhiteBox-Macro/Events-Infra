#!/usr/bin/env python3
"""social_reddit.py — Reddit search ingester.

Reads tickers from signals.watchlist (active rows), queries each across a
fixed set of finance subreddits, and writes results to social.posts. Uses
Reddit's public JSON endpoint — no OAuth, no API key, ~10 req/min IP limit.

The fetch pattern is borrowed from TauricResearch/TradingAgents
tradingagents/dataflows/reddit.py — same throttle (0.4s between subreddit
calls), same defensive parse, same User-Agent.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import load_dotenv_files  # noqa: E402
from dbkit.http import HttpRetryError, request_json  # noqa: E402
from scripts.ingest._common import (  # noqa: E402
    persist_raw_payload,
    poll_loop,
    setup_logging,
    singleton_lock,
    stop_event,
)

INGESTER_NAME = "social_reddit"
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing", "options", "SecurityAnalysis")
USER_AGENT = "AOTC-Signals/0.1 (contact@example.com)"
INTER_REQUEST_DELAY = 0.4   # seconds between subreddit calls; keeps us under ~10 req/min
PER_SUB_LIMIT = 10           # newest N posts per (ticker, subreddit) per tick
POLL_INTERVAL_SEC = 90       # full watchlist scan cadence


def _active_watchlist() -> list[str]:
    rows = pg.query("signals.watchlist", select=["ticker"], where={"active": True})
    return [r["ticker"] for r in rows]


def _fetch_subreddit(ticker: str, sub: str) -> list[dict]:
    url = f"https://www.reddit.com/r/{sub}/search.json"
    params = {
        "q": ticker,
        "restrict_sr": "on",
        "sort": "new",
        "t": "week",
        "limit": str(PER_SUB_LIMIT),
    }
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        payload = request_json("GET", url, params=params, headers=headers, timeout=10)
    except HttpRetryError:
        return []
    children = (payload.get("data") or {}).get("children") or []
    return [c.get("data", {}) for c in children if isinstance(c, dict)]


def _row_from_post(post: dict, ticker: str, sub: str) -> dict | None:
    title = (post.get("title") or "").strip()
    if not title:
        return None
    selftext = (post.get("selftext") or "").strip()
    body = selftext or title
    external_id = post.get("name") or post.get("id")
    if not external_id:
        return None
    posted_at_ts = post.get("created_utc")
    posted_at = datetime.fromtimestamp(posted_at_ts, tz=timezone.utc) if posted_at_ts else datetime.now(timezone.utc)
    return {
        "source": "reddit",
        "external_id": str(external_id),
        "author": post.get("author"),
        "author_followers": None,
        "parent_id": None,
        "channel": sub,
        "title": title,
        "body": body,
        "url": f"https://www.reddit.com{post.get('permalink', '')}",
        "posted_at": posted_at,
        "language": None,
        "score": post.get("score"),
        "comments": post.get("num_comments"),
        "reposts": None,
        # We do NOT assume the search match guarantees the ticker is mentioned
        # in the body — the enrichment layer will re-extract. But seeding the
        # column lets the dispatcher filter cheaply.
        "tickers": [ticker.upper()],
        "metadata": {
            "subreddit": sub,
            "flair": post.get("link_flair_text"),
            "over_18": post.get("over_18"),
            "search_ticker": ticker,
        },
    }


def _ingest_ticker(ticker: str, log) -> int:
    inserted = 0
    for i, sub in enumerate(DEFAULT_SUBREDDITS):
        if stop_event.is_set():
            return inserted
        if i > 0:
            time.sleep(INTER_REQUEST_DELAY)
        posts = _fetch_subreddit(ticker, sub)
        for post in posts:
            row = _row_from_post(post, ticker, sub)
            if not row:
                continue
            persist_raw_payload("reddit", row["external_id"], post)
            try:
                pg.upsert("social.posts", row, conflict_on=["source", "external_id"])
                inserted += 1
            except Exception:
                log.exception("reddit upsert failed for %s", row["external_id"])
    if inserted:
        log.info("%s: ingested %d reddit posts", ticker, inserted)
    return inserted


def tick() -> None:
    log = setup_logging(INGESTER_NAME)
    tickers = _active_watchlist()
    if not tickers:
        log.debug("watchlist is empty; nothing to fetch")
        return
    for ticker in tickers:
        if stop_event.is_set():
            return
        try:
            _ingest_ticker(ticker, log)
        except Exception:
            log.exception("reddit poll failed for %s", ticker)


def main() -> int:
    load_dotenv_files()
    setup_logging(INGESTER_NAME)
    with singleton_lock(INGESTER_NAME):
        poll_loop(name=INGESTER_NAME, tick_fn=tick, tick_interval_sec=POLL_INTERVAL_SEC)
    return 0


if __name__ == "__main__":
    sys.exit(main())
