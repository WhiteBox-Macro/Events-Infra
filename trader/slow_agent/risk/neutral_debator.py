"""Neutral risk analyst — synthesises the two extremes, surfaces the
risk-reward trade-off the others underweighted."""
from __future__ import annotations

from typing import Any

from trader.slow_agent.llm import get_quick_llm
from trader.slow_agent.state import AgentState, RiskDebateState

PROMPT = """You are the Neutral Risk Analyst. Synthesise the aggressive and conservative views on {ticker}. Point out where each side overstates its case. Identify the specific risk-reward trade-off the trader's plan is taking and whether it's symmetric.

Resources:
News report: {news_report}
Social report: {sentiment_report}
Technical report: {market_report}
Macro report: {macro_report}

Trader's proposed transaction:
{trader_plan}

Conversation history so far: {history}
Last aggressive argument: {current_aggressive}
Last conservative argument: {current_conservative}

Output a punchy, conversational argument — no bullet points, no boilerplate. Defend the neutral case directly."""


def create_neutral_debator():
    llm = get_quick_llm(temperature=0.4)

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
            current_aggressive=risk.get("current_aggressive_response", ""),
            current_conservative=risk.get("current_conservative_response", ""),
        )
        resp: Any = llm.invoke(prompt)
        argument = f"Neutral Analyst: {getattr(resp, 'content', str(resp))}"
        new_risk: RiskDebateState = RiskDebateState(
            history=(risk.get("history", "") + "\n" + argument).strip(),
            neutral_history=(risk.get("neutral_history", "") + "\n" + argument).strip(),
            aggressive_history=risk.get("aggressive_history", ""),
            conservative_history=risk.get("conservative_history", ""),
            latest_speaker="Neutral",
            current_neutral_response=argument,
            current_aggressive_response=risk.get("current_aggressive_response", ""),
            current_conservative_response=risk.get("current_conservative_response", ""),
            judge_decision=risk.get("judge_decision", ""),
            count=(risk.get("count") or 0) + 1,
        )
        return {"risk_debate_state": new_risk}

    return node
