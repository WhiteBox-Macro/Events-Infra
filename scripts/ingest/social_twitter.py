#!/usr/bin/env python3
"""social_twitter.py — X (Twitter) cashtag ingester.

Gated on TWITTER_BEARER_TOKEN. If unset, exits 0 with a one-line message —
the harness can safely include this in a launcher script before paying for
the API.

When set: combines all active watchlist tickers into one OR-query and polls
the v2 `tweets/search/recent` endpoint. Forward-only pagination via since_id
(the latest stored tweet id) so each call returns only tweets we haven't
seen.

Tier requirements (as of 2024):
  Free        — useless for ingestion (post-only).
  Basic $200  — search/recent + 15k posts/month + 60 req/15min.
  Pro $5000   — filtered stream + 1M posts/month + much higher limits.

Default cadence (60s) is well under the Basic tier per-app limit, leaving
headroom for restarts.
"""
from __future__ import annotations

import os
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

INGESTER_NAME = "social_twitter"
# api.twitter.com remained the canonical host through the X rename and continues
# to work; api.x.com is the documented alias. Override via TWITTER_API_BASE.
DEFAULT_BASE = os.environ.get("TWITTER_API_BASE", "https://api.twitter.com")
SEARCH_PATH = "/2/tweets/search/recent"
USERS_BY_PATH = "/2/users/by/username/{username}"
USER_TIMELINE_PATH = "/2/users/{user_id}/tweets"
POLL_INTERVAL_SEC = 60
MAX_RESULTS = 100              # API max per page
QUERY_MAX_CHARS = 512          # X limit
# Excluded by default: retweets (noise), non-English (lang:en) so the
# downstream LLM doesn't have to translate first.
QUERY_SUFFIX = " -is:retweet lang:en"
TWEET_FIELDS = "created_at,author_id,public_metrics,lang,in_reply_to_user_id"
USER_FIELDS = "username,verified,public_metrics"
EXPANSIONS = "author_id"


def _active_watchlist() -> list[str]:
    rows = pg.query("signals.watchlist", select=["ticker"], where={"active": True})
    return [r["ticker"] for r in rows]


def _build_query(tickers: list[str]) -> str:
    """Pack cashtags into an OR-query under the 512-char limit.

    Cashtags ($AAPL) are the primary tag style on X; we also fall back to the
    hashtag form for tickers that are commonly referenced without the dollar
    sign (e.g. #NVDA).
    """
    if not tickers:
        return ""
    body = " OR ".join(f"(${t} OR #{t})" for t in tickers)
    query = f"({body}){QUERY_SUFFIX}"
    while len(query) > QUERY_MAX_CHARS and tickers:
        tickers = tickers[:-1]
        body = " OR ".join(f"(${t} OR #{t})" for t in tickers)
        query = f"({body}){QUERY_SUFFIX}"
    return query


def _latest_seen_id() -> str | None:
    rows = pg.execute(
        "SELECT external_id FROM social.posts WHERE source = %s "
        "ORDER BY external_id DESC LIMIT 1",
        ["twitter"],
    )
    return rows[0]["external_id"] if rows else None


# ── Tracked-handle helpers ──────────────────────────────────────────────────
def _active_handles() -> list[dict]:
    """Return rows from social.handles WHERE platform='twitter' AND active=TRUE.

    Falls back to an empty list if the table doesn't exist yet (migration 007
    not applied) — keeps the cashtag-search path working on a stale schema."""
    try:
        return pg.execute(
            "SELECT handle_id, username, user_id, impact_weight, last_seen_external_id, "
            "       poll_interval_sec, last_polled_at, tags, expected_themes "
            "FROM social.handles "
            "WHERE platform = 'twitter' AND active = TRUE"
        )
    except Exception:
        # Most likely cause: 007_social_handles.sql not yet applied. Skip the
        # per-handle pass and let the cashtag search carry the load.
        return []


