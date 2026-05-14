"""Research manager — judges the bull/bear debate, emits a 5-tier rating.

Borrowed structure: TradingAgents tradingagents/agents/managers/research_manager.py.
We add structured output via pydantic so downstream code can pull a clean
rating without regex-parsing markdown.

Output schema:
    rating ∈ {Buy, Overweight, Hold, Underweight, Sell}
    confidence ∈ [0, 1]
    rationale  — markdown body for signals.decisions.rationale_md
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from trader.slow_agent.llm import get_deep_llm
from trader.slow_agent.state import AgentState

RATING_VALUES = ("Buy", "Overweight", "Hold", "Underweight", "Sell")


class ResearchPlan(BaseModel):
    rating: str = Field(..., description="One of Buy/Overweight/Hold/Underweight/Sell")
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., description="Markdown body explaining the call")


PROMPT = """As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader on {ticker}.

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

Commit to a clear stance whenever the debate's strongest arguments warrant one; reserve Hold for situations where the evidence on both sides is genuinely balanced.

You will also see the fast-path baseline decision (from a deterministic scorer). Treat it as a prior, not as ground truth — confirm, adjust, or invert it based on the debate.

---

**Fast-path baseline**:
{fast_summary}

**Past context**:
{past_context}

**Debate History**:
{history}

Return a JSON object that conforms to the ResearchPlan schema (rating, confidence in [0,1], rationale).
"""


def _fast_summary(fast: dict | None) -> str:
    if not fast:
        return "(no fast-path baseline)"
    factors = (fast.get("debate_transcript") or {}).get("factors") or []
    return (
        f"rating={fast.get('rating')}, confidence={fast.get('confidence')}, "
        f"factors={factors}"
    )


def _normalise_rating(value: str) -> str:
    s = (value or "").strip().lower()
    for r in RATING_VALUES:
        if s == r.lower():
            return r
    # Map common LLM variants.
    if s in ("strong buy", "long"):
        return "Buy"
    if s in ("strong sell", "short"):
        return "Sell"
    return "Hold"


def create_research_manager():
    llm = get_deep_llm(temperature=0.2)
    structured = llm.with_structured_output(ResearchPlan)

    def node(state: AgentState) -> dict:
        debate = state.get("investment_debate_state") or {}
        prompt = PROMPT.format(
            ticker=state.get("ticker", "?"),
            fast_summary=_fast_summary(state.get("fast_decision")),
            past_context=state.get("past_context", "(no prior context)"),
            history=debate.get("history", ""),
        )
        try:
            plan: ResearchPlan = structured.invoke(prompt)
        except Exception:
            # If structured output fails (provider hiccup, schema mismatch),
            # fall back to a Hold so the run still finishes with a row.
            plan = ResearchPlan(
                rating="Hold",
                confidence=0.5,
                rationale="Structured output unavailable; defaulting to Hold.",
            )

        rating = _normalise_rating(plan.rating)
        new_debate = dict(debate)
        new_debate["judge_decision"] = plan.rationale
        return {
            "investment_debate_state": new_debate,
            "investment_plan": (
                f"**Rating**: {rating}\n"
                f"**Confidence**: {plan.confidence:.2f}\n\n"
                f"{plan.rationale}"
            ),
        }

    return node
