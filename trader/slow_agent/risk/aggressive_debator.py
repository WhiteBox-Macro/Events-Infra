"""Aggressive risk analyst — championships high-reward / high-conviction takes.

Lifted from TradingAgents tradingagents/agents/risk_mgmt/aggressive_debator.py
with state field names matching ours.
"""
from __future__ import annotations

from typing import Any

from trader.slow_agent.llm import get_quick_llm
from trader.slow_agent.state import AgentState, RiskDebateState

PROMPT = """You are the Aggressive Risk Analyst. Champion high-reward, high-conviction takes on {ticker}. Counter the conservative and neutral views with data-driven rebuttals; show where their caution misses critical opportunity or over-weights tail risk.

Resources:
News report: {news_report}
Social report: {sentiment_report}
Technical report: {market_report}
Macro report: {macro_report}

Trader's proposed transaction:
{trader_plan}

Conversation history so far: {history}
Last conservative argument: {current_conservative}
Last neutral argument: {current_neutral}

Output a punchy, conversational argument — no bullet points, no boilerplate. Defend the aggressive case directly."""


def create_aggressive_debator():
    llm = get_quick_llm(temperature=0.6)

    def node(state: AgentState) -> dict:
        risk: RiskDebateState = state.get("risk_debate_state") or {}
        prompt = PROMPT.format(
            ticker=state.get("ticker", "?"),
            news_report=state.get("news_report", ""),
            sentiment_report=state.get("sentiment_report", ""),
            market_report=state.get("market_report", ""),
            macro_report=state.get("macro_report", ""),
            trader_plan=state.get("trader_investment_plan", ""),
            history=risk.get("history", ""),
            current_conservative=risk.get("current_conservative_response", ""),
            current_neutral=risk.get("current_neutral_response", ""),
        )
        resp: Any = llm.invoke(prompt)
        argument = f"Aggressive Analyst: {getattr(resp, 'content', str(resp))}"
        new_risk: RiskDebateState = RiskDebateState(
            history=(risk.get("history", "") + "\n" + argument).strip(),
            aggressive_history=(risk.get("aggressive_history", "") + "\n" + argument).strip(),
            conservative_history=risk.get("conservative_history", ""),
            neutral_history=risk.get("neutral_history", ""),
            latest_speaker="Aggressive",
            current_aggressive_response=argument,
            current_conservative_response=risk.get("current_conservative_response", ""),
            current_neutral_response=risk.get("current_neutral_response", ""),
            judge_decision=risk.get("judge_decision", ""),
            count=(risk.get("count") or 0) + 1,
        )
        return {"risk_debate_state": new_risk}

    return node
