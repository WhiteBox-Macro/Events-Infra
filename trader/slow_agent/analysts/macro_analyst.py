"""Macro analyst.

Writes into state.macro_report. The TradingAgents reference rolls macro
into a single 'news' analyst — we split it out because our schema
explicitly separates macro.releases from news.articles, and the slow
agent should reason about CPI surprises differently from a single-stock
headline.
"""
from __future__ import annotations

from typing import Any

from trader.slow_agent import tools
from trader.slow_agent.llm import get_quick_llm
from trader.slow_agent.state import AgentState

SYSTEM = (
    "You are a macro analyst for a multi-agent trading desk.\n"
    "Below are the most recent US macro releases. For each, the surprise vs "
    "consensus is shown (z-scored when available). Write a concise report "
    "(≤250 words) that:\n"
    "  1. identifies the 1-2 highest-impact prints in the window,\n"
    "  2. names the prevailing regime (risk-on / risk-off / mixed) with a "
    "short justification anchored to specific releases,\n"
    "  3. notes how this regime is expected to weigh on {ticker} given its "
    "rough sector profile (you may infer the sector from the ticker name; "
    "if unclear, say so),\n"
    "  4. ends with a one-line 'macro tilt for {ticker}:' summary in "
    "{{bullish | mixed | bearish}}.\n"
    "If no releases are present in the window, say so explicitly and exit."
)


def create_macro_analyst():
    def node(state: AgentState) -> dict:
        ticker = state["ticker"]
        clock = state.get("decision_clock")
        macro = tools.recent_macro_releases(decision_clock=clock)
        prompt = SYSTEM.format(ticker=ticker) + "\n\n---\n" + macro
        resp: Any = get_quick_llm().invoke(prompt)
        return {"macro_report": getattr(resp, "content", str(resp))}

    return node
