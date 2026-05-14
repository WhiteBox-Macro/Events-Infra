"""trader/backtest — historical replay harness.

The replay re-uses everything from the live path:
  trader.fast_signal        — same scoring rules, mode='backtest'
  trader.slow_agent.runner  — same LangGraph debate (optional, off by default)
  trader.paper.mtm/settle/supersede/execute — same paper-trade sim
  trader.reflect            — same post-mortem prompt

The only differences:

  Clock          LiveClock         → ReplayClock(advance_to=event.published_at)
  PriceSource    LivePriceSource   → HistoricalPriceSource(tolerance=24h)
  Trigger        pg_notify LISTEN  → SELECT FROM news/social/macro ORDER BY ts

This contract — same code, swapped clock and price source — is what makes
"tune a prompt in backtest, ship it unchanged to live" work.
"""
