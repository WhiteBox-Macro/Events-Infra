"""trader/slow_agent — LangGraph multi-agent debate.

Topology (mirrors TauricResearch/TradingAgents tradingagents/graph/setup.py):

    News Analyst ─┐
    Social ────────┤   (each writes its report into state)
    Macro ─────────┤
    Technical ─────┘
                   │
                   ▼
            Bull Researcher ⇄ Bear Researcher   (debate, capped rounds)
                   │
                   ▼
            Research Manager   (structured 5-tier rating)
                   │
                   ▼
                Trader         (transaction proposal)
                   │
                   ▼
      Aggressive ⇄ Conservative ⇄ Neutral  (risk debate, capped rounds)
                   │
                   ▼
            Portfolio Manager  (final approval, writes signals.decisions)

State is Postgres-checkpointed (langgraph-checkpoint-postgres) so a crashed
run can resume mid-debate, and tokens spent on analyst reports aren't
re-spent if the graph re-enters the same thread.

The slow run is triggered by trader/dispatcher.py after fast_signal has
written its row; the slow decision row uses `supersedes` to point at the
fast one so backtest reports can attribute alpha to "what the slow agent
added or corrected" vs. the fast baseline.
"""
