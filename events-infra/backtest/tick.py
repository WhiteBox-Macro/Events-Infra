"""Tick types, orders, fills, and positions for the sequencer."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Union


@dataclass(frozen=True, slots=True)
class BarTick:
    ticker: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    bar_index: int


@dataclass(frozen=True, slots=True)
class EventTick:
    event_id: str
    publish_time: datetime
    event_type: str
    is_regular: bool
    headline: str | None
    inferred_tone: str
    inferred_magnitude: str
    tickers: list
    primary_ticker: str | None
    surprise: float | None
    indicator_name: str | None
    metadata: dict


Tick = Union[BarTick, EventTick]


@dataclass(frozen=True, slots=True)
class Order:
    strategy: str
    ticker: str
    side: str           # "buy" | "sell"
    qty_pct: float      # percentage of portfolio
    reason: str
    submitted_at: datetime = field(default=None)
    metadata: dict = field(default_factory=dict)


@dataclass
class Fill:
    strategy: str
    ticker: str
    side: str
    qty: float
    price: float
    slippage_bps: float
    fill_time: datetime
    order_submitted_at: datetime
    reason: str


@dataclass
class Position:
    strategy: str
    ticker: str
    side: str
    qty: float
    entry_price: float
    entry_time: datetime
    reason: str
    unrealized_pnl: float = 0.0
    exit_price: float | None = None
    exit_time: datetime | None = None
    realized_pnl: float | None = None


class LookaheadViolation(Exception):
    """Raised when a lookahead guard detects future data access."""
