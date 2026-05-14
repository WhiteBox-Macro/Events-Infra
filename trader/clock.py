"""Clock abstraction.

The trader makes time-sensitive decisions (compute confidence decay from a
news article's age, mark positions, expire stale watchlist rules). All such
calls go through a `Clock` so backtest replay can pin "now" to a historical
timestamp without code changes.

`LiveClock.now()` is wall-clock UTC. `ReplayClock` advances on demand —
typically the backtest harness calls `advance_to(article.published_at)` just
before feeding the article through the dispatcher, so downstream logic sees
the article as if it had just landed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class LiveClock:
    """Wall-clock UTC. The only implementation used in live mode."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ReplayClock:
    """A monotonic clock the caller advances explicitly.

    Used by trader/backtest/replay.py: it walks `news.articles` in
    `published_at` order and calls `advance_to(...)` before invoking
    the dispatcher with each row.
    """

    def __init__(self, start: datetime):
        self._now = _coerce_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, t: datetime) -> None:
        t = _coerce_utc(t)
        if t < self._now:
            raise ValueError(
                f"ReplayClock cannot go backwards (current={self._now}, requested={t})"
            )
        self._now = t


def _coerce_utc(t: datetime) -> datetime:
    """Naive datetimes are interpreted as UTC; everything else is converted."""
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)
