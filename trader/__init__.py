"""trader/ — event-driven trading agent.

This package consumes news/social/macro events written by scripts/ingest/*
and produces decisions in signals.decisions, with optional paper-trade fills
in signals.paper_positions.

Layout:
  clock.py        — Clock abstraction (LiveClock vs ReplayClock). Used by
                    every other module so backtest and live share code.
  prices.py       — PriceSource abstraction (LivePriceSource vs Historical).
  tickers.py      — Three-stage ticker resolver: pre-tagged > regex > LLM.
  dispatcher.py   — Long-lived LISTEN/NOTIFY loop; fans out to fast + slow.
  fast_signal.py  — Phase 3.
  slow_agent/     — Phase 4 (LangGraph package).
  paper/          — Phase 5 (execute/mtm/settle).
  reflect.py      — Phase 5.
  backtest/       — Phase 6.

The signal layer never imports from scripts/ingest/* — they communicate via
Postgres only (rows + LISTEN/NOTIFY). This keeps live and backtest paths
clean and makes the dispatcher trivially testable with synthetic rows.
"""