def _resolve_user_id(username: str, bearer: str, log) -> str | None:
    """One-shot /2/users/by/username/{username} to bootstrap user_id."""
    url = DEFAULT_BASE + USERS_BY_PATH.format(username=username)
    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        payload = request_json("GET", url, headers=headers, timeout=10)
    except HttpRetryError:
        log.warning("could not resolve user_id for @%s", username)
        return None
    user_id = (payload.get("data") or {}).get("id") if isinstance(payload, dict) else None
    if user_id:
        pg.update(
            "social.handles",
            {"user_id": user_id, "display_name": (payload["data"] or {}).get("name")},
            {"platform": "twitter", "username": username},
        )
    return user_id


def _pull_handle_timeline(handle: dict, bearer: str, log) -> int:
    """Pull tweets newer than `last_seen_external_id` for one handle.

    Tweets are written with `tickers=[]` — the dispatcher will route them to
    trader.social_inference (LLM theme→ticker) when no cashtag resolves."""
    username = handle["username"]
    user_id = handle.get("user_id") or _resolve_user_id(username, bearer, log)
    if not user_id:
        return 0

    params = {
        "max_results": str(min(MAX_RESULTS, 100)),
        "tweet.fields": TWEET_FIELDS,
        "expansions": EXPANSIONS,
        "user.fields": USER_FIELDS,
    }
    if handle.get("last_seen_external_id"):
        params["since_id"] = str(handle["last_seen_external_id"])
    url = DEFAULT_BASE + USER_TIMELINE_PATH.format(user_id=user_id)
    headers = {"Authorization": f"Bearer {bearer}"}

    try:
        payload = request_json("GET", url, params=params, headers=headers, timeout=15)
    except HttpRetryError:
        return 0

    tweets = payload.get("data") or []
    users_list = (payload.get("includes") or {}).get("users") or []
    users_by_id = {u.get("id"): u for u in users_list if u.get("id")}
    newest_id = (payload.get("meta") or {}).get("newest_id")

    inserted = 0
    for tweet in tweets:
        row = _row_from_tweet(tweet, users_by_id)
        if not row:
            continue
        # Mark the channel so downstream can recognise tracked-handle posts
        # in a single column query instead of joining social.handles.
        row["channel"] = f"handle:{username}"
        row["metadata"] = {
            **(row.get("metadata") or {}),
            "from_tracked_handle": True,
            "handle_id": handle.get("handle_id"),
            "impact_weight": float(handle.get("impact_weight") or 1.0),
            "handle_tags": handle.get("tags") or [],
            "handle_expected_themes": handle.get("expected_themes") or [],
        }
        persist_raw_payload("twitter", row["external_id"], tweet)
        try:
            pg.upsert("social.posts", row, conflict_on=["source", "external_id"])
            inserted += 1
        except Exception:
            log.exception("twitter (handle) upsert failed for %s", row["external_id"])

    if newest_id:
        pg.update(
            "social.handles",
            {"last_seen_external_id": str(newest_id),
             "last_polled_at": datetime.now(timezone.utc)},
            {"handle_id": handle["handle_id"]},
        )
    elif inserted == 0:
        # Update poll timestamp so a quiet handle doesn't keep skipping cadence checks.
        pg.update(
            "social.handles",
            {"last_polled_at": datetime.now(timezone.utc)},
            {"handle_id": handle["handle_id"]},
        )

    if inserted:
        log.info("@%s: ingested %d tweets (since_id=%s → %s)",
                 username, inserted, handle.get("last_seen_external_id"), newest_id)
    return inserted


