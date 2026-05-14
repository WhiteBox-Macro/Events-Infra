"""trader/paper/execute.py — paper-trading fills.

Phase 3 provides `open_position()`: takes a decision_id + ticker + rating
and writes a `signals.paper_positions` row plus the paired opening
`signals.paper_trades` row. Both inserts happen in one transaction so a
partial failure can't leave dangling state.

Phase 5 will add `close_position()` + partial-close logic for when the slow
agent supersedes a fast decision and wants to size the position down.

Sizing logic (kept simple here; tune later if it doesn't generate alpha):
    notional = PORTFOLIO_NOTIONAL                    (env, default 100_000)
    qty_raw  = (max_position_pct * notional / entry_price) * confidence
    qty      = round(qty_raw, 6) clamped to ≥ 1e-6

Slippage:
    open long  → entry_price = quote * (1 + bps/10_000)
    open short → entry_price = quote * (1 - bps/10_000)

Both `clock` and `price_source` are injected by the dispatcher so backtest
runs (ReplayClock + HistoricalPriceSource) use the same code path.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from psycopg2.extras import Json

from dbkit import pg
from trader.clock import Clock
from trader.prices import PriceMissing, PriceSource

_log = logging.getLogger(__name__)

DEFAULT_PORTFOLIO_NOTIONAL = Decimal("100000")
DEFAULT_BPS_SLIPPAGE = Decimal("5")
QTY_QUANTUM = Decimal("0.000001")

LONG_RATINGS = {"Buy", "Overweight"}
SHORT_RATINGS = {"Sell", "Underweight"}


def _portfolio_notional() -> Decimal:
    raw = os.environ.get("PORTFOLIO_NOTIONAL")
    if not raw:
        return DEFAULT_PORTFOLIO_NOTIONAL
    try:
        return Decimal(raw)
    except Exception:
        _log.warning("PORTFOLIO_NOTIONAL=%r is not a number; using default", raw)
        return DEFAULT_PORTFOLIO_NOTIONAL


def _slippage_bps() -> Decimal:
    raw = os.environ.get("BPS_SLIPPAGE")
    if not raw:
        return DEFAULT_BPS_SLIPPAGE
    try:
        return Decimal(raw)
    except Exception:
        _log.warning("BPS_SLIPPAGE=%r is not a number; using default", raw)
        return DEFAULT_BPS_SLIPPAGE


def _apply_slippage(price: Decimal, side: str) -> tuple[Decimal, Decimal]:
    """Return (filled_price, bps_used)."""
    bps = _slippage_bps()
    factor = bps / Decimal("10000")
    if side == "long":
        return (price * (Decimal("1") + factor), bps)
    return (price * (Decimal("1") - factor), bps)


def open_position(
    *,
    decision_id: str,
    ticker: str,
    rating: str,
    confidence: float,
    watchlist_row: dict,
    clock: Clock,
    price_source: PriceSource,
    mode: str = "live",
) -> Optional[str]:
    """Open a paper position. Returns the position_id, or None if no fill happened.

    Returns None (without raising) for:
      - price source can't quote the ticker (yfinance miss / cache miss),
      - sizing math produces qty ≈ 0 (e.g. ultra-low confidence × tiny notional).
    Raises if the DB write fails — that's a real problem, not a fill skip.
    """
    side = _side_for_rating(rating)
    if side is None:
        return None

    quote = _quote(price_source, ticker, clock=clock)
    if quote is None:
        _log.info("skip fill: no quote for %s", ticker)
        return None

    notional = _portfolio_notional()
    max_pct = Decimal(str(watchlist_row.get("max_position_pct") or "0.05"))
    confidence_d = Decimal(str(confidence)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    raw_qty = (max_pct * notional / quote) * confidence_d
    qty = raw_qty.quantize(QTY_QUANTUM, rounding=ROUND_HALF_UP)
    if qty <= 0:
        _log.info("skip fill: computed qty=0 for %s (notional=%s, pct=%s, conf=%s, price=%s)",
                  ticker, notional, max_pct, confidence_d, quote)
        return None

    entry_price, bps = _apply_slippage(quote, side)
    entry_at = clock.now()

    position_row = {
        "decision_id": decision_id,
        "ticker": ticker.upper(),
        "side": side,
        "qty": qty,
        "entry_price": entry_price,
        "entry_at": entry_at,
        "status": "open",
        "mode": mode,
        "metadata": Json({
            "rating": rating,
            "confidence": float(confidence_d),
            "quote_pre_slippage": str(quote),
            "slippage_bps": str(bps),
            "portfolio_notional": str(notional),
            "max_position_pct": str(max_pct),
        }),
    }

    with pg.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO signals.paper_positions "
                "(decision_id, ticker, side, qty, entry_price, entry_at, status, mode, metadata) "
                "VALUES (%(decision_id)s, %(ticker)s, %(side)s, %(qty)s, %(entry_price)s, "
                "        %(entry_at)s, %(status)s, %(mode)s, %(metadata)s) "
                "RETURNING position_id",
                position_row,
            )
            position_id = str(cur.fetchone()[0])

            # Paired opening trade. Side here is the direction of *this* fill —
            # a short open uses `sell`; a long open uses `buy`.
            fill_side = "buy" if side == "long" else "sell"
            cur.execute(
                "INSERT INTO signals.paper_trades "
                "(position_id, kind, side, qty, price, executed_at, slippage_bps, mode, metadata) "
                "VALUES (%s, 'open', %s, %s, %s, %s, %s, %s, %s)",
                [
                    position_id,
                    fill_side,
                    qty,
                    entry_price,
                    entry_at,
                    bps,
                    mode,
                    Json({"decision_id": decision_id, "rating": rating}),
                ],
            )
    return position_id


def _side_for_rating(rating: str) -> Optional[str]:
    if rating in LONG_RATINGS:
        return "long"
    if rating in SHORT_RATINGS:
        return "short"
    return None  # Hold


def _quote(price_source: PriceSource, ticker: str, *, clock: Clock) -> Optional[Decimal]:
    try:
        price = price_source.get_price(ticker, at=clock.now())
    except PriceMissing:
        return None
    except Exception:
        _log.exception("price lookup failed for %s", ticker)
        return None
    if price is None or price <= 0:
        return None
    return price


# ── Close path (Phase 5) ────────────────────────────────────────────────────
def close_position(
    *,
    position_id: str,
    clock: Clock,
    price_source: PriceSource,
    reason: str = "horizon",
    override_price: Optional[Decimal] = None,
) -> Optional[Decimal]:
    """Close an open position at the current quote (or override_price for
    deterministic backtest exits).

    Returns realized_pnl, or None if the close was skipped (no quote / position
    already closed). Writes a `kind='close'` row to signals.paper_trades and
    flips the position to status='closed' atomically.

    `reason` is recorded in trade metadata so reports can distinguish
    horizon-driven exits, slow-agent supersession exits, and manual closes."""
    rows = pg.execute(
        "SELECT position_id, ticker, side, qty, entry_price, status, mode "
        "FROM signals.paper_positions WHERE position_id = %s FOR UPDATE",
        [position_id],
    )
    if not rows:
        _log.warning("close_position: position %s not found", position_id)
        return None
    pos = rows[0]
    if pos["status"] != "open":
        _log.info("close_position: %s already closed; no-op", position_id)
        return None

    side = pos["side"]
    qty = Decimal(str(pos["qty"]))
    entry_price = Decimal(str(pos["entry_price"]))

    if override_price is not None:
        raw_price = Decimal(str(override_price))
    else:
        raw_price = _quote(price_source, pos["ticker"], clock=clock)
        if raw_price is None:
            _log.info("close_position: no quote for %s; skipping", pos["ticker"])
            return None

    # Closing slippage is the inverse of opening: closing a long is a sell
    # (price worse by `bps` ticks); closing a short is a buy (price worse the
    # other way). Either way the operator's quote pays the spread.
    bps = _slippage_bps()
    factor = bps / Decimal("10000")
    if side == "long":
        exit_price = raw_price * (Decimal("1") - factor)
        realized = qty * (exit_price - entry_price)
        fill_side = "sell"
    elif side == "short":
        exit_price = raw_price * (Decimal("1") + factor)
        realized = qty * (entry_price - exit_price)
        fill_side = "buy"
    else:
        _log.warning("close_position: unexpected side=%s on %s", side, position_id)
        return None

    exit_at = clock.now()
    with pg.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE signals.paper_positions "
                "SET status='closed', exit_price=%s, exit_at=%s, realized_pnl=%s "
                "WHERE position_id=%s AND status='open'",
                [exit_price, exit_at, realized, position_id],
            )
            if cur.rowcount == 0:
                # Another worker closed it between the SELECT and the UPDATE.
                _log.info("close_position: %s lost CAS race; skipping", position_id)
                return None
            cur.execute(
                "INSERT INTO signals.paper_trades "
                "(position_id, kind, side, qty, price, executed_at, slippage_bps, mode, metadata) "
                "VALUES (%s, 'close', %s, %s, %s, %s, %s, %s, %s)",
                [
                    position_id, fill_side, qty, exit_price, exit_at, bps, pos["mode"],
                    Json({"reason": reason, "pre_slippage_price": str(raw_price)}),
                ],
            )
    return realized
