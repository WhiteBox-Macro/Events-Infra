"""Order manager — queues orders, fills, tracks positions.

Supports two fill modes:
- BAR_CLOSE: fill at the current bar's close (for event-driven entry)
- NEXT_BAR_OPEN: fill at the next bar's open (standard no-lookahead)

For this strategy: entry at event bar close, exit at t+N close.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from tick import BarTick, Order, Fill, Position, LookaheadViolation

log = logging.getLogger("sequencer.order_mgr")


class ScheduledExit:
    """An exit order scheduled for a future bar index."""
    def __init__(self, strategy: str, ticker: str, bars_remaining: int,
                 position_key: tuple[str, str]):
        self.strategy = strategy
        self.ticker = ticker
        self.bars_remaining = bars_remaining
        self.position_key = position_key


class OrderManager:
    def __init__(self, slippage_bps: float = 5.0, portfolio_notional: float = 100_000.0):
        self.slippage_bps = slippage_bps
        self.portfolio_value = portfolio_notional

        self.positions: dict[tuple[str, str], Position] = {}
        self.closed_positions: list[Position] = []
        self.fills: list[Fill] = []
        self.scheduled_exits: list[ScheduledExit] = []
        self._pending_next_bar: list[tuple[Order, datetime]] = []

    def fill_at_close(self, order: Order, bar: BarTick, current_time: datetime) -> Fill | None:
        """Immediately fill at the bar's close price. For event-driven entries."""
        price = bar.close
        slipped = self._apply_slippage(price, order.side)
        qty = (order.qty_pct * self.portfolio_value) / slipped if slipped > 0 else 0

        if qty <= 0:
            return None

        fill = Fill(
            strategy=order.strategy,
            ticker=order.ticker,
            side=order.side,
            qty=qty,
            price=slipped,
            slippage_bps=self.slippage_bps,
            fill_time=current_time,
            order_submitted_at=current_time,
            reason=order.reason,
        )
        self.fills.append(fill)

        key = (order.strategy, order.ticker)
        self.positions[key] = Position(
            strategy=order.strategy,
            ticker=order.ticker,
            side=order.side,
            qty=qty,
            entry_price=slipped,
            entry_time=current_time,
            reason=order.reason,
        )

        log.debug("FILL entry %s %s %.4f qty=%.2f at %s",
                   order.side, order.ticker, slipped, qty, current_time)
        return fill

    def schedule_exit(self, strategy: str, ticker: str, bars_ahead: int) -> None:
        """Schedule an exit N bars from now."""
        key = (strategy, ticker)
        if key not in self.positions:
            return
        self.scheduled_exits.append(ScheduledExit(
            strategy=strategy, ticker=ticker,
            bars_remaining=bars_ahead, position_key=key,
        ))

    def process_bar(self, bar: BarTick, current_time: datetime) -> list[Fill]:
        """Process scheduled exits on each bar tick. Returns exit fills."""
        exit_fills = []
        remaining = []

        for se in self.scheduled_exits:
            if se.ticker != bar.ticker:
                remaining.append(se)
                continue

            se.bars_remaining -= 1

            if se.bars_remaining <= 0:
                pos = self.positions.get(se.position_key)
                if pos and pos.exit_time is None:
                    exit_price = bar.close
                    slipped = self._apply_slippage(exit_price, "sell" if pos.side == "buy" else "buy")

                    if pos.side == "buy":
                        pos.realized_pnl = (slipped - pos.entry_price) / pos.entry_price
                    else:
                        pos.realized_pnl = (pos.entry_price - slipped) / pos.entry_price

                    pos.exit_price = slipped
                    pos.exit_time = current_time

                    fill = Fill(
                        strategy=pos.strategy, ticker=pos.ticker,
                        side="sell" if pos.side == "buy" else "buy",
                        qty=pos.qty, price=slipped,
                        slippage_bps=self.slippage_bps,
                        fill_time=current_time,
                        order_submitted_at=current_time,
                        reason=f"scheduled_exit_{pos.reason}",
                    )
                    self.fills.append(fill)
                    exit_fills.append(fill)

                    self.closed_positions.append(pos)
                    del self.positions[se.position_key]

                    log.debug("FILL exit %s %.4f pnl=%.4f%% at %s",
                               pos.ticker, slipped, pos.realized_pnl * 100, current_time)
            else:
                remaining.append(se)

        self.scheduled_exits = remaining
        return exit_fills

    def mark_positions(self, bar: BarTick) -> None:
        """Update unrealized P&L for open positions."""
        for key, pos in self.positions.items():
            if pos.ticker == bar.ticker and pos.exit_time is None:
                if pos.side == "buy":
                    pos.unrealized_pnl = (bar.close - pos.entry_price) / pos.entry_price
                else:
                    pos.unrealized_pnl = (pos.entry_price - bar.close) / pos.entry_price

    def close_all(self, last_prices: dict[str, float], current_time: datetime) -> None:
        """Force-close all open positions at last known prices."""
        for key in list(self.positions.keys()):
            pos = self.positions[key]
            price = last_prices.get(pos.ticker, pos.entry_price)
            if pos.side == "buy":
                pos.realized_pnl = (price - pos.entry_price) / pos.entry_price
            else:
                pos.realized_pnl = (pos.entry_price - price) / pos.entry_price
            pos.exit_price = price
            pos.exit_time = current_time
            self.closed_positions.append(pos)
            del self.positions[key]

    def has_position(self, strategy: str, ticker: str) -> bool:
        return (strategy, ticker) in self.positions

    def _apply_slippage(self, price: float, side: str) -> float:
        bps = self.slippage_bps / 10_000
        if side == "buy":
            return price * (1 + bps)
        else:
            return price * (1 - bps)
