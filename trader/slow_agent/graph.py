"""LangGraph assembly.

Topology (matches TradingAgents tradingagents/graph/setup.py):

  START
    → News Analyst → Social Analyst → Macro Analyst → Technical Analyst
    → Bull Researcher ⇄ Bear Researcher (round-capped)
    → Research Manager
    → Trader
    → Aggressive ⇄ Conservative ⇄ Neutral (risk debate, round-capped)
    → Portfolio Manager
    → END

Analysts are chained linearly (not in parallel) so each analyst can in
principle read the prior ones' reports if needed; the gains from parallel
execution are small at this scale and the serial path is easier to debug.

Checkpointing: the runner injects a Postgres-backed checkpointer at compile
time using thread_id = sha256(ticker + decision_clock + experiment_key)[:16].
This means a crash mid-debate doesn't lose the analyst reports already paid for.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from trader.slow_agent.analysts.macro_analyst import create_macro_analyst
from trader.slow_agent.analysts.news_analyst import create_news_analyst
from trader.slow_agent.analysts.social_analyst import create_social_analyst
from trader.slow_agent.analysts.technical_analyst import create_technical_analyst
from trader.slow_agent.conditional import ConditionalLogic
from trader.slow_agent.manager import create_research_manager
from trader.slow_agent.portfolio_manager import create_portfolio_manager
from trader.slow_agent.researchers.bear_researcher import create_bear_researcher
from trader.slow_agent.researchers.bull_researcher import create_bull_researcher
from trader.slow_agent.risk.aggressive_debator import create_aggressive_debator
from trader.slow_agent.risk.conservative_debator import create_conservative_debator
from trader.slow_agent.risk.neutral_debator import create_neutral_debator
from trader.slow_agent.state import AgentState
from trader.slow_agent.trader import create_trader


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def build_graph(*, checkpointer: Optional[Any] = None):
    """Construct and compile the StateGraph. `checkpointer` is an
    optional langgraph-checkpoint-postgres saver — pass None during tests
    or when running without resumability."""
    # Lazy import so the slow_agent package can be imported without
    # langgraph installed (e.g. in the test path that only touches state.py).
    from langgraph.graph import END, START, StateGraph

    logic = ConditionalLogic(
        max_debate_rounds=_env_int("MAX_DEBATE_ROUNDS", 1),
        max_risk_rounds=_env_int("MAX_RISK_DISCUSS_ROUNDS", 1),
    )

    workflow = StateGraph(AgentState)

    # ── Nodes ────────────────────────────────────────────────────────────
    workflow.add_node("News Analyst", create_news_analyst())
    workflow.add_node("Social Analyst", create_social_analyst())
    workflow.add_node("Macro Analyst", create_macro_analyst())
    workflow.add_node("Technical Analyst", create_technical_analyst())

    workflow.add_node("Bull Researcher", create_bull_researcher())
    workflow.add_node("Bear Researcher", create_bear_researcher())
    workflow.add_node("Research Manager", create_research_manager())

    workflow.add_node("Trader", create_trader())

    workflow.add_node("Aggressive Analyst", create_aggressive_debator())
    workflow.add_node("Conservative Analyst", create_conservative_debator())
    workflow.add_node("Neutral Analyst", create_neutral_debator())
    workflow.add_node("Portfolio Manager", create_portfolio_manager())

    # ── Linear analyst chain ─────────────────────────────────────────────
    workflow.add_edge(START, "News Analyst")
    workflow.add_edge("News Analyst", "Social Analyst")
    workflow.add_edge("Social Analyst", "Macro Analyst")
    workflow.add_edge("Macro Analyst", "Technical Analyst")
    workflow.add_edge("Technical Analyst", "Bull Researcher")

    # ── Bull/bear debate (round-capped) ──────────────────────────────────
    workflow.add_conditional_edges(
        "Bull Researcher",
        logic.should_continue_debate,
        {"Bear Researcher": "Bear Researcher", "Research Manager": "Research Manager"},
    )
    workflow.add_conditional_edges(
        "Bear Researcher",
        logic.should_continue_debate,
        {"Bull Researcher": "Bull Researcher", "Research Manager": "Research Manager"},
    )

    # ── Trader → risk debate → portfolio manager ─────────────────────────
    workflow.add_edge("Research Manager", "Trader")
    workflow.add_edge("Trader", "Aggressive Analyst")

    workflow.add_conditional_edges(
        "Aggressive Analyst",
        logic.should_continue_risk,
        {"Conservative Analyst": "Conservative Analyst", "Portfolio Manager": "Portfolio Manager"},
    )
    workflow.add_conditional_edges(
        "Conservative Analyst",
        logic.should_continue_risk,
        {"Neutral Analyst": "Neutral Analyst", "Portfolio Manager": "Portfolio Manager"},
    )
    workflow.add_conditional_edges(
        "Neutral Analyst",
        logic.should_continue_risk,
        {"Aggressive Analyst": "Aggressive Analyst", "Portfolio Manager": "Portfolio Manager"},
    )

    workflow.add_edge("Portfolio Manager", END)

    return workflow.compile(checkpointer=checkpointer) if checkpointer else workflow.compile()
