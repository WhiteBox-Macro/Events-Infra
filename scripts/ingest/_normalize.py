"""News item normalisation + dedup.

Both helpers are lifted (and trimmed) from HKUDS/AI-Trader's
service/server/market_intel.py — they handle Alpha Vantage NEWS_SENTIMENT's
quirky shape cleanly and are battle-tested.

normalize_av_news_item turns one Alpha Vantage feed entry into our common
shape; dedupe_news_items collapses duplicates that arrive across overlapping
windows (live + backfill) or from rate-limited retries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def parse_av_timestamp(raw: Optional[str]) -> Optional[datetime]:
    """Alpha Vantage timestamps are `YYYYMMDDTHHMMSS` (UTC, no offset)."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_av_news_item(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalise one Alpha Vantage `feed` entry.

    Returns None if the item is missing critical fields (title or timestamp).
    Output shape is decoupled from Alpha Vantage so downstream code stays
    vendor-agnostic.
    """
    title = (item.get("title") or "").strip()
    if not title:
        return None

    time_published = parse_av_timestamp(item.get("time_published"))
    if not time_published:
        return None

    ticker_sentiment: list[dict] = []
    for entry in item.get("ticker_sentiment") or []:
        if not isinstance(entry, dict):
            continue
        ticker = (entry.get("ticker") or "").strip()
        if not ticker:
            continue
        ticker_sentiment.append({
            "ticker": ticker,
            "relevance_score": _as_float(entry.get("relevance_score")),
            "sentiment_score": _as_float(entry.get("ticker_sentiment_score")),
            "sentiment_label": entry.get("ticker_sentiment_label"),
        })

    topics: list[dict] = []
    for entry in item.get("topics") or []:
        if not isinstance(entry, dict):
            continue
        topic = (entry.get("topic") or "").strip()
        if topic:
            topics.append({
                "topic": topic,
                "relevance_score": _as_float(entry.get("relevance_score")),
            })

    return {
        "title": title,
        "url": (item.get("url") or "").strip(),
        "publisher": (item.get("source") or "").strip() or "Unknown",
        "summary": (item.get("summary") or "").strip(),
        "banner_image": item.get("banner_image"),
        "time_published": time_published,
        "overall_sentiment_score": _as_float(item.get("overall_sentiment_score")),
        "overall_sentiment_label": item.get("overall_sentiment_label"),
        "ticker_sentiment": ticker_sentiment,
        "topics": topics,
        "authors": item.get("authors") or [],
        "category_within_source": item.get("category_within_source"),
    }


def dedupe_news_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicates, keeping newest first.

    Dedup key is the URL when present, else `title::publisher`. This catches
    both the obvious case (same article fetched twice) and AV's habit of
    republishing the same headline through different syndication paths.
    """
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda r: r["time_published"], reverse=True):
        key = item.get("url") or f"{item['title']}::{item.get('publisher', '')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def extract_av_tickers(item: dict[str, Any]) -> list[str]:
    """Pull just the ticker symbols from a normalised AV item."""
    return [t["ticker"] for t in item.get("ticker_sentiment", []) if t.get("ticker")]


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
