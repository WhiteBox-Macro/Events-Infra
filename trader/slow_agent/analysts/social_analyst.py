"""Social analyst.

Writes into state.sentiment_report (TradingAgents-compatible field name so
bull/bear prompts work unmodified).

Inputs: recent social.posts mentioning the ticker, tallied bullish/bearish
counts pre-computed by tools.recent_social_for_ticker.
"""
from __future__ import annotations

from typing import Any

from trader.slow_agent import tools
from trader.slow_agent.llm import get_quick_llm
from trader.slow_agent.state import AgentState

SYSTEM = (
    "You are a social-media analyst for a multi-agent trading desk.\n"
    "Below are recent posts from Reddit, StockTwits, and (when enabled) X "
    "mentioning {ticker}. Write a concise report (≤250 words) that:\n"
    "  1. summarises the prevailing retail / social tone,\n"
    "  2. flags any single high-influence post (≥100k followers) that's worth weighting,\n"
    "  3. distinguishes 'noise / meme' from substantive commentary,\n"
    "  4. ends with a one-line 'tone:' summary in {{bullish | mixed | bearish}}.\n"
    "Quote handles and timestamps where useful. Do not invent posts."
)


def create_social_analyst():
    def node(state: AgentState) -> dict:
        ticker = state["ticker"]
        clock = state.get("decision_clock")
        posts = tools.recent_social_for_ticker(ticker, decision_clock=clock)
        prompt = SYSTEM.format(ticker=ticker) + "\n\n---\n" + posts
        resp: Any = get_quick_llm().invoke(prompt)
        return {"sentiment_report": getattr(resp, "content", str(resp))}

    return node
