"""Bear researcher. Mirror of bull_researcher with adversarial prompt.

Lifted from TradingAgents tradingagents/agents/researchers/bear_researcher.py
with the same two adaptations (our state field names + fast_decision /
past_context injection)."""
from __future__ import annotations

from typing import Any

from trader.slow_agent.llm import get_quick_llm
from trader.slow_agent.state import AgentState, InvestDebateState
from trader.slow_agent.researchers.bull_researcher import _fast_summary

PROMPT = """You are a Bear Analyst making the case against investing in {ticker}. Present a well-reasoned argument emphasizing risks, challenges, and negative indicators. Leverage the provided research and data to highlight potential downsides and counter bullish arguments effectively.

Key points to focus on:
- Risks and Challenges: Highlight factors like market saturation, financial instability, or macroeconomic threats that could hinder the stock's performance.
- Competitive Weaknesses: Emphasize vulnerabilities such as weaker market positioning, declining innovation, or threats from competitors.
- Negative Indicators: Use evidence from financial data, market trends, or recent adverse news to support your position.
- Bull Counterpoints: Critically analyze the bull argument with specific data and sound reasoning, exposing weaknesses or over-optimistic assumptions.
- Engagement: Present your argument in a conversational style, directly engaging with the bull analyst's points and debating effectively rather than simply listing facts.

Resources available:
Technical market report: {market_report}
Social media sentiment report: {sentiment_report}
Latest news report: {news_report}
Macro context report: {macro_report}

Fast-path baseline:
{fast_summary}

Past context:
{past_context}

Conversation history of the debate: {history}
Last bull argument: {current_response}

Use this information to deliver a compelling bear argument, refute the bull's claims, and engage in a dynamic debate that demonstrates the risks and weaknesses of investing in {ticker}."""


def create_bear_researcher():
    llm = get_quick_llm(temperature=0.5)

    def node(state: AgentState) -> dict:
        debate: InvestDebateState = state.get("investment_debate_state") or {}
        prompt = PROMPT.format(
            ticker=state.get("ticker", "?"),
            market_report=state.get("market_report", ""),
            sentiment_report=state.get("sentiment_report", ""),
            news_report=state.get("news_report", ""),
            macro_report=state.get("macro_report", ""),
            fast_summary=_fast_summary(state.get("fast_decision")),
            past_context=state.get("past_context", "(no prior context)"),
            history=debate.get("history", ""),
            current_response=debate.get("current_response", ""),
        )
        resp: Any = llm.invoke(prompt)
        argument = f"Bear Analyst: {getattr(resp, 'content', str(resp))}"
        new_debate: InvestDebateState = InvestDebateState(
            history=(debate.get("history", "") + "\n" + argument).strip(),
            bear_history=(debate.get("bear_history", "") + "\n" + argument).strip(),
            bull_history=debate.get("bull_history", ""),
            current_response=argument,
            judge_decision=debate.get("judge_decision", ""),
            count=(debate.get("count") or 0) + 1,
        )
        return {"investment_debate_state": new_debate}

    return node
