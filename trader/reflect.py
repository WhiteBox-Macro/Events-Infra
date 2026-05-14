"""Post-mortem reflection job.

For each newly-resolved decision (pending=false, raw_return IS NOT NULL,
reflection_md IS NULL), generate a 2-4 sentence reflection via the fast LLM
and write it to signals.decisions.reflection_md.

The reflection text is then re-injected into future analyst runs via
trader.slow_agent.memory.past_context_for(ticker) — that's the feedback
loop the system uses to learn across runs without any model fine-tuning.

Prompt is lifted verbatim from TauricResearch/TradingAgents
tradingagents/graph/reflection.py:_get_log_reflection_prompt — it's tightly
tuned, no reason to redo.

Gate: skipped cleanly if SLOW_AGENT_ENABLED=false or no ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from dbkit import pg
from trader.slow_agent.llm import get_quick_llm, is_enabled

_log = logging.getLogger(__name__)

DEFAULT_BATCH_LIMIT = 25

REFLECTION_PROMPT = (
    "You are a trading analyst reviewing your own past decision now that the outcome is known.\n"
    "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
    "Cover in order:\n"
    "1. Was the directional call correct? (cite the alpha figure)\n"
    "2. Which part of the investment thesis held or failed?\n"
    "3. One concrete lesson to apply to the next similar analysis.\n\n"
    "Be specific and terse. Your output will be stored verbatim in a decision log "
    "and re-read by future analysts, so every word must earn its place."
)


def _unreflected(limit: int) -> list[dict]:
    return pg.execute(
        "SELECT decision_id, ticker, rating, raw_return, alpha_return, holding_hours, "
        "       rationale_md, tier, mode "
        "FROM signals.decisions "
        "WHERE pending = FALSE "
        "  AND raw_return IS NOT NULL "
        "  AND reflection_md IS NULL "
        "ORDER BY created_at ASC "  # oldest first so the backlog drains fairly
        "LIMIT %s",
        [limit],
    )


def _format_user_message(row: dict) -> str:
    raw = float(row["raw_return"]) if row.get("raw_return") is not None else None
    alpha = row.get("alpha_return")
    alpha = float(alpha) if alpha is not None else None
    raw_pct = f"{raw*100:+.2f}%" if raw is not None else "n/a"
    alpha_pct = f"{alpha*100:+.2f}%" if alpha is not None else "n/a"
    hold = row.get("holding_hours")
    hold_str = f"{float(hold):.1f}" if hold is not None else "n/a"

    return (
        f"Ticker: {row.get('ticker')}\n"
        f"Tier: {row.get('tier')}\n"
        f"Rating issued: {row.get('rating')}\n"
        f"Raw return: {raw_pct}\n"
        f"Alpha vs benchmark: {alpha_pct}\n"
        f"Holding hours: {hold_str}\n\n"
        f"Rationale at decision time:\n{row.get('rationale_md') or ''}"
    )


def _reflect_one(row: dict) -> Optional[str]:
    llm = get_quick_llm(temperature=0.2)
    msg = _format_user_message(row)
    resp = llm.invoke([("system", REFLECTION_PROMPT), ("human", msg)])
    text = getattr(resp, "content", str(resp)).strip()
    if not text:
        return None
    return text


def run_once(*, limit: Optional[int] = None) -> dict:
    if not is_enabled():
        _log.info("reflect: slow agent disabled; skipping")
        return {"reflected": 0, "skipped": 0, "limit_reached": False}

    cap = limit if limit is not None else int(os.environ.get("REFLECT_BATCH_LIMIT", DEFAULT_BATCH_LIMIT))
    rows = _unreflected(cap)
    if not rows:
        return {"reflected": 0, "skipped": 0, "limit_reached": False}

    reflected = 0
    skipped = 0
    for row in rows:
        try:
            text = _reflect_one(row)
        except Exception:
            _log.exception("reflect: LLM call failed for decision %s", row["decision_id"])
            skipped += 1
            continue
        if not text:
            skipped += 1
            continue
        try:
            pg.update("signals.decisions", {"reflection_md": text}, {"decision_id": row["decision_id"]})
            reflected += 1
        except Exception:
            _log.exception("reflect: DB write failed for decision %s", row["decision_id"])
            skipped += 1
    return {"reflected": reflected, "skipped": skipped, "limit_reached": len(rows) == cap}
