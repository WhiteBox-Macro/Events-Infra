"""Bull researcher.

Prompt lifted from TauricResearch/TradingAgents
tradingagents/agents/researchers/bull_researcher.py — it's well-tuned, so we
keep the wording. The two changes:
  * field names match our AgentState (which intentionally tracks TradingAgents'
    names: market_report, sentiment_report, news_report, fundamentals_report),
  * the prompt now includes our `fast_decision` and `past_context` blocks so
    the bull argument can engage with what the deterministic path concluded
    and what past runs found out.
"""
from __future__ import annotations

from typing import Any

from trader.slow_agent.llm import get_quick_llm
from trader.slow_agent.state import AgentState, InvestDebateState

PROMPT = """You are a Bull Analyst advocating for investing in {ticker}. Build a strong, evidence-based case emphasizing growth potential, competitive advantages, and positive market indicators. Leverage the provided research and data to address concerns and counter bearish arguments effectively.

Key points to focus on:
- Growth Potential: Highlight the company's market opportunities, revenue projections, and scalability.
- Competitive Advantages: Emphasize factors like unique products, strong branding, or dominant market positioning.
- Positive Indicators: Use financial health, industry trends, and recent positive news as evidence.
- Bear Counterpoints: Critically analyze the bear argument with specific data and sound reasoning, addressing concerns thoroughly and showing why the bull perspective holds stronger merit.
- Engagement: Present your argument in a conversational style, engaging directly with the bear analyst's points and debating effectively rather than just listing data.

Resources available:
Technical market report: {market_report}
Social media sentiment report: {sentiment_report}
Latest news report: {news_report}
Macro context report: {macro_report}

Fast-path baseline (deterministic call already made by the fast-signal agent — argue for or against it on the merits):
{fast_summary}

Past context (same-ticker history + recent cross-ticker lessons):
{past_context}

Conversation history of the debate: {history}
Last bear argument: {current_response}

Use this information to deliver a compelling bull argument, refute the bear's concerns, and engage in a dynamic debate that demonstrates the strengths of the bull position."""


def _fast_summary(fast: dict | None) -> str:
    if not fast:
        return "(no fast-path baseline available)"
    return (
        f"rating={fast.get('rating')}, confidence={fast.get('confidence')}, "
        f"factors={(fast.get('debate_transcript') or {}).get('factors')}"
    )


def create_bull_researcher():
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
        argument = f"Bull Analyst: {getattr(resp, 'content', str(resp))}"
        new_debate: InvestDebateState = InvestDebateState(
            history=(debate.get("history", "") + "\n" + argument).strip(),
            bull_history=(debate.get("bull_history", "") + "\n" + argument).strip(),
            bear_history=debate.get("bear_history", ""),
            current_response=argument,
            judge_decision=debate.get("judge_decision", ""),
            count=(debate.get("count") or 0) + 1,
        )
        return {"investment_debate_state": new_debate}

    return node
