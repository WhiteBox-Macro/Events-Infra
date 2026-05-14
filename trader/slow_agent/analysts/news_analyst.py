"""News analyst.

Pre-fetches recent ticker-specific + global news from Postgres and asks the
fast LLM to write a focused report. The output goes into state.news_report
where the bull/bear researchers can read it.

Prompt structure follows TradingAgents' news_analyst.py but trimmed: we
don't bind tools because the data is already in the prompt (one fewer
round trip per analyst run).
"""
from __future__ import annotations

from typing import Any

from trader.slow_agent import tools
from trader.slow_agent.llm import get_quick_llm
from trader.slow_agent.state import AgentState

SYSTEM = (
    "You are a news analyst writing for a multi-agent trading desk.\n"
    "You will be shown ticker-specific news and broader global news from the "
    "last few days. Produce a concise report (≤300 words, no preamble) that:\n"
    "  1. surfaces the 2-3 most market-moving items for {ticker},\n"
    "  2. notes any contradictions between headlines,\n"
    "  3. ends with a one-line 'tone:' summary in {{bullish | mixed | bearish}}.\n"
    "Be specific and quote dates. Do not speculate beyond the data shown."
)


def create_news_analyst():
    def node(state: AgentState) -> dict:
        ticker = state["ticker"]
        clock = state.get("decision_clock")

        ticker_news = tools.recent_news_for_ticker(ticker, decision_clock=clock)
        global_news = tools.recent_global_news(decision_clock=clock)

        prompt = (
            SYSTEM.format(ticker=ticker)
            + "\n\n---\n"
            + f"## Ticker-specific news\n{ticker_news}\n\n"
            + f"## Global news\n{global_news}\n"
        )
        llm = get_quick_llm()
        resp: Any = llm.invoke(prompt)
        text = getattr(resp, "content", str(resp))
        return {"news_report": text}

    return node
