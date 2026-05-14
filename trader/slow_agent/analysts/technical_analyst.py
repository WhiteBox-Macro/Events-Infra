"""Technical analyst.

Writes into state.market_report (TradingAgents-compatible field name).

Inputs: cached prices from signals.price_cache. On a fresh deploy this is
sparse; the prompt handles the "<no price data>" case by telling the
analyst to flag the gap rather than fabricate a chart read.
"""
from __future__ import annotations

from typing import Any

from trader.slow_agent import tools
from trader.slow_agent.llm import get_quick_llm
from trader.slow_agent.state import AgentState

SYSTEM = (
    "You are a technical analyst for a multi-agent trading desk.\n"
    "Below is the cached price series for {ticker}. The data may be sparse "
    "(we're early in the deployment); when that's the case, say so plainly "
    "and limit your analysis to what the series actually shows.\n"
    "Write a concise report (≤200 words) covering:\n"
    "  1. short-term trend direction and recent magnitude,\n"
    "  2. proximity to local high / low,\n"
    "  3. any divergence vs. the fundamentals/news already-cached signal "
    "(you'll see it later — for now just describe the price action),\n"
    "  4. one-line 'technical bias for {ticker}:' summary in "
    "{{bullish | mixed | bearish | insufficient_data}}."
)


def create_technical_analyst():
    def node(state: AgentState) -> dict:
        ticker = state["ticker"]
        clock = state.get("decision_clock")
        prices = tools.recent_price_history(ticker, decision_clock=clock)
        prompt = SYSTEM.format(ticker=ticker) + "\n\n---\n" + prices
        resp: Any = get_quick_llm().invoke(prompt)
        return {"market_report": getattr(resp, "content", str(resp))}

    return node
