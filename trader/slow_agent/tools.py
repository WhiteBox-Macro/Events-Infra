"""SQL-backed analyst tools.

The TradingAgents analysts call HTTP APIs (Alpha Vantage, yfinance) for
their data. We swap those for SQL queries against the schemas already
populated by scripts/ingest/* and signals.price_cache.

This means a slow agent run reads ZERO external APIs (apart from the LLM
calls themselves) — every fact comes from Postgres. Side-effects: a clean
backtest contract (same SQL = same data both modes) and a much faster
slow run than TradingAgents' HTTP-bound version.

Functions exposed:
  recent_news_for(ticker, since)
  recent_social_for(ticker, since)
  recent_macro_releases(since)
  recent_prices(ticker, since)
  recent_indicators(ticker, lookback_days)
  fundamentals_snapshot(ticker)

Each returns either a list[dict] (raw rows) or a short markdown summary
suitable for prompt injection. Analyst nodes get the markdown form.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from dbkit import pg

_log = logging.getLogger(__name__)

DEFAULT_NEWS_LOOKBACK_HOURS = 48
DEFAULT_SOCIAL_LOOKBACK_HOURS = 24
DEFAULT_MACRO_LOOKBACK_DAYS = 30
DEFAULT_PRICE_LOOKBACK_DAYS = 30


# ── News ──────────────────────────────────────────────────────────────────
def recent_news_for(ticker: str, *, since: Optional[datetime] = None, limit: int = 25) -> list[dict]:
    since = since or _hours_ago(DEFAULT_NEWS_LOOKBACK_HOURS)
    try:
        return pg.execute(
            "SELECT a.article_id, a.title, a.summary, a.url, a.published_at, "
            "       a.sentiment_score, a.sentiment_model, a.tickers, a.categories, "
            "       s.publisher, s.category AS source_category, s.name AS source_name "
            "FROM news.articles a "
            "JOIN news.sources s ON s.id = a.source_id "
            "WHERE %s = ANY(a.tickers) "
            "  AND a.published_at >= %s "
            "ORDER BY a.published_at DESC LIMIT %s",
            [ticker.upper(), since, limit],
        )
    except Exception:
        _log.exception("recent_news_for(%s) failed", ticker)
        return []


def news_summary_for(ticker: str, *, since: Optional[datetime] = None) -> str:
    rows = recent_news_for(ticker, since=since)
    if not rows:
        return f"No news for {ticker} in the lookback window."
    lines = [f"## Recent news for {ticker} ({len(rows)} items)"]
    for r in rows[:15]:
        when = r["published_at"].strftime("%Y-%m-%d %H:%M UTC") if r.get("published_at") else "?"
        sent = f"sent={r['sentiment_score']:+.2f}" if r.get("sentiment_score") is not None else "sent=n/a"
        lines.append(
            f"- [{when} | {r.get('publisher') or r.get('source_name', '?')} | {sent}] {r.get('title')}"
        )
        if r.get("summary"):
            summary = r["summary"][:240].replace("\n", " ")
            lines.append(f"    {summary}")
    return "\n".join(lines)


# ── Social ────────────────────────────────────────────────────────────────
def recent_social_for(ticker: str, *, since: Optional[datetime] = None, limit: int = 50) -> list[dict]:
    since = since or _hours_ago(DEFAULT_SOCIAL_LOOKBACK_HOURS)
    try:
        return pg.execute(
            "SELECT post_id, source, author, author_followers, channel, body, posted_at, "
            "       score, comments, reposts, tickers, sentiment_label, sentiment_score "
            "FROM social.posts "
            "WHERE %s = ANY(tickers) AND posted_at >= %s "
            "ORDER BY posted_at DESC LIMIT %s",
            [ticker.upper(), since, limit],
        )
    except Exception:
        _log.exception("recent_social_for(%s) failed", ticker)
        return []


def social_summary_for(ticker: str, *, since: Optional[datetime] = None) -> str:
    rows = recent_social_for(ticker, since=since)
    if not rows:
        return f"No social posts about {ticker} in the lookback window."
    bull = sum(1 for r in rows if (r.get("sentiment_label") or "").lower() == "bullish")
    bear = sum(1 for r in rows if (r.get("sentiment_label") or "").lower() == "bearish")
    influencers = [r for r in rows if (r.get("author_followers") or 0) >= 100_000]
    lines = [
        f"## Social chatter for {ticker} (last {len(rows)} posts)",
        f"- Labelled sentiment: bullish={bull}, bearish={bear}, unlabelled={len(rows) - bull - bear}",
        f"- High-follower (≥100k) posts: {len(influencers)}",
    ]
    if influencers:
        lines.append("\n**Top influencer posts:**")
        for r in influencers[:5]:
            when = r["posted_at"].strftime("%Y-%m-%d %H:%M UTC") if r.get("posted_at") else "?"
            body = (r.get("body") or "").replace("\n", " ")[:200]
            lines.append(f"- [{when} | @{r.get('author')} ({r.get('author_followers'):,} followers) | {r.get('source')}] {body}")
    lines.append("\n**Most recent posts:**")
    for r in rows[:8]:
        when = r["posted_at"].strftime("%H:%M UTC") if r.get("posted_at") else "?"
        body = (r.get("body") or "").replace("\n", " ")[:160]
        label = r.get("sentiment_label") or "—"
        lines.append(f"- [{when} | @{r.get('author')} | {label}] {body}")
    return "\n".join(lines)


# ── Macro ─────────────────────────────────────────────────────────────────
def recent_macro_releases(*, since: Optional[datetime] = None, limit: int = 30) -> list[dict]:
    since = since or _days_ago(DEFAULT_MACRO_LOOKBACK_DAYS)
    try:
        return pg.execute(
            "SELECT r.release_id, r.period_start, r.value, r.prior_value, r.consensus, "
            "       r.surprise, r.surprise_z, r.released_at, "
            "       i.code, i.name, i.units, i.importance "
            "FROM macro.releases r "
            "JOIN macro.indicators i ON i.id = r.indicator_id "
            "WHERE r.released_at >= %s "
            "ORDER BY r.released_at DESC LIMIT %s",
            [since, limit],
        )
    except Exception:
        _log.exception("recent_macro_releases failed")
        return []


def macro_summary(*, since: Optional[datetime] = None) -> str:
    rows = recent_macro_releases(since=since)
    if not rows:
        return "No macro releases in the lookback window."
    lines = ["## Recent macro releases"]
    for r in rows[:15]:
        when = r["released_at"].strftime("%Y-%m-%d") if r.get("released_at") else "?"
        sur = ""
        if r.get("surprise_z") is not None:
            sur = f" (z={float(r['surprise_z']):+.2f})"
        elif r.get("surprise") is not None:
            sur = f" (surprise={float(r['surprise']):+.3f})"
        lines.append(
            f"- [{when} | imp={r.get('importance')}] {r.get('code')} ({r.get('units')}): "
            f"{r.get('value')} (prior {r.get('prior_value')}, consensus {r.get('consensus')}){sur}"
        )
    return "\n".join(lines)


# ── Prices / technicals ───────────────────────────────────────────────────
def recent_prices(ticker: str, *, since: Optional[datetime] = None, limit: int = 250) -> list[dict]:
    since = since or _days_ago(DEFAULT_PRICE_LOOKBACK_DAYS)
    try:
        return pg.execute(
            "SELECT price_at, price, source FROM signals.price_cache "
            "WHERE ticker = %s AND price_at >= %s "
            "ORDER BY price_at ASC LIMIT %s",
            [ticker.upper(), since, limit],
        )
    except Exception:
        _log.exception("recent_prices(%s) failed", ticker)
        return []


def technical_summary_for(ticker: str, *, since: Optional[datetime] = None) -> str:
    rows = recent_prices(ticker, since=since)
    if not rows:
        return (
            f"No cached prices for {ticker} in the lookback window. "
            f"Live trading will rely on the fast path's quote; backtest must "
            f"prime signals.price_cache first."
        )
    closes = [float(r["price"]) for r in rows if r.get("price") is not None]
    if not closes:
        return f"price_cache for {ticker} has no usable rows."
    last = closes[-1]
    first = closes[0]
    pct = (last / first - 1) * 100 if first else 0
    high, low = max(closes), min(closes)
    ma5 = _ma(closes, 5)
    ma20 = _ma(closes, 20)
    ma5_s = f"{ma5:.2f}" if ma5 is not None else "n/a"
    ma20_s = f"{ma20:.2f}" if ma20 is not None else "n/a"
    return (
        f"## Technical snapshot for {ticker}\n"
        f"- {len(closes)} cached points, "
        f"{rows[0]['price_at'].strftime('%Y-%m-%d %H:%M')} → "
        f"{rows[-1]['price_at'].strftime('%Y-%m-%d %H:%M')}\n"
        f"- Last={last:.2f}, period return={pct:+.2f}%, range=[{low:.2f}, {high:.2f}]\n"
        f"- 5pt MA={ma5_s}, 20pt MA={ma20_s}"
    )


def _ma(values: list[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


# ── Fundamentals stub ─────────────────────────────────────────────────────
def fundamentals_summary_for(ticker: str) -> str:
    """Read what we can from AOTC-DB's stock_os.* tables.

    Conservative implementation: we only pull `stock_os.securities` because
    that's the one cross-repo table the README + hello_db.py both reference.
    Richer fundamentals (filings, transcripts, ratios) require knowing the
    AOTC-DB schema in detail; that integration lives on a separate ticket.
    """
    try:
        rows = pg.execute(
            "SELECT symbol, name, exchange, sector, industry "
            "FROM stock_os.securities WHERE symbol = %s LIMIT 1",
            [ticker.upper()],
        )
    except Exception:
        return f"AOTC-DB stock_os.securities unreachable; no fundamentals available for {ticker}."
    if not rows:
        return f"No stock_os.securities entry for {ticker}."
    r = rows[0]
    return (
        f"## Reference data for {ticker}\n"
        f"- Name: {r.get('name') or '?'}\n"
        f"- Exchange: {r.get('exchange') or '?'}\n"
        f"- Sector / Industry: {r.get('sector') or '?'} / {r.get('industry') or '?'}\n"
        f"\n(Richer fundamentals — filings, earnings transcripts, ratios — pending AOTC-DB integration.)"
    )


# ── Helpers ───────────────────────────────────────────────────────────────
def _hours_ago(h: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=h)


def _days_ago(d: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=d)
