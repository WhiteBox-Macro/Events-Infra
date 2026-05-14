"""Slow-agent runner — single entry point called by the dispatcher.

Input:
  fast_decision_id — the row that fired the slow path.
  ticker           — which ticker to score.
  mode             — 'live' or 'backtest'.
  experiment_key   — optional tag for backtest experiment grouping.

Output:
  A new row in signals.decisions(tier='slow', supersedes=<fast_decision_id>),
  with the portfolio manager's structured rating / confidence / horizon /
  rationale, plus the full debate_transcript JSONB (analyst reports + bull /
  bear / risk histories).

Postgres checkpointer:
  We use `langgraph-checkpoint-postgres`'s PostgresSaver pointed at the
  same DATABASE_URL. thread_id is sha256(ticker + decision_clock +
  experiment_key)[:16] — deterministic so a retry resumes the right thread.

Failure modes:
  - SLOW_AGENT_ENABLED=false or no API key → no-op (logs and returns).
  - LangGraph compile fails (deps missing) → log + skip.
  - Pydantic structured-output fails on any node → that node's fallback
    keeps the run finishable; we still get a Hold row written.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg2.extras import Json

from dbkit import pg
from trader.slow_agent.llm import is_enabled
from trader.slow_agent.memory import past_context_for
from trader.slow_agent.state import AgentState, fresh_invest_debate_state, fresh_risk_debate_state

_log = logging.getLogger(__name__)

AGENT_HASH = "slow_agent_v1"


def thread_id(*, ticker: str, decision_clock: str, experiment_key: Optional[str]) -> str:
    raw = f"{ticker.upper()}|{decision_clock}|{experiment_key or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_fast_decision(decision_id: str) -> Optional[dict]:
    rows = pg.execute(
        "SELECT decision_id, ticker, rating, confidence, horizon_hours, "
        "       rationale_md, debate_transcript, source_event, mode, "
        "       experiment_key, created_at "
        "FROM signals.decisions WHERE decision_id = %s",
        [decision_id],
    )
    return rows[0] if rows else None


def _get_checkpointer():
    """Build a Postgres-backed langgraph checkpointer.

    Returns None on failure — runner will then run uncheckpointed, which is
    fine for short debates but means a crash mid-run forfeits any analyst
    tokens already paid for.
    """
    try:
        # The exact import path is `langgraph_checkpoint_postgres`. The
        # package version on PyPI is `langgraph-checkpoint-postgres`.
        from langgraph_checkpoint_postgres import PostgresSaver  # type: ignore[import-not-found]
    except ImportError:
        _log.warning("langgraph-checkpoint-postgres not installed; running uncheckpointed")
        return None
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        # Most versions expose a context manager class method; fall back to
        # direct construction if that signature isn't available.
        saver = PostgresSaver.from_conn_string(url) if hasattr(PostgresSaver, "from_conn_string") else PostgresSaver(url)
        if hasattr(saver, "setup"):
            saver.setup()  # idempotent — creates the checkpoint tables
        return saver
    except Exception:
        _log.exception("failed to construct PostgresSaver; running uncheckpointed")
        return None


def _build_initial_state(*, ticker: str, fast: dict, decision_clock: str,
                         mode: str, experiment_key: Optional[str]) -> AgentState:
    return AgentState(
        ticker=ticker.upper(),
        decision_clock=decision_clock,
        source_event=fast.get("source_event") or {},
        fast_decision={
            "decision_id": str(fast["decision_id"]),
            "rating": fast.get("rating"),
            "confidence": float(fast.get("confidence") or 0),
            "horizon_hours": int(fast.get("horizon_hours") or 24),
            "rationale_md": fast.get("rationale_md") or "",
            "debate_transcript": fast.get("debate_transcript") or {},
        },
        mode=mode,
        experiment_key=experiment_key,
        past_context=past_context_for(ticker),
        news_report="", sentiment_report="", market_report="",
        macro_report="", fundamentals_report="",
        investment_debate_state=fresh_invest_debate_state(),
        investment_plan="",
        trader_investment_plan="",
        risk_debate_state=fresh_risk_debate_state(),
        final_trade_decision="",
    )


def _write_slow_decision(*, ticker: str, fast_decision_id: str, final: dict,
                         debate_transcript: dict, mode: str,
                         experiment_key: Optional[str],
                         source_event: dict) -> Optional[str]:
    row = {
        "ticker": ticker.upper(),
        "tier": "slow",
        "mode": mode,
        "supersedes": fast_decision_id,
        "source_event": source_event,
        "rating": final["rating"],
        "confidence": round(float(final["confidence"]), 3),
        "horizon_hours": int(final["horizon_hours"]),
        "rationale_md": final["rationale_md"],
        "debate_transcript": debate_transcript,
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
                    {k: (Json(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()},
                )
                rec = cur.fetchone()
                return str(rec[0]) if rec else None
    except Exception:
        _log.exception("failed to write slow decision for %s", ticker)
        return None


def run_slow_for_fast_decision(
    fast_decision_id: str,
    *,
    ticker: Optional[str] = None,
    mode: Optional[str] = None,
    experiment_key: Optional[str] = None,
) -> Optional[str]:
    """Entry point. Returns the new slow decision_id (or None if skipped/failed).

    `ticker`, `mode`, `experiment_key` default to the fast row's values; the
    dispatcher passes them through unchanged. Backtest replay overrides
    `mode='backtest'` + an `experiment_key`.
    """
    if not is_enabled():
        _log.info("slow agent disabled (SLOW_AGENT_ENABLED=false or ANTHROPIC_API_KEY missing)")
        return None

    fast = _load_fast_decision(fast_decision_id)
    if not fast:
        _log.warning("slow runner: fast decision %s not found", fast_decision_id)
        return None

    ticker = (ticker or fast.get("ticker") or "").upper()
    if not ticker:
        _log.warning("slow runner: missing ticker on fast decision %s", fast_decision_id)
        return None
    mode = mode or fast.get("mode") or "live"
    experiment_key = experiment_key or fast.get("experiment_key")

    decision_clock = (fast.get("created_at") or datetime.now(timezone.utc)).isoformat()
    initial_state = _build_initial_state(
        ticker=ticker, fast=fast, decision_clock=decision_clock,
        mode=mode, experiment_key=experiment_key,
    )

    # Compile the graph (with checkpointer if available).
    try:
        from trader.slow_agent.graph import build_graph
        checkpointer = _get_checkpointer()
        graph = build_graph(checkpointer=checkpointer)
    except Exception:
        _log.exception("slow runner: graph compile failed")
        return None

    config = {
        "configurable": {
            "thread_id": thread_id(
                ticker=ticker, decision_clock=decision_clock,
                experiment_key=experiment_key,
            ),
        },
        "recursion_limit": 50,
    }

    try:
        final_state: Any = graph.invoke(initial_state, config=config)
    except Exception:
        _log.exception("slow runner: graph invocation failed for %s", ticker)
        return None

    structured = (final_state or {}).get("__final")
    if not structured:
        _log.warning("slow runner: no structured final-decision in state for %s", ticker)
        return None

    transcript = {
        "agent": AGENT_HASH,
        "thread_id": config["configurable"]["thread_id"],
        "news_report": (final_state or {}).get("news_report", ""),
        "sentiment_report": (final_state or {}).get("sentiment_report", ""),
        "macro_report": (final_state or {}).get("macro_report", ""),
        "market_report": (final_state or {}).get("market_report", ""),
        "investment_debate_state": (final_state or {}).get("investment_debate_state", {}),
        "research_plan": (final_state or {}).get("investment_plan", ""),
        "trader_proposal": (final_state or {}).get("trader_investment_plan", ""),
        "risk_debate_state": (final_state or {}).get("risk_debate_state", {}),
        "fast_decision_id": fast_decision_id,
    }
    return _write_slow_decision(
        ticker=ticker,
        fast_decision_id=fast_decision_id,
        final=structured,
        debate_transcript=transcript,
        mode=mode,
        experiment_key=experiment_key,
        source_event=fast.get("source_event") or {},
    )
