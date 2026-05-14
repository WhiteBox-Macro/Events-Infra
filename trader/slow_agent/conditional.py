"""LangGraph conditional routers.

Two round-capped loops:

  Investment debate (bull ⇄ bear):
      count >= 2 * max_debate_rounds      → Research Manager
      current_response starts with 'Bull' → Bear Researcher
      else                                → Bull Researcher

  Risk debate (aggressive ⇄ conservative ⇄ neutral):
      count >= 3 * max_risk_rounds         → Portfolio Manager
      latest_speaker == 'Aggressive'       → Conservative
      latest_speaker == 'Conservative'     → Neutral
      else                                 → Aggressive

Lifted from TradingAgents tradingagents/graph/conditional_logic.py with
no behavioural changes — the prompts depend on this exact rotation, so we
don't tinker.
"""
from __future__ import annotations

from trader.slow_agent.state import AgentState


class ConditionalLogic:
    def __init__(self, *, max_debate_rounds: int = 1, max_risk_rounds: int = 1):
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_rounds = max_risk_rounds

    # ── Investment debate ─────────────────────────────────────────────────
    def should_continue_debate(self, state: AgentState) -> str:
        debate = state.get("investment_debate_state") or {}
        if (debate.get("count") or 0) >= 2 * self.max_debate_rounds:
            return "Research Manager"
        if (debate.get("current_response") or "").startswith("Bull"):
            return "Bear Researcher"
        return "Bull Researcher"

    # ── Risk debate ───────────────────────────────────────────────────────
    def should_continue_risk(self, state: AgentState) -> str:
        risk = state.get("risk_debate_state") or {}
        if (risk.get("count") or 0) >= 3 * self.max_risk_rounds:
            return "Portfolio Manager"
        speaker = risk.get("latest_speaker") or ""
        if speaker.startswith("Aggressive"):
            return "Conservative Analyst"
        if speaker.startswith("Conservative"):
            return "Neutral Analyst"
        return "Aggressive Analyst"
