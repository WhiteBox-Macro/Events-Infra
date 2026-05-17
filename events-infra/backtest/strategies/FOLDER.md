# strategies/

Strategy implementations for the event backtest engine.

## Files

| File | Role |
|------|------|
| `sonnet_event_strategy.py` | LLM-as-tagger strategy: classifies events into categories, trades deterministically from an impact table of historical returns per (category, ticker) pair |

## Strategy Protocol

All strategies must implement the `StrategyEngine` protocol from `engine.py`:

- `name: str` -- unique identifier
- `on_bar(tick, ctx) -> list[Order]` -- called on each bar tick
- `on_event(tick, ctx) -> list[Order]` -- called on each event tick
- `refit(train_start, train_end, ctx) -> None` -- walk-forward refit callback

## Adding a New Strategy

1. Create a new `.py` file in this directory
2. Implement the StrategyEngine protocol
3. Register it in `runner.py` or `dashboard/server.py`
