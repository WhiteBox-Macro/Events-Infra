"""Past-context retrieval.

The research manager's prompt is injected with a `past_context` block
containing:
  * up to N same-ticker decisions (most recent first), formatted as
    [date | ticker | rating | raw_return | alpha_return | holding_hours]
    DECISION:
    <decision rationale>
    REFLECTION:
    <reflection_md if pending=false>
  * up to N cross-ticker reflections (most recent first), short form.

Lifted from TradingAgents tradingagents/agents/utils/memory.py
get_past_context() but rewritten as a SQL query against signals.decisions
(no markdown file). Pending entries (raw_return is NULL) are excluded —
they have no learning signal yet.

`past_context_for(ticker)` is the only public entry point. Returns "" when
the table is empty so prompts degrade gracefully on a fresh deploy.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

from dbkit import pg

_log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def past_context_for(ticker: str) -> str:
    ticker = ticker.upper()
    n_same = _env_int("PAST_CONTEXT_SAME_TICKER", 5)
    n_cross = _env_int("PAST_CONTEXT_CROSS_TICKER", 3)

    same = _same_ticker(ticker, n_same)
    cross = _cross_ticker(ticker, n_cross)
    if not same and not cross:
        return ""

    parts: list[str] = []
    if same:
        parts.append(f"Past analyses of {ticker} (most recent first):")
        parts.extend(_format_full(r) for r in same)
    if cross:
        parts.append("\nRecent cross-ticker lessons:")
        parts.extend(_format_short(r) for r in cross)
    return "\n\n".join(parts)


def _same_ticker(ticker: str, limit: int) -> list[dict]:
    try:
        return pg.execute(
            "SELECT ticker, rating, raw_return, alpha_return, holding_hours, "
            "       rationale_md, reflection_md, created_at "
            "FROM signals.decisions "
            "WHERE ticker = %s "
            "  AND tier = 'slow' "
            "  AND pending = FALSE "
            "  AND alpha_return IS NOT NULL "
            "ORDER BY created_at DESC "
            "LIMIT %s",
            [ticker, limit],
        )
    except Exception:
        _log.exception("same-ticker past-context query failed")
        return []


def _cross_ticker(ticker: str, limit: int) -> list[dict]:
    try:
        return pg.execute(
            "SELECT ticker, rating, raw_return, alpha_return, reflection_md, created_at "
            "FROM signals.decisions "
            "WHERE ticker != %s "
            "  AND tier = 'slow' "
            "  AND pending = FALSE "
            "  AND reflection_md IS NOT NULL "
            "ORDER BY created_at DESC "
            "LIMIT %s",
            [ticker, limit],
        )
    except Exception:
        _log.exception("cross-ticker past-context query failed")
        return []


def _format_full(r: dict) -> str:
    date = r["created_at"].strftime("%Y-%m-%d") if r.get("created_at") else "?"
    tag = (
        f"[{date} | {r.get('ticker')} | {r.get('rating')} | "
        f"raw={_pct(r.get('raw_return'))} | alpha={_pct(r.get('alpha_return'))} | "
        f"{_hours(r.get('holding_hours'))}h]"
    )
    lines = [tag]
    if r.get("rationale_md"):
        lines.append(f"DECISION:\n{r['rationale_md']}")
    if r.get("reflection_md"):
        lines.append(f"REFLECTION:\n{r['reflection_md']}")
    return "\n\n".join(lines)


def _format_short(r: dict) -> str:
    date = r["created_at"].strftime("%Y-%m-%d") if r.get("created_at") else "?"
    tag = (
        f"[{date} | {r.get('ticker')} | {r.get('rating')} | "
        f"alpha={_pct(r.get('alpha_return'))}]"
    )
    reflection = r.get("reflection_md") or ""
    return f"{tag}\n{reflection}"


def _pct(v) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v)*100:+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _hours(v) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return "n/a"
