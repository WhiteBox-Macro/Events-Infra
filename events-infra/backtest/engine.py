"""Strategy engine protocol and context."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import numpy as np

from tick import BarTick, EventTick, Order, Position, LookaheadViolation


class StrategyEngine(Protocol):
    name: str

    def on_bar(self, tick: BarTick, ctx: "StrategyContext") -> list[Order]: ...
    def on_event(self, tick: EventTick, ctx: "StrategyContext") -> list[Order]: ...
    def refit(self, train_start: datetime, train_end: datetime, ctx: "StrategyContext") -> None: ...


class StrategyContext:
    """Read-only view of the backtest state provided to strategies.

    Enforces lookahead guards: strategies can only access data
    up to (not including) the current tick time.
    """

    def __init__(self, bar_dfs: dict, portfolio_notional: float):
        self._bar_dfs = bar_dfs
        self._cursor: dict[str, int] = {t: -1 for t in bar_dfs}
        self._last_price: dict[str, float] = {}
        self._current_time: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._positions: dict[tuple[str, str], Position] = {}
        self.portfolio_notional = portfolio_notional
        self._portfolio_value = portfolio_notional

    def advance_cursor(self, ticker: str, bar_index: int, close_price: float) -> None:
        """Called by the runner after MTM, before dispatching to strategies."""
        self._cursor[ticker] = bar_index
        self._last_price[ticker] = close_price

    def set_time(self, ts: datetime) -> None:
        self._current_time = ts

    def set_portfolio_value(self, value: float) -> None:
        self._portfolio_value = value

    def set_positions(self, positions: dict) -> None:
        self._positions = positions

    @property
    def clock(self) -> datetime:
        return self._current_time

    @property
    def portfolio_value(self) -> float:
        return self._portfolio_value

    def price(self, ticker: str) -> float | None:
        """Last known close price. Guard 2: only returns fully elapsed bars."""
        return self._last_price.get(ticker)

    def bars(self, ticker: str, lookback_n: int) -> list[dict] | None:
        """Last N bars as list of dicts. Guard 1: cannot see past cursor."""
        if ticker not in self._bar_dfs:
            return None
        cursor = self._cursor[ticker]
        if cursor < 0:
            return []
        df = self._bar_dfs[ticker]
        start = max(0, cursor - lookback_n + 1)
        end = cursor + 1
        return df.iloc[start:end].to_dict("records")

    def positions(self, strategy_name: str) -> list[Position]:
        return [p for (s, _), p in self._positions.items()
                if s == strategy_name and p.exit_time is None]
