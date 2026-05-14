"""Trader node — turns the research-manager's rating into a transaction
proposal, ready for the risk team to evaluate.

We use structured output again so the downstream risk-team prompt sees a
clean side / size / horizon triple rather than free-form prose.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from trader.slow_agent.llm import get_deep_llm
from trader.slow_agent.state import AgentState


class TraderProposal(BaseModel):
    side: str = Field(..., description="long | short | flat")
    size_fraction: float = Field(..., ge=0.0, le=1.0,
                                  description="Fraction of max_position_pct to use; 1.0 = full size")
    horizon_hours: int = Field(..., ge=1, le=720)
    rationale: str = Field(..., description="Markdown body explaining the trade")


PROMPT = """You are the Trader. Convert the research manager's plan for {ticker} into a transaction proposal.

Inputs:
**Research plan**:
{plan}

**Past context**:
{past_context}

Rules:
- side='flat' for any Hold rating.
- size_fraction in [0,1] scales the watchlist's max_position_pct cap; lean smaller when the manager's confidence is below 0.6.
- horizon_hours is your intended holding period in hours.
- Anchor the rationale in specific points from the plan; do not invent new analysis.

Return a JSON object that conforms to the TraderProposal schema (side, size_fraction, horizon_hours, rationale)."""


def create_trader():
    llm = get_deep_llm(temperature=0.2)
    structured = llm.with_structured_output(TraderProposal)

    def node(state: AgentState) -> dict:
        prompt = PROMPT.format(
            ticker=state.get("ticker", "?"),
            plan=state.get("investment_plan", ""),
            past_context=state.get("past_context", "(no prior context)"),
        )
        try:
            proposal: TraderProposal = structured.invoke(prompt)
        except Exception:
            proposal = TraderProposal(
                side="flat", size_fraction=0.0, horizon_hours=24,
                rationale="Trader proposal unavailable; defaulting to flat.",
            )

        rendered = (
            f"**Side**: {proposal.side}\n"
            f"**Size fraction**: {proposal.size_fraction:.2f}\n"
            f"**Horizon hours**: {proposal.horizon_hours}\n\n"
            f"{proposal.rationale}"
        )
        return {"trader_investment_plan": rendered}

    return node
