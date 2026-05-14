"""PriceSource abstraction — single swap point between live and backtest.

The trader needs prices for two things:
  1. opening / closing paper positions (entry_price, exit_price),
  2. mark-to-market jobs and alpha computation against the benchmark.

Two implementations:
  LivePriceSource       — pulls the latest quote from yfinance, writes the
                          price into signals.price_cache so mtm jobs and
                          backtests can read it without re-fetching.
  HistoricalPriceSource — pure DB read out of signals.price_cache; raises
                          PriceMissing when the key isn't there so the
                          backtest can decide whether to skip or backfill.

yfinance is used for live live because it's free and 15-min-delayed-but-fresh-
enough for paper trading. Swap to a paid feed (Polygon, Alpha Vantage premium)
later by adding a sibling implementation; nothing else in the codebase
references yfinance directly.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from dbkit import pg

_log = logging.getLogger(__name__)


class PriceMissing(LookupError):
    """Raised by HistoricalPriceSource when a (ticker, time) isn't cached."""


class PriceSource(ABC):
    """Common interface. Callers type-hint `PriceSource`."""

    @abstractmethod
    def get_price(self, ticker: str, at: Optional[datetime] = None) -> Optional[Decimal]:
        """Return the price of `ticker` at `at` (UTC).

        Live mode: `at=None` means "latest"; an explicit `at` is interpreted
        as a backstop tolerance — fetch latest if within tolerance, else fall
        back to a historical lookup.

        Backtest mode: `at` is required; the historical implementation only
        knows how to look up cached marks.
        """


class LivePriceSource(PriceSource):
    """yfinance-backed live quotes with passthrough cache writes.

    Every successful lookup writes a row into signals.price_cache, which means
    later mtm marks (and any backtest that overlaps live data) get a free
    cache hit without re-calling yfinance.
    """

    def __init__(self, *, source_tag: str = "yfinance"):
        self.source_tag = source_tag
        # Lazy-import yfinance — keeping it out of the module's top scope means
        # the test/lint path doesn't require it to be installed.
        self._yf = None

    def _yfinance(self):
        if self._yf is None:
            import yfinance as yf  # type: ignore[import-untyped]
            self._yf = yf
        return self._yf

    def get_price(self, ticker: str, at: Optional[datetime] = None) -> Optional[Decimal]:
        yf = self._yfinance()
        try:
            tk = yf.Ticker(ticker)
            # `fast_info.last_price` is the cheapest call on yfinance and
            # avoids pulling the full history. Falls back to history() on
            # tickers where fast_info is unpopulated.
            price_raw = getattr(tk.fast_info, "last_price", None)
            if price_raw is None or price_raw != price_raw:  # NaN check
                hist = tk.history(period="1d", interval="1m")
                if hist is None or hist.empty:
                    return None
                price_raw = float(hist["Close"].dropna().iloc[-1])
        except Exception:
            _log.exception("yfinance lookup failed for %s", ticker)
            return None

        price = Decimal(str(price_raw))
        mark_time = _coerce_utc(at) if at else datetime.now(timezone.utc)
        # Cache write is best-effort: a DB hiccup shouldn't break the caller.
        try:
            pg.upsert(
                "signals.price_cache",
                {
                    "ticker": ticker.upper(),
                    "price_at": mark_time,
                    "price": price,
                    "source": self.source_tag,
                },
                conflict_on=["ticker", "price_at"],
            )
        except Exception:
            _log.exception("price_cache upsert failed for %s @ %s", ticker, mark_time)
        return price


class HistoricalPriceSource(PriceSource):
    """Reads signals.price_cache only. No network.

    `at` is required. Returns the nearest cached price within `tolerance_minutes`
    (default 60). Backtest runs prime the cache via Phase 6's backfill step.
    """

    def __init__(self, *, tolerance_minutes: int = 60, source_tag: Optional[str] = None):
        self.tolerance_minutes = int(tolerance_minutes)
        self.source_tag = source_tag

    def get_price(self, ticker: str, at: Optional[datetime] = None) -> Optional[Decimal]:
        if at is None:
            raise ValueError("HistoricalPriceSource.get_price requires `at`")
        target = _coerce_utc(at)
        sql = (
            "SELECT price, price_at FROM signals.price_cache "
            "WHERE ticker = %s "
            "AND price_at BETWEEN %s AND %s "
            + ("AND source = %s " if self.source_tag else "")
            + "ORDER BY ABS(EXTRACT(EPOCH FROM (price_at - %s))) ASC LIMIT 1"
        )
        from datetime import timedelta
        lo = target - timedelta(minutes=self.tolerance_minutes)
        hi = target + timedelta(minutes=self.tolerance_minutes)
        params = [ticker.upper(), lo, hi]
        if self.source_tag:
            params.append(self.source_tag)
        params.append(target)

        rows = pg.execute(sql, params)
        if not rows:
            raise PriceMissing(f"no cached price for {ticker} near {target.isoformat()}")
        return Decimal(str(rows[0]["price"]))


def _coerce_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
