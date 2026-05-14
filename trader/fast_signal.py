"""trader/fast_signal.py — deterministic, no-LLM signal path.

For each watchlist ticker that an event mentions, this module:
  1. accumulates a score from the event's structured fields (Alpha Vantage
     sentiment, StockTwits Bullish/Bearish, macro surprise z-score, …),
  2. maps the score to a 5-tier rating (Buy/Overweight/Hold/Underweight/Sell),
  3. writes a signals.decisions row (always — Hold rows are still useful for
     the backtest as "what would have happened" labels),
  4. opens a paper position via trader.paper.execute, but only when the
     rating is actionable AND there isn't already an open position for the
     same (ticker, mode).

Scoring rules are intentionally simple and named ("article tone constructive",
"ticker is the focus and bullish", …). The factors list is persisted into
signals.decisions.debate_transcript so backtest reports can attribute alpha
to individual rules and we can tune them without re-reading every article.

Score → rating bands (mirrors TradingAgents' 5-tier scale):
   score ≥ +2.5   Buy
   score ≥ +1.5   Overweight
   |score| < 1.5  Hold       (no fill)
   score ≤ -1.5   Underweight
   score ≤ -2.5   Sell

Confidence = min(|score| / 3.0, 1.0). Used to scale paper position size.

This module is pure: it reads the event row + watchlist + (price source for
fills) and writes to Postgres. The dispatcher constructs Clock + PriceSource
once and passes them in, so backtest and live share the same code path.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from dbkit import pg
from trader.clock import Clock
from trader.prices import PriceSource

_log = logging.getLogger(__name__)

# Bump this string whenever scoring rules change so backtest experiments can
# group runs by agent version. Phase 4 will switch to a hash of (prompts +
# model versions); for the deterministic fast path a tag is enough.
AGENT_HASH = "fast_signal_v1"

CONFIDENCE_NORMALISER = 3.0  # score that maps to confidence=1.0

# Per-rule weights (extracted so the user can tune via env or future config).
W_ARTICLE_TONE       = 1.0    # overall_sentiment_score ≥ 0.35
W_ARTICLE_TICKER     = 1.5    # ticker-specific AV sentiment (relevance ≥ 0.5)
W_ARTICLE_TICKER_LOW = 0.5    # ticker-specific AV sentiment (relevance 0.25-0.5)
W_HIGH_IMPACT_TOPIC  = 0.5    # earnings / M&A / IPO
W_PRIORITY_BONUS     = 0.5    # high-priority source (SEC EDGAR 8-K, Fed press)
W_RECENCY_FRESH      = 0.3    # < 60 min old
W_RECENCY_STALE      = -0.5   # > 24h old
W_POST_SENTIMENT     = 0.4    # StockTwits user_label
W_POST_INFLUENCER    = 1.5    # multiplies post score for high-follower author
W_MACRO_BASE         = 1.0    # macro surprise (capped)

SENTIMENT_BUY_THRESHOLD  = 0.35
SENTIMENT_TICKER_RELEVANCE_HIGH = 0.5
SENTIMENT_TICKER_RELEVANCE_LOW  = 0.25
HIGH_IMPACT_TOPICS = {"earnings", "mergers_and_acquisitions", "ipo", "financial_results"}

# ── Rating mapping ──────────────────────────────────────────────────────────
RATING_BANDS = (
    ( 2.5, "Buy"),
    ( 1.5, "Overweight"),
    (-1.5, "Hold"),
    (-2.5, "Underweight"),
)
ACTIONABLE = {"Buy", "Overweight", "Underweight", "Sell"}
LONG_RATINGS = {"Buy", "Overweight"}
SHORT_RATINGS = {"Sell", "Underweight"}


@dataclass
class ScoreResult:
    """Deterministic scoring output for one (event, ticker) pair."""
    ticker: str
    score: float = 0.0
    factors: list[str] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return min(abs(self.score) / CONFIDENCE_NORMALISER, 1.0)

    @property
    def rating(self) -> str:
        for threshold, label in RATING_BANDS:
            if self.score >= threshold:
                return label
        return "Sell"

    def as_transcript(self) -> dict:
        return {
            "agent": AGENT_HASH,
            "score": round(self.score, 3),
            "confidence": round(self.confidence, 3),
            "rating": self.rating,
            "factors": self.factors,
            "risk_factors": self.risk_factors,
        }


# ── Scoring: articles ───────────────────────────────────────────────────────
def score_article_for_ticker(article: dict, ticker: str, *, clock: Clock) -> ScoreResult:
    result = ScoreResult(ticker=ticker.upper())

    sentiment = article.get("sentiment_score")
    if sentiment is not None:
        s = float(sentiment)
        if s >= SENTIMENT_BUY_THRESHOLD:
            result.score += W_ARTICLE_TONE
            result.factors.append(f"Article tone constructive (sentiment={s:+.2f})")
        elif s <= -SENTIMENT_BUY_THRESHOLD:
            result.score -= W_ARTICLE_TONE
            result.risk_factors.append(f"Article tone defensive (sentiment={s:+.2f})")

    # Per-ticker sentiment is more specific than the headline tone.
    meta = article.get("metadata") or {}
    av_ts = meta.get("av_ticker_sentiment") or []
    for ts in av_ts:
        if not isinstance(ts, dict):
            continue
        if (ts.get("ticker") or "").upper() != result.ticker:
            continue
        rel = float(ts.get("relevance_score") or 0)
        sent = float(ts.get("sentiment_score") or 0)
        if rel >= SENTIMENT_TICKER_RELEVANCE_HIGH and sent >= SENTIMENT_BUY_THRESHOLD:
            result.score += W_ARTICLE_TICKER
            result.factors.append(f"{result.ticker} is the focus and bullish (rel={rel:.2f}, s={sent:+.2f})")
        elif rel >= SENTIMENT_TICKER_RELEVANCE_HIGH and sent <= -SENTIMENT_BUY_THRESHOLD:
            result.score -= W_ARTICLE_TICKER
            result.risk_factors.append(f"{result.ticker} is the focus and bearish (rel={rel:.2f}, s={sent:+.2f})")
        elif rel >= SENTIMENT_TICKER_RELEVANCE_LOW and sent >= SENTIMENT_BUY_THRESHOLD:
            result.score += W_ARTICLE_TICKER_LOW
            result.factors.append(f"{result.ticker} mentioned positively (rel={rel:.2f}, s={sent:+.2f})")
        elif rel >= SENTIMENT_TICKER_RELEVANCE_LOW and sent <= -SENTIMENT_BUY_THRESHOLD:
            result.score -= W_ARTICLE_TICKER_LOW
            result.risk_factors.append(f"{result.ticker} mentioned negatively (rel={rel:.2f}, s={sent:+.2f})")

    # Topic-based impact bump (earnings/M&A/IPO move stocks more than a generic
    # commentary piece).
    categories = [c.lower() for c in (article.get("categories") or [])]
    if any(c in HIGH_IMPACT_TOPICS for c in categories):
        sign = 1.0 if result.score >= 0 else -1.0
        result.score += sign * W_HIGH_IMPACT_TOPIC
        result.factors.append(f"High-impact topic ({','.join(c for c in categories if c in HIGH_IMPACT_TOPICS)})")

    # Source priority lives in the `news.sources.metadata.priority` field —
    # we don't join here on the hot path. Instead this lifts ratings on
    # rows ingested from SEC EDGAR / Fed press where the article's own
    # metadata carries a priority value.
    src_priority = (article.get("metadata") or {}).get("priority")
    try:
        if src_priority is not None and float(src_priority) >= 4:
            sign = 1.0 if result.score >= 0 else -1.0
            result.score += sign * W_PRIORITY_BONUS
            result.factors.append(f"High-priority source (priority={src_priority})")
    except (TypeError, ValueError):
        pass

    # Recency: a fresh article carries more action signal than one from a day ago.
    published_at = article.get("published_at")
    if published_at is not None:
        age = clock.now() - published_at
        if age < timedelta(minutes=60):
            sign = 1.0 if result.score >= 0 else -1.0
            result.score += sign * W_RECENCY_FRESH
            result.factors.append("Published within last 60 minutes")
        elif age > timedelta(hours=24):
            sign = 1.0 if result.score >= 0 else -1.0
            result.score += sign * W_RECENCY_STALE
            result.risk_factors.append(f"Stale ({age.total_seconds()/3600:.0f}h old)")

    return result


# ── Scoring: social posts ───────────────────────────────────────────────────
def score_post_for_ticker(post: dict, ticker: str, *, clock: Clock,
                          handle_weight: float = 1.0) -> ScoreResult:
    """`handle_weight` is the curator-set multiplier from social.handles.
    Untracked authors pass 1.0; tracked accounts pass their impact_weight
    so a tweet from Trump scores harder than a 1M-follower retail account."""
    result = ScoreResult(ticker=ticker.upper())

    label = (post.get("sentiment_label") or "").lower()
    if label == "bullish":
        result.score += W_POST_SENTIMENT
        result.factors.append("Post labelled bullish")
    elif label == "bearish":
        result.score -= W_POST_SENTIMENT
        result.risk_factors.append("Post labelled bearish")
    else:
        # No labelled sentiment + no LLM enrichment = no signal worth acting on.
        result.factors.append("No labelled sentiment; fast path neutral")

    followers = post.get("author_followers")
    try:
        f = int(followers) if followers is not None else 0
    except (TypeError, ValueError):
        f = 0
    if f >= 1_000_000:
        multiplier = 4.0
        tier = "1M+"
    elif f >= 100_000:
        multiplier = 2.0
        tier = "100k+"
    elif f >= 10_000:
        multiplier = 1.5
        tier = "10k+"
    else:
        multiplier = 1.0
        tier = None
    if tier and result.score != 0:
        result.score *= multiplier
        result.factors.append(f"Influencer weight ×{multiplier} ({tier} followers)")

    # Curator-set per-handle weight stacks on top of follower-tier. A tweet
    # from a low-follower but high-impact account (newly-launched ministry
    # spokesperson, niche reporter) can still score actionable.
    if handle_weight and handle_weight != 1.0 and result.score != 0:
        result.score *= float(handle_weight)
        result.factors.append(f"Tracked-handle weight ×{handle_weight}")

    # Posts older than 4 hours don't trade on news; drop weight aggressively.
    posted_at = post.get("posted_at")
    if posted_at is not None:
        age = clock.now() - posted_at
        if age > timedelta(hours=4):
            result.score *= 0.5
            result.risk_factors.append(f"Stale post ({age.total_seconds()/3600:.1f}h old)")

    return result


# ── Scoring: macro releases ─────────────────────────────────────────────────
def score_macro_for_ticker(release: dict, ticker: str, watchlist_row: dict, *, clock: Clock) -> ScoreResult:
    """Macro releases affect every ticker; sign of the effect depends on the
    ticker's exposure. Phase 3 keeps this simple: surprise → broad-market
    direction (positive surprise on growth indicators = bullish for SPY/QQQ;
    negative for defensives). Ticker-specific routing is a Phase 4 job.
    """
    result = ScoreResult(ticker=ticker.upper())

    surprise_z = release.get("surprise_z")
    surprise = release.get("surprise")
    importance = release.get("indicator_importance") or 3
    code = release.get("indicator_code") or release.get("code") or "?"

    z = None
    if surprise_z is not None:
        try:
            z = float(surprise_z)
        except (TypeError, ValueError):
            z = None
    if z is None and surprise is not None:
        try:
            z = float(surprise)  # crude fallback; better than nothing
        except (TypeError, ValueError):
            z = None
    if z is None:
        # No surprise data available; fast path is neutral and the slow agent
        # in Phase 4 can read the prior_value + value and reason about it.
        result.factors.append(f"No surprise z for {code}; deferring to slow path")
        return result

    weight = float(importance) / 5.0
    capped = max(-2.0, min(2.0, z))
    result.score += W_MACRO_BASE * capped * weight

    direction = "positive" if z > 0 else "negative" if z < 0 else "neutral"
    result.factors.append(
        f"{code} surprise {direction} (z={z:+.2f}, importance={importance}, weight={weight:.2f})"
    )
    return result


# ── Decision row writer ────────────────────────────────────────────────────
def _source_event(kind: str, event_id: str, extras: Optional[dict] = None) -> dict:
    out = {"kind": kind, "id": event_id}
    if extras:
        out.update(extras)
    return out


def _rationale(result: ScoreResult) -> str:
    lines = [f"## Fast-path decision: {result.rating} (confidence {result.confidence:.2f})", ""]
    if result.factors:
        lines.append("**Supporting factors:**")
        lines.extend(f"- {f}" for f in result.factors)
    if result.risk_factors:
        lines.append("")
        lines.append("**Risk factors:**")
        lines.extend(f"- {r}" for r in result.risk_factors)
    return "\n".join(lines)


def _open_paper_position_count(ticker: str, mode: str) -> int:
    rows = pg.execute(
        "SELECT COUNT(*) AS n FROM signals.paper_positions "
        "WHERE ticker = %s AND mode = %s AND status = 'open'",
        [ticker.upper(), mode],
    )
    return int(rows[0]["n"]) if rows else 0


def _get_watchlist_row(ticker: str) -> Optional[dict]:
    rows = pg.query("signals.watchlist", where={"ticker": ticker.upper()}, limit=1)
    return rows[0] if rows else None


def _write_decision(
    *,
    ticker: str,
    kind: str,
    event_id: str,
    result: ScoreResult,
    horizon_hours: int,
    mode: str,
    experiment_key: Optional[str],
    extras: Optional[dict] = None,
) -> Optional[str]:
    """Insert one signals.decisions row. Returns decision_id or None on failure."""
    row = {
        "ticker": ticker.upper(),
        "tier": "fast",
        "mode": mode,
        "supersedes": None,
        "source_event": _source_event(kind, event_id, extras),
        "rating": result.rating,
        "confidence": round(result.confidence, 3),
        "horizon_hours": horizon_hours,
        "rationale_md": _rationale(result),
        "debate_transcript": result.as_transcript(),
        "agent_hash": AGENT_HASH,
        "experiment_key": experiment_key,
        "pending": True,
    }
    try:
        with pg.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO signals.decisions "
                    "(ticker, tier, mode, supersedes, source_event, rating, confidence, "
                    " horizon_hours, rationale_md, debate_transcript, agent_hash, experiment_key, pending) "
                    "VALUES (%(ticker)s, %(tier)s, %(mode)s, %(supersedes)s, %(source_event)s, %(rating)s, "
                    "        %(confidence)s, %(horizon_hours)s, %(rationale_md)s, %(debate_transcript)s, "
                    "        %(agent_hash)s, %(experiment_key)s, %(pending)s) "
                    "RETURNING decision_id",
                    {k: (pg.Json(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
                    if hasattr(pg, "Json")
                    else _adapt(row),
                )
                rec = cur.fetchone()
                return str(rec[0]) if rec else None
    except Exception:
        _log.exception("failed to write decision for %s (%s)", ticker, kind)
        return None


def _adapt(row: dict) -> dict:
    """Fallback adapter for dict/list → JSONB when pg.Json isn't re-exported."""
    from psycopg2.extras import Json
    return {k: (Json(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}


# ── Dispatcher entry points ────────────────────────────────────────────────
def handle_article_event(
    article: dict,
    tickers: list[str],
    *,
    clock: Clock,
    price_source: PriceSource,
    mode: str = "live",
    experiment_key: Optional[str] = None,
) -> list[str]:
    """Score the article for each watchlist ticker, write decisions, fill if actionable.

    Returns the list of decision_ids written — the dispatcher hands these
    to the slow agent so each fast decision gets a paired slow run."""
    written: list[str] = []
    for ticker in tickers:
        watch = _get_watchlist_row(ticker)
        if not watch or not watch.get("active"):
            continue
        result = score_article_for_ticker(article, ticker, clock=clock)
        decision_id = _write_decision(
            ticker=ticker,
            kind="article",
            event_id=str(article["article_id"]),
            result=result,
            horizon_hours=int(watch.get("horizon_hours") or 24),
            mode=mode,
            experiment_key=experiment_key,
        )
        if not decision_id:
            continue
        written.append(decision_id)
        if result.rating in ACTIONABLE:
            _maybe_open_position(decision_id, ticker, result, watch, clock=clock, price_source=price_source, mode=mode)
    return written


def handle_post_event(
    post: dict,
    tickers: list[str],
    *,
    clock: Clock,
    price_source: PriceSource,
    mode: str = "live",
    experiment_key: Optional[str] = None,
    handle_weight: float = 1.0,
) -> list[str]:
    """Score a cashtag-tagged post for each watchlist ticker it mentions.

    `handle_weight` is the curator-set impact multiplier from social.handles;
    the dispatcher passes through 1.0 for untracked authors."""
    written: list[str] = []
    for ticker in tickers:
        watch = _get_watchlist_row(ticker)
        if not watch or not watch.get("active"):
            continue
        result = score_post_for_ticker(post, ticker, clock=clock, handle_weight=handle_weight)
        decision_id = _write_decision(
            ticker=ticker,
            kind="post",
            event_id=str(post["post_id"]),
            result=result,
            horizon_hours=int(watch.get("horizon_hours") or 24),
            mode=mode,
            experiment_key=experiment_key,
        )
        if not decision_id:
            continue
        written.append(decision_id)
        if result.rating in ACTIONABLE:
            _maybe_open_position(decision_id, ticker, result, watch, clock=clock, price_source=price_source, mode=mode)
    return written


def handle_release_event(
    release: dict,
    watchlist_tickers: list[str],
    *,
    clock: Clock,
    price_source: PriceSource,
    mode: str = "live",
    experiment_key: Optional[str] = None,
) -> list[str]:
    written: list[str] = []
    for ticker in watchlist_tickers:
        watch = _get_watchlist_row(ticker)
        if not watch or not watch.get("active"):
            continue
        result = score_macro_for_ticker(release, ticker, watch, clock=clock)
        if abs(result.score) < 0.5:
            # Too weak to act on; skip writing a row to avoid table bloat
            # on every CPI release × every watchlist ticker.
            continue
        decision_id = _write_decision(
            ticker=ticker,
            kind="macro_release",
            event_id=str(release["release_id"]),
            result=result,
            horizon_hours=int(watch.get("horizon_hours") or 24),
            mode=mode,
            experiment_key=experiment_key,
            extras={"indicator_code": release.get("indicator_code")},
        )
        if not decision_id:
            continue
        written.append(decision_id)
        if result.rating in ACTIONABLE:
            _maybe_open_position(decision_id, ticker, result, watch, clock=clock, price_source=price_source, mode=mode)
    return written


# ── Position open helper ───────────────────────────────────────────────────
def _maybe_open_position(
    decision_id: str,
    ticker: str,
    result: ScoreResult,
    watchlist_row: dict,
    *,
    clock: Clock,
    price_source: PriceSource,
    mode: str,
) -> None:
    """Open a paper position if none is currently open for this ticker+mode."""
    if _open_paper_position_count(ticker, mode) > 0:
        _log.info("skip fill: %s already has open %s position", ticker, mode)
        return
    # Late import to avoid a module-load cycle (paper.execute can use fast_signal
    # in the future for size hints).
    from trader.paper.execute import open_position

    try:
        position_id = open_position(
            decision_id=decision_id,
            ticker=ticker,
            rating=result.rating,
            confidence=result.confidence,
            watchlist_row=watchlist_row,
            clock=clock,
            price_source=price_source,
            mode=mode,
        )
        if position_id:
            _log.info("opened %s position %s for %s (decision %s)", mode, position_id, ticker, decision_id)
    except Exception:
        _log.exception("open_position failed for %s (decision %s)", ticker, decision_id)
