"""Portfolio allocator — continuous weight-based rebalancing.

Manages a portfolio of N tickers with target weights that shift via event tilts
and decay back to equal weight over time. Generates rebalancing orders when
current weights deviate from targets beyond a threshold.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

log = logging.getLogger("sequencer.portfolio_alloc")

MAX_WEIGHT = 0.15
MIN_WEIGHT = -0.05
DECAY_PER_BAR = 0.997
TILT_SCALE = 0.001
TILT_UNIT = 0.01  # 1% per unit of LLM impact_weight
REBAL_THRESHOLD = 0.001


@dataclass
class AllocPosition:
    ticker: str
    qty: float
    avg_price: float
    current_price: float

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return self.qty * (self.current_price - self.avg_price)


@dataclass
class RebalOrder:
    ticker: str
    side: str
    qty: float
    target_weight: float
    current_weight: float
    reason: str


def compute_tilt(
    category: str, ticker: str, tone: str,
    stats, llm_weight: float, tilt_scale: float = TILT_SCALE,
    params=None, surprise: float | None = None,
) -> float:
    """Tilt = direction x llm_impact_weight x tilt_unit.

    When `params` (a GateParams instance) is provided, honors:
      - params.tilt_unit  (overrides module TILT_UNIT)
      - params.side_rule  (tone_reliable | contrarian | surprise_direction)
      - params.min_obs    (raises bar for the stats-based path)
      - params.min_hit_rate (used by the stats-based path)
    When `params` is None, behaves identically to the pre-refactor logic
    (preserves backward compatibility).
    """
    # Resolve effective knobs from params (or fall back to today's constants)
    if params is not None:
        tilt_unit = params.tilt_unit
        side_rule = params.side_rule
        min_obs = params.min_obs
        min_hit_rate = params.min_hit_rate
    else:
        tilt_unit = TILT_UNIT
        side_rule = "tone_reliable"
        min_obs = 3
        min_hit_rate = 0.55

    # surprise_direction: side from surprise sign, no stats needed
    if side_rule == "surprise_direction":
        if surprise is None or surprise == 0:
            return 0.0
        direction = 1.0 if surprise > 0 else -1.0
        return direction * llm_weight * tilt_unit

    # sector_spillover not yet implemented — loud-fail (no trade)
    if side_rule == "sector_spillover":
        source = params.source if params is not None else "default"
        log.warning("compute_tilt: side_rule='sector_spillover' not yet implemented "
                    "(cat=%s ticker=%s params.source=%s); returning 0",
                    category, ticker, source)
        return 0.0

    if tone in ("neutral", "mixed"):
        return 0.0

    if stats is not None and stats.count >= min_obs:
        tone_reliable = stats.mean > 0 and stats.hit_rate >= min_hit_rate
        tone_contrarian = stats.mean < 0 and (1 - stats.hit_rate) >= min_hit_rate

        if side_rule == "contrarian":
            if not tone_contrarian:
                return 0.0
            direction = -1.0 if tone == "bullish" else 1.0
        elif tone_reliable:
            direction = 1.0 if tone == "bullish" else -1.0
        elif tone_contrarian:
            direction = -1.0 if tone == "bullish" else 1.0
        else:
            return 0.0
    else:
        direction = 1.0 if tone == "bullish" else -1.0

    return direction * llm_weight * tilt_unit


class PortfolioAllocator:
    def __init__(
        self,
        tickers: list[str],
        notional: float,
        slippage_bps: float = 5.0,
        decay_per_bar: float = DECAY_PER_BAR,
        rebal_threshold: float = REBAL_THRESHOLD,
        max_weight: float = MAX_WEIGHT,
        min_weight: float = MIN_WEIGHT,
    ):
        self.tickers = list(tickers)
        self.notional = notional
        self.slippage_bps = slippage_bps
        self.decay_per_bar = decay_per_bar
        self.rebal_threshold = rebal_threshold
        self.max_weight = max_weight
        self.min_weight = min_weight

        n = len(tickers)
        base = 1.0 / n if n > 0 else 0
        self.base_weights: dict[str, float] = {t: base for t in tickers}
        self.tilts: dict[str, float] = {t: 0.0 for t in tickers}
        self.target_weights: dict[str, float] = dict(self.base_weights)

        self.positions: dict[str, AllocPosition] = {}
        self.cash: float = notional
        self.initialized: bool = False
        self.rebal_fills: list[dict] = []

    @property
    def nav(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    def get_weights(self) -> dict[str, float]:
        total = self.nav
        if total <= 0:
            return {t: 0.0 for t in self.tickers}
        return {t: (self.positions[t].market_value / total if t in self.positions else 0.0)
                for t in self.tickers}

    def get_tilts(self) -> dict[str, float]:
        return dict(self.tilts)

    def get_target_weights(self) -> dict[str, float]:
        return dict(self.target_weights)

    def initialize_positions(self, prices: dict[str, float], ts: datetime) -> list[RebalOrder]:
        if self.initialized:
            return []

        orders = []
        total = self.notional
        for ticker in self.tickers:
            price = prices.get(ticker)
            if price is None or price <= 0:
                continue
            target_value = total * self.base_weights[ticker]
            slipped = price * (1 + self.slippage_bps / 10_000)
            qty = target_value / slipped

            self.positions[ticker] = AllocPosition(
                ticker=ticker, qty=qty, avg_price=slipped, current_price=price
            )
            self.cash -= qty * slipped
            orders.append(RebalOrder(
                ticker=ticker, side="buy", qty=qty,
                target_weight=self.base_weights[ticker], current_weight=0.0,
                reason="initial_alloc",
            ))

        self.initialized = True
        log.info("portfolio initialized: %d tickers, $%.0f NAV", len(self.positions), self.nav)
        return orders

    def apply_event_tilts(self, new_tilts: dict[str, float]) -> None:
        for ticker, delta in new_tilts.items():
            if ticker in self.tilts:
                self.tilts[ticker] += delta
        self._recompute_targets()

    def decay_tilts(self, dt_bars: int = 1) -> None:
        factor = self.decay_per_bar ** dt_bars
        for ticker in self.tilts:
            self.tilts[ticker] *= factor
        self._recompute_targets()

    def _recompute_targets(self) -> None:
        for ticker in self.tickers:
            raw = self.base_weights[ticker] + self.tilts[ticker]
            self.target_weights[ticker] = max(self.min_weight, min(self.max_weight, raw))
        total = sum(self.target_weights.values())
        if total > 0 and abs(total - 1.0) > 0.001:
            for ticker in self.tickers:
                self.target_weights[ticker] /= total

    def mark_to_market(self, prices: dict[str, float]) -> None:
        for ticker, pos in self.positions.items():
            if ticker in prices:
                pos.current_price = prices[ticker]

    def get_rebal_orders(self, prices: dict[str, float]) -> list[RebalOrder]:
        current_weights = self.get_weights()
        total = self.nav
        if total <= 0:
            return []

        orders = []
        for ticker in self.tickers:
            cw = current_weights.get(ticker, 0.0)
            tw = self.target_weights.get(ticker, self.base_weights[ticker])
            delta = tw - cw

            if abs(delta) < self.rebal_threshold:
                continue

            price = prices.get(ticker)
            if price is None or price <= 0:
                continue

            trade_value = delta * total
            side = "buy" if trade_value > 0 else "sell"
            slipped = price * (1 + self.slippage_bps / 10_000) if side == "buy" \
                else price * (1 - self.slippage_bps / 10_000)
            qty = abs(trade_value) / slipped

            orders.append(RebalOrder(
                ticker=ticker, side=side, qty=qty,
                target_weight=tw, current_weight=cw,
                reason=f"rebal_{delta:+.3f}",
            ))

        return orders

    def execute_rebal(self, orders: list[RebalOrder], prices: dict[str, float],
                      ts: datetime) -> list[dict]:
        fills = []
        for order in orders:
            price = prices.get(order.ticker)
            if price is None or price <= 0:
                continue

            slipped = price * (1 + self.slippage_bps / 10_000) if order.side == "buy" \
                else price * (1 - self.slippage_bps / 10_000)

            pos = self.positions.get(order.ticker)

            if order.side == "buy":
                cost = order.qty * slipped
                if pos:
                    old_cost = pos.qty * pos.avg_price
                    pos.qty += order.qty
                    if abs(pos.qty) < 1e-10:
                        del self.positions[order.ticker]
                    elif pos.qty > 0:
                        pos.avg_price = (old_cost + cost) / pos.qty
                    else:
                        pos.avg_price = slipped
                else:
                    self.positions[order.ticker] = AllocPosition(
                        ticker=order.ticker, qty=order.qty,
                        avg_price=slipped, current_price=price,
                    )
                self.cash -= cost
            else:
                proceeds = order.qty * slipped
                if pos:
                    pos.qty -= order.qty
                    if abs(pos.qty) < 1e-10:
                        del self.positions[order.ticker]
                    elif pos.qty < 0:
                        pos.avg_price = slipped
                else:
                    self.positions[order.ticker] = AllocPosition(
                        ticker=order.ticker, qty=-order.qty,
                        avg_price=slipped, current_price=price,
                    )
                self.cash += proceeds

            fill = {
                "ticker": order.ticker, "side": order.side,
                "qty": round(order.qty, 4), "price": round(slipped, 2),
                "target_w": round(order.target_weight, 4),
                "current_w": round(order.current_weight, 4),
                "reason": order.reason, "t": ts,
            }
            fills.append(fill)
            self.rebal_fills.append(fill)

        return fills

    def reset(self):
        n = len(self.tickers)
        base = 1.0 / n if n > 0 else 0
        self.base_weights = {t: base for t in self.tickers}
        self.tilts = {t: 0.0 for t in self.tickers}
        self.target_weights = dict(self.base_weights)
        self.positions.clear()
        self.cash = self.notional
        self.initialized = False
        self.rebal_fills.clear()
