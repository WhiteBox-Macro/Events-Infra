"""Portfolio manager — final approver, emits the row that gets written.

Reads the risk debate, the trader's proposal, and all analyst reports.
Outputs a structured FinalDecision via pydantic so the runner can persist
into signals.decisions without regex-parsing markdown.

Confidence here is the *combined* call (debate consensus × baseline strength),
distinct from the research-manager's intermediate confidence — that one
gauges the bull/bear debate; this one gauges the whole pipeline.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from trader.slow_agent.llm import get_deep_llm
from trader.slow_agent.manager import RATING_VALUES, _normalise_rating
from trader.slow_agent.state import AgentState


class FinalDecision(BaseModel):
    rating: str = Field(..., description="One of Buy/Overweight/Hold/Underweight/Sell")
    confidence: float = Field(..., ge=0.0, le=1.0)
    horizon_hours: int = Field(..., ge=1, le=720)
    rationale: str = Field(..., description="Markdown body explaining the final call")


PROMPT = """You are the Portfolio Manager and final approver for {ticker}. Read the analyst reports, the bull/bear debate's outcome, the trader's proposal, and the three-way risk debate, then commit to a single decision the desk will execute.

Inputs:

**Analyst reports**
News:      {news_report}
Social:    {sentiment_report}
Technical: {market_report}
Macro:     {macro_report}

**Research manager's plan**:
{plan}

**Trader's proposal**:
{trader_plan}

**Risk debate transcript**:
{risk_history}

**Fast-path baseline (deterministic prior)**:
{fast_summary}

**Past context** (same-ticker history + cross-ticker lessons):
{past_context}

Rules:
- Pick exactly one rating from {ratings}.
- confidence in [0,1]: a 0.9 means the debate and the data converged hard; a 0.5 means it could plausibly go the other way.
- horizon_hours is the holding period you'd assign to this position.
- rationale: tight markdown body, 200-400 words, citing specific points from the inputs.
- If the fast-path call disagrees with you, say *why* you're overriding it.

Return a JSON object that conforms to the FinalDecision schema."""


def _fast_summary(fast: dict | None) -> str:
    if not fast:
        return "(no fast-path baseline)"
    return (
        f"rating={fast.get('rating')}, confidence={fast.get('confidence')}, "
        f"factors={(fast.get('debate_transcript') or {}).get('factors')}"
    )


def create_portfolio_manager():
    llm = get_deep_llm(temperature=0.15)
    structured = llm.with_structured_output(FinalDecision)

    def node(state: AgentState) -> dict:
        risk = state.get("risk_debate_state") or {}
        prompt = PROMPT.format(
            ticker=state.get("ticker", "?"),
            news_report=state.get("news_report", ""),
            sentiment_report=state.get("sentiment_report", ""),
            market_report=state.get("market_report", ""),
            macro_report=state.get("macro_report", ""),
            plan=state.get("investment_plan", ""),
            trader_plan=state.get("trader_investment_plan", ""),
            risk_history=risk.get("history", ""),
            fast_summary=_fast_summary(state.get("fast_decision")),
            past_context=state.get("past_context", "(no prior context)"),
            ratings=", ".join(RATING_VALUES),
        )
        try:
            decision: FinalDecision = structured.invoke(prompt)
        except Exception:
            decision = FinalDecision(
                rating="Hold", confidence=0.5, horizon_hours=24,
                rationale="Portfolio manager structured output unavailable; defaulting to Hold.",
            )
        rating = _normalise_rating(decision.rating)
        rendered = (
            f"**Rating**: {rating}\n"
            f"**Confidence**: {decision.confidence:.2f}\n"
            f"**Horizon**: {decision.horizon_hours}h\n\n"
            f"{decision.rationale}"
        )
        # Patch judge_decision so it's visible if anyone inspects the state.
        new_risk = dict(risk)
        new_risk["judge_decision"] = rendered
        return {
            "risk_debate_state": new_risk,
            "final_trade_decision": rendered,
            # Stash the structured fields under a private key for the runner.
            "__final": {
                "rating": rating,
                "confidence": float(decision.confidence),
                "horizon_hours": int(decision.horizon_hours),
                "rationale_md": rendered,
            },
        }

    return node
