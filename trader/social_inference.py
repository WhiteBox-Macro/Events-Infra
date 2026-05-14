"""trader/social_inference.py — theme→ticker inference for tracked accounts.

When the dispatcher receives a post from a `social.handles` author and
trader.tickers.resolve_tickers cannot extract a watchlist ticker (because
the post talks about *themes* — war, tariffs, monetary policy — rather than
named companies), we route here instead of dropping the event.

What this does:
  1. Pull the post body + author profile + the active watchlist (with the
     watchlist row's notes/tags as sector hints).
  2. Cheap Haiku call: "Given this post from <handle>, which watchlist
     tickers will materially move and in which direction?"
  3. Parse the JSON reply into a list of {ticker, direction, confidence, reasoning}.
  4. For each item above SOCIAL_INFERENCE_CONFIDENCE_FLOOR (default 0.4):
       * write signals.decisions(tier='fast') with a "theme inference" rationale,
       * open a paper position if the rating is actionable.
  5. Return decision_ids so the dispatcher fires the slow agent on them.

Why this still counts as "fast path":
  * One LLM call per inbound post (not per ticker × per debate round).
  * Bounded latency (~500ms-1.5s for Haiku).
  * Watchlist-constrained output — no chance of trading something we don't
    cover.
  * The slow agent (Phase 4) still runs afterward for full debate; this just
    seeds it with a useful prior instead of a blank slate.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Any, Optional

from dbkit import pg
from trader.clock import Clock
from trader.fast_signal import (  # reuse existing decision/position helpers
    ACTIONABLE,
    AGENT_HASH as FAST_AGENT_HASH,
    ScoreResult,
    _maybe_open_position,
    _write_decision,
)
from trader.prices import PriceSource

_log = logging.getLogger(__name__)

AGENT_HASH = "fast_signal_social_inference_v1"


# ── Config ─────────────────────────────────────────────────────────────────
def _confidence_floor() -> float:
    try:
        return float(os.environ.get("SOCIAL_INFERENCE_CONFIDENCE_FLOOR", "0.40"))
    except (TypeError, ValueError):
        return 0.40


def _max_picks() -> int:
    try:
        return int(os.environ.get("SOCIAL_INFERENCE_MAX_PICKS", "5"))
    except (TypeError, ValueError):
        return 5


# ── Data fetchers ──────────────────────────────────────────────────────────
def _load_handle(username: Optional[str]) -> Optional[dict]:
    if not username:
        return None
    rows = pg.execute(
        "SELECT handle_id, username, display_name, category, tags, "
        "       expected_themes, impact_weight "
        "FROM social.handles "
        "WHERE platform = 'twitter' AND lower(username) = lower(%s) AND active = TRUE "
        "LIMIT 1",
        [username],
    )
    return rows[0] if rows else None


def _watchlist_view() -> list[dict]:
    """Return active watchlist with the optional `notes` field — passed into
    the LLM prompt as sector hints. The user can keep these short
    ("megacap tech", "oil major", "small-cap defense")."""
    return pg.execute(
        "SELECT ticker, horizon_hours, max_position_pct, benchmark_ticker, notes "
        "FROM signals.watchlist WHERE active = TRUE ORDER BY ticker"
    )


# ── LLM call ───────────────────────────────────────────────────────────────
SYSTEM = (
    "You are a fast inference layer for a multi-agent trading desk. "
    "Given a single social-media post from a tracked, market-moving account, "
    "your job is to identify which watchlist tickers will be materially "
    "affected and in what direction.\n\n"
    "Rules:\n"
    "  - Only pick tickers from the provided watchlist. Never invent symbols.\n"
    "  - Be conservative. Many posts have NO clear ticker impact — return [] "
    "in that case rather than forcing picks.\n"
    "  - 'direction' is one of 'long' (ticker should rise) or 'short' "
    "(ticker should fall).\n"
    "  - 'confidence' is in [0,1]: 0.4 = directionally probable, 0.7 = "
    "strong conviction, 0.9 = a clear, named catalyst.\n"
    "  - Keep 'reasoning' to one sentence per pick. Tie it to the post text.\n"
    "  - Return at most {max_picks} picks, sorted by descending confidence.\n\n"
    "Output JSON only — a single object with a 'picks' array. No prose. Example:\n"
    "  {{\"picks\": [\n"
    "     {{\"ticker\": \"LMT\", \"direction\": \"long\", \"confidence\": 0.72, \n"
    "      \"reasoning\": \"Heightened Iran tensions historically lift defense primes.\"}},\n"
    "     {{\"ticker\": \"XOM\", \"direction\": \"long\", \"confidence\": 0.55, \n"
    "      \"reasoning\": \"Middle East risk premium typically bids crude → integrateds.\"}}\n"
    "  ]}}"
)


def _build_prompt(*, post: dict, handle: dict, watchlist: list[dict]) -> str:
    wl_lines = []
    for w in watchlist:
        line = f"  {w['ticker']}"
        if w.get("notes"):
            line += f" — {w['notes']}"
        wl_lines.append(line)
    wl_block = "\n".join(wl_lines) if wl_lines else "  (empty)"

    body = (post.get("body") or "").strip()
    author = post.get("author") or handle.get("username") or "?"
    posted = post.get("posted_at")
    posted_str = posted.isoformat() if posted else "?"
    tags = ", ".join(handle.get("tags") or []) or "—"
    themes = ", ".join(handle.get("expected_themes") or []) or "—"

    return (
        f"## Post\n"
        f"Author: @{author} ({handle.get('display_name') or '—'}, category={handle.get('category')})\n"
        f"Posted at: {posted_str}\n"
        f"Tags: {tags}\n"
        f"Expected themes: {themes}\n\n"
        f"Body:\n{body}\n\n"
        f"## Active watchlist (ticker — optional sector note)\n"
        f"{wl_block}\n\n"
        f"Output JSON now."
    )


def _invoke_llm(prompt: str) -> Optional[dict]:
    """Call Haiku via langchain_anthropic. Returns parsed JSON dict or None."""
    try:
        from trader.slow_agent.llm import get_quick_llm
        llm = get_quick_llm(temperature=0.2)
    except Exception:
        _log.exception("social_inference: failed to construct LLM")
        return None
    try:
        sys_msg = SYSTEM.format(max_picks=_max_picks())
        resp = llm.invoke([("system", sys_msg), ("human", prompt)])
    except Exception:
        _log.exception("social_inference: LLM call failed")
        return None

    raw = getattr(resp, "content", str(resp)).strip()
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("social_inference: LLM returned non-JSON: %s", raw[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


# ── Pick → ScoreResult / decision row ──────────────────────────────────────
def _watchlist_set(watchlist: list[dict]) -> set[str]:
    return {w["ticker"].upper() for w in watchlist if w.get("ticker")}


def _watchlist_row_map(watchlist: list[dict]) -> dict[str, dict]:
    return {w["ticker"].upper(): w for w in watchlist if w.get("ticker")}


def _make_score(pick: dict, handle: dict) -> Optional[ScoreResult]:
    """Translate one LLM pick into a ScoreResult our existing fast path
    helpers know how to consume."""
    ticker = (pick.get("ticker") or "").upper()
    direction = (pick.get("direction") or "").lower()
    try:
        conf = float(pick.get("confidence") or 0)
    except (TypeError, ValueError):
        return None
    if not ticker or direction not in ("long", "short") or conf <= 0:
        return None

    impact = float(handle.get("impact_weight") or 1.0)

    # Convert confidence (0..1) into a fast-signal score. The bands are:
    #   conf 0.9 × impact 3.0 = score 3.6 → Buy
    #   conf 0.5 × impact 1.5 = score 1.05 → Hold edge → would NOT trade
    # Scaling: score = 4.0 * confidence * impact_weight (so 1.0×1.0 gives 4.0).
    score_mag = 4.0 * conf * impact
    score = score_mag if direction == "long" else -score_mag

    reasoning = (pick.get("reasoning") or "").strip() or "(no reasoning provided)"
    factor = f"@{handle.get('username')} ({handle.get('category')}): {reasoning}"
    result = ScoreResult(ticker=ticker, score=score)
    if direction == "long":
        result.factors.append(factor)
    else:
        result.risk_factors.append(factor)
    result.factors.append(f"impact_weight={impact} × LLM confidence={conf:.2f}")
    return result


# ── Public entry point ────────────────────────────────────────────────────
def handle_influencer_post(
    post: dict,
    *,
    clock: Clock,
    price_source: PriceSource,
    mode: str = "live",
    experiment_key: Optional[str] = None,
) -> list[str]:
    """Route a post from a tracked handle through LLM theme inference.

    Returns the list of decision_ids the dispatcher should hand to the slow
    agent. Empty list when the inference produces no actionable picks (the
    common case for non-market-moving posts — daily-life chatter, etc.).
    """
    handle = _load_handle(post.get("author"))
    if not handle:
        # Defensive — caller should have already verified the author is tracked.
        return []
    watchlist = _watchlist_view()
    if not watchlist:
        return []

    prompt = _build_prompt(post=post, handle=handle, watchlist=watchlist)
    payload = _invoke_llm(prompt)
    if not payload:
        return []

    picks = payload.get("picks") or []
    if not isinstance(picks, list):
        return []

    floor = _confidence_floor()
    valid_tickers = _watchlist_set(watchlist)
    row_map = _watchlist_row_map(watchlist)

    decision_ids: list[str] = []
    for pick in picks[: _max_picks()]:
        if not isinstance(pick, dict):
            continue
        ticker = (pick.get("ticker") or "").upper()
        if ticker not in valid_tickers:
            _log.info("social_inference: skipping non-watchlist pick %s", ticker)
            continue
        try:
            conf = float(pick.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        if conf < floor:
            continue

        result = _make_score(pick, handle)
        if not result:
            continue

        watch = row_map.get(ticker, {})
        decision_id = _write_decision(
            ticker=ticker,
            kind="post",
            event_id=str(post["post_id"]),
            result=result,
            horizon_hours=int(watch.get("horizon_hours") or 24),
            mode=mode,
            experiment_key=experiment_key,
            extras={
                "agent_path": "social_inference",
                "handle": handle.get("username"),
                "handle_category": handle.get("category"),
                "llm_confidence": conf,
            },
        )
        if not decision_id:
            continue
        decision_ids.append(decision_id)
        if result.rating in ACTIONABLE:
            _maybe_open_position(
                decision_id, ticker, result, watch,
                clock=clock, price_source=price_source, mode=mode,
            )
    if decision_ids:
        _log.info(
            "social_inference: @%s post %s → %d decision(s) %s",
            handle.get("username"), post.get("post_id"),
            len(decision_ids), decision_ids,
        )
    return decision_ids