def _row_from_tweet(tweet: dict, users_by_id: dict) -> dict | None:
    body = (tweet.get("text") or "").strip()
    external_id = tweet.get("id")
    if not body or not external_id:
        return None
    created_at = tweet.get("created_at")
    posted_at = datetime.now(timezone.utc)
    if created_at:
        try:
            posted_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass
    author = users_by_id.get(tweet.get("author_id")) or {}
    metrics = tweet.get("public_metrics") or {}

    return {
        "source": "twitter",
        "external_id": str(external_id),
        "author": author.get("username"),
        "author_followers": (author.get("public_metrics") or {}).get("followers_count"),
        "parent_id": tweet.get("in_reply_to_user_id"),
        "channel": None,
        "title": None,
        "body": body,
        "url": f"https://x.com/{author.get('username') or 'i'}/status/{external_id}",
        "posted_at": posted_at,
        "language": tweet.get("lang"),
        "score": metrics.get("like_count"),
        "comments": metrics.get("reply_count"),
        "reposts": (metrics.get("retweet_count") or 0) + (metrics.get("quote_count") or 0),
        "tickers": [],   # enrichment layer extracts; cashtag presence in body is enough hint
        "metadata": {
            "verified": author.get("verified"),
            "author_id": tweet.get("author_id"),
            "impression_count": metrics.get("impression_count"),
            "quote_count": metrics.get("quote_count"),
            "retweet_count": metrics.get("retweet_count"),
        },
    }


def _search(query: str, bearer: str, since_id: str | None) -> tuple[list[dict], dict]:
    """Returns (tweets, users_by_id)."""
    params = {
        "query": query,
        "max_results": str(MAX_RESULTS),
        "tweet.fields": "created_at,author_id,public_metrics,lang,in_reply_to_user_id",
        "expansions": "author_id",
        "user.fields": "username,verified,public_metrics",
    }
    if since_id:
        params["since_id"] = since_id

    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        payload = request_json("GET", DEFAULT_BASE + SEARCH_PATH,
                               params=params, headers=headers, timeout=15)
    except HttpRetryError:
        return [], {}

    tweets = payload.get("data") or []
    users_list = ((payload.get("includes") or {}).get("users") or [])
    users_by_id = {u.get("id"): u for u in users_list if u.get("id")}
    return tweets, users_by_id


def tick(bearer: str) -> None:
    log = setup_logging(INGESTER_NAME)

    # ── Pass 1: cashtag search across watchlist (catches anyone mentioning a
    # ticker we follow). Skipped silently if the watchlist is empty.
    tickers = _active_watchlist()
    if tickers:
        query = _build_query(tickers)
        since_id = _latest_seen_id()
        tweets, users_by_id = _search(query, bearer, since_id)
        inserted = 0
        for tweet in tweets:
            row = _row_from_tweet(tweet, users_by_id)
            if not row:
                continue
            persist_raw_payload("twitter", row["external_id"], tweet)
            try:
                pg.upsert("social.posts", row, conflict_on=["source", "external_id"])
                inserted += 1
            except Exception:
                log.exception("twitter (cashtag) upsert failed for %s", row["external_id"])
        if inserted:
            log.info("cashtag pass: ingested %d tweets (since_id=%s)", inserted, since_id)
    else:
        log.debug("watchlist empty; skipping cashtag pass")

    # ── Pass 2: per-handle timeline for tracked accounts (Elon, Trump, etc.)
    # — catches posts regardless of cashtag presence. Skipped if no handles
    # are seeded (no rows in social.handles).
    if stop_event.is_set():
        return
    handles = _active_handles()
    for handle in handles:
        if stop_event.is_set():
            return
        # Respect per-handle poll_interval_sec so we don't pound a quiet
        # account every tick.
        last = handle.get("last_polled_at")
        interval = handle.get("poll_interval_sec") or 60
        if last is not None and (datetime.now(timezone.utc) - last).total_seconds() < interval:
            continue
        try:
            _pull_handle_timeline(handle, bearer, log)
        except Exception:
            log.exception("twitter (handle) tick failed for @%s", handle.get("username"))


def main() -> int:
    load_dotenv_files()
    log = setup_logging(INGESTER_NAME)
    bearer = os.environ.get("TWITTER_BEARER_TOKEN", "").strip()
    if not bearer:
        log.info(
            "TWITTER_BEARER_TOKEN not set — Twitter/X ingester is gated; "
            "exiting cleanly. Set the env var (paid X API key) to enable."
        )
        return 0
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    with singleton_lock(INGESTER_NAME):
        poll_loop(name=INGESTER_NAME, tick_fn=lambda: tick(bearer),
                  tick_interval_sec=POLL_INTERVAL_SEC)
    return 0


if __name__ == "__main__":
    sys.exit(main())
