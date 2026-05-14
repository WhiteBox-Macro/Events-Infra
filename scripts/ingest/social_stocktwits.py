#!/usr/bin/env python3
"""social_stocktwits.py — StockTwits per-symbol stream ingester.

Reads tickers from signals.watchlist (active rows) and pulls the public
per-symbol message stream. No API key, no OAuth.

The clean bit: StockTwits messages carry a user-labelled sentiment field
(`entities.sentiment.basic` ∈ {Bullish, Bearish, null}). We persist that
verbatim as `sentiment_label` with sentiment_model='user_label' — the
fast path can use it directly, and we don't pay an LLM call to label
retail sentiment that the user already labelled themselves.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

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

INGESTER_NAME = "social_stocktwits"
STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
USER_AGENT = "AOTC-Signals/0.1 (contact@example.com)"
PER_TICKER_LIMIT = 30
POLL_INTERVAL_SEC = 60


def _active_watchlist() -> list[str]:
    rows = pg.query("signals.watchlist", select=["ticker"], where={"active": True})
    return [r["ticker"] for r in rows]


def _fetch(ticker: str) -> list[dict]:
    url = STREAM_URL.format(ticker=ticker.upper())
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        payload = request_json("GET", url, headers=headers, timeout=10)
    except HttpRetryError:
        return []
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return []
    return messages[:PER_TICKER_LIMIT]


def _row_from_message(msg: dict, ticker: str) -> dict | None:
    body = (msg.get("body") or "").strip()
    if not body:
        return None
    external_id = msg.get("id")
    if external_id is None:
        return None

    user = msg.get("user") or {}
    entities = msg.get("entities") or {}
    sentiment_obj = entities.get("sentiment") or {}
    sentiment_basic = sentiment_obj.get("basic") if isinstance(sentiment_obj, dict) else None

    sentiment_label = None
    sentiment_score = None
    sentiment_model = None
    if sentiment_basic in ("Bullish", "Bearish"):
        sentiment_label = sentiment_basic.lower()
        sentiment_score = 1.0 if sentiment_label == "bullish" else -1.0
        sentiment_model = "user_label"

    # StockTwits embeds the cashtag(s) the message tagged. Capture them all.
    symbols = entities.get("symbols") if isinstance(entities, dict) else None
    if not isinstance(symbols, list):
        symbols = []
    tickers = [s.get("symbol") for s in symbols if isinstance(s, dict) and s.get("symbol")]
    if ticker.upper() not in [t.upper() for t in tickers]:
        tickers.append(ticker.upper())

    created_at_raw = msg.get("created_at")
    posted_at = datetime.now(timezone.utc)
    if created_at_raw:
        try:
            posted_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass

    return {
        "source": "stocktwits",
        "external_id": str(external_id),
        "author": user.get("username"),
        "author_followers": user.get("followers"),
        "parent_id": None,
        "channel": ticker.upper(),
        "title": None,
        "body": body,
        "url": f"https://stocktwits.com/{user.get('username') or 'unknown'}/message/{external_id}",
        "posted_at": posted_at,
        "language": None,
        "score": (msg.get("likes") or {}).get("total") if isinstance(msg.get("likes"), dict) else None,
        "comments": (msg.get("conversation") or {}).get("replies") if isinstance(msg.get("conversation"), dict) else None,
        "reposts": None,
        "tickers": [t.upper() for t in tickers],
        "sentiment_label": sentiment_label,
        "sentiment_score": sentiment_score,
        "sentiment_model": sentiment_model,
        "metadata": {
            "stocktwits_user_id": user.get("id"),
            "user_join_date": user.get("join_date"),
            "official": user.get("official"),
            "raw_sentiment_basic": sentiment_basic,
        },
    }


def _ingest_ticker(ticker: str, log) -> int:
    messages = _fetch(ticker)
    inserted = 0
    for msg in messages:
        row = _row_from_message(msg, ticker)
        if not row:
            continue
        persist_raw_payload("stocktwits", row["external_id"], msg)
        try:
            pg.upsert("social.posts", row, conflict_on=["source", "external_id"])
            inserted += 1
        except Exception:
            log.exception("stocktwits upsert failed for %s", row["external_id"])
    if inserted:
        log.info("%s: ingested %d stocktwits messages", ticker, inserted)
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
            log.exception("stocktwits poll failed for %s", ticker)


def main() -> int:
    load_dotenv_files()
    setup_logging(INGESTER_NAME)
    with singleton_lock(INGESTER_NAME):
        poll_loop(name=INGESTER_NAME, tick_fn=tick, tick_interval_sec=POLL_INTERVAL_SEC)
    return 0


if __name__ == "__main__":
    sys.exit(main())
