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
    """Unified event shape consumed by strategies.

    Mirrors the post-migration-008 events.classified columns; _make_event_tick
    tolerates both the old (inferred_tone) and new (tone) column names so
    transitions across migration 009 don't break the timeline reader.
    """
    event_id: str
    publish_time: datetime

    # Taxonomy
    event_category: str           # 14-label broad bucket (ImpactTable key)
    event_type: str               # 30-label fine type
    event_outcome: str | None     # beat|miss|hike|cut|... or None
    is_regular: bool

    # Content
    headline: str | None

    # Opinion (renamed from inferred_*)
    tone: str
    magnitude: str
    confidence: float

    # Affected entities (unified structure)
    primary_ticker: str | None    # objective truth, any ticker (in or out of universe)
    ticker_impacts: list          # [{"ticker":"NVDA","weight":1.0,"role":"primary"}, ...] max 3
    sector: str | None            # single dominant sector or None

    # Scheduled-release block (only meaningful when is_regular)
    indicator_name: str | None
    consensus_value: float | None
    actual_value: float | None
    surprise: float | None
    reporting_period: str | None

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
