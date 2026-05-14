"""LangGraph state shapes.

Lifted in spirit (not verbatim) from TauricResearch/TradingAgents
tradingagents/agents/utils/agent_states.py — same TypedDict pattern, but
adapted to our event-driven model:

  * `ticker` is the single instrument we're scoring (one slow run per
    relevant ticker — the dispatcher fans out).
  * `source_event` mirrors signals.decisions.source_event so we can
    reconstruct what triggered the run.
  * `fast_decision` is the row written moments earlier by trader/fast_signal;
    every analyst gets access to it so the bull/bear debate can argue *for*
    or *against* the fast call rather than starting blind.
  * `past_context` is the same-ticker + cross-ticker reflection string built
    from signals.decisions — gives the manager memory across runs.

InvestDebateState and RiskDebateState mirror TradingAgents' structures so
the conditional-routing helpers (next_speaker, round counters) carry over
unchanged.
"""
from __future__ import annotations

from typing import Annotated, Any, Optional

from typing_extensions import TypedDict


class InvestDebateState(TypedDict, total=False):
    """Bull/bear debate accumulator. `count` is incremented by each agent
    so should_continue_debate() can stop after MAX_DEBATE_ROUNDS rounds."""
    history: Annotated[str, "running transcript across both researchers"]
    bull_history: Annotated[str, "running transcript of bull arguments only"]
    bear_history: Annotated[str, "running transcript of bear arguments only"]
    current_response: Annotated[str, "most-recent argument (lets next agent quote it)"]
    judge_decision: Annotated[str, "research manager's final write-up"]
    count: Annotated[int, "messages exchanged so far"]


class RiskDebateState(TypedDict, total=False):
    """Aggressive ⇄ conservative ⇄ neutral risk debate."""
    history: Annotated[str, "running transcript across all three"]
    aggressive_history: Annotated[str, ""]
    conservative_history: Annotated[str, ""]
    neutral_history: Annotated[str, ""]
    latest_speaker: Annotated[str, "Aggressive | Conservative | Neutral"]
    current_aggressive_response: Annotated[str, ""]
    current_conservative_response: Annotated[str, ""]
    current_neutral_response: Annotated[str, ""]
    judge_decision: Annotated[str, "portfolio manager's final write-up"]
    count: Annotated[int, "messages exchanged so far"]


class AgentState(TypedDict, total=False):
    """Top-level graph state. Analyst nodes write their reports into the
    matching `*_report` fields; researchers/manager/trader/risk-team read
    them. Fields are total=False so the graph can incrementally populate.

    Note on naming: we keep the TradingAgents field names (`market_report`,
    `sentiment_report`, ...) so the bull/bear prompts can be lifted verbatim
    without text-find-replace. The `technical_analyst` populates
    `market_report`, the `social_analyst` populates `sentiment_report`.
    """
    # ── Routing / inputs ──────────────────────────────────────────────────
    ticker: Annotated[str, "the symbol this run is scoring"]
    decision_clock: Annotated[Any, "ISO string of the trigger event's time (str for serialisability)"]
    source_event: Annotated[dict, "{kind, id, ...} — what fired this run"]
    fast_decision: Annotated[dict, "row from signals.decisions(tier='fast') that started this run"]
    mode: Annotated[str, "'live' | 'backtest'"]
    experiment_key: Annotated[Optional[str], "backtest experiment tag, None in live"]
    past_context: Annotated[str, "same-ticker + cross-ticker reflections from signals.decisions"]

    # ── Analyst reports ───────────────────────────────────────────────────
    news_report: Annotated[str, "news analyst's write-up"]
    sentiment_report: Annotated[str, "social analyst's write-up"]
    market_report: Annotated[str, "technical analyst's write-up"]
    macro_report: Annotated[str, "macro analyst's write-up"]
    fundamentals_report: Annotated[str, "(optional) fundamentals analyst — empty until AOTC-DB integration matures"]

    # ── Debates ──────────────────────────────────────────────────────────
    investment_debate_state: Annotated[InvestDebateState, "bull/bear state"]
    investment_plan: Annotated[str, "research manager's structured plan"]

    trader_investment_plan: Annotated[str, "trader's transaction proposal"]

    risk_debate_state: Annotated[RiskDebateState, "aggressive/conservative/neutral state"]
    final_trade_decision: Annotated[str, "portfolio manager's final write-up; rating parsed out of it"]


def fresh_invest_debate_state() -> InvestDebateState:
    return InvestDebateState(
        history="", bull_history="", bear_history="",
        current_response="", judge_decision="", count=0,
    )


def fresh_risk_debate_state() -> RiskDebateState:
    return RiskDebateState(
        history="", aggressive_history="", conservative_history="", neutral_history="",
        latest_speaker="", current_aggressive_response="", current_conservative_response="",
        current_neutral_response="", judge_decision="", count=0,
    )
