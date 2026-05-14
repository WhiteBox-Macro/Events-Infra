"""Slow-supersession close.

When the slow LangGraph agent disagrees with the fast deterministic call,
we de-risk the fast-opened position immediately rather than waiting for
the horizon.

Rules (kept conservative — easy to relax later):

  * If the slow rating is on the *opposite* side (Buy/Overweight ↔ Sell/Underweight
    or either ↔ Hold), close the fast position with reason='slow_inverted'.
  * If both are same-direction but slow confidence < SUPERSEDE_DOWNGRADE_DELTA
    below fast confidence (i.e. the slow agent is much less convinced), close
    with reason='slow_downgraded'. Default delta = 0.30.
  * Otherwise no action — the fast position rides to horizon.

Position closure here writes to paper_trades + flips paper_positions to
status='closed'; settle.py later picks up the decision row and writes
raw_return / alpha_return / pending=false.

Idempotency: we only act on slow decisions whose fast counterpart still has
an open position AND we haven't acted yet (look for a paper_trades row with
metadata->>'reason' starting with 'slow_'). A second supersede tick is a
no-op for the same pair.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from dbkit import pg
from trader.clock import Clock, LiveClock
from trader.paper.execute import close_position
from trader.prices import PriceSource, LivePriceSource

_log = logging.getLogger(__name__)

LONG = {"Buy", "Overweight"}
SHORT = {"Sell", "Underweight"}
HOLD = {"Hold"}


def _direction(rating: str) -> str:
    if rating in LONG:
        return "long"
    if rating in SHORT:
        return "short"
    return "hold"


def _delta() -> float:
    try:
        return float(os.environ.get("SUPERSEDE_DOWNGRADE_DELTA", "0.30"))
    except (TypeError, ValueError):
        return 0.30


def _pending_slow_vs_fast() -> list[dict]:
    """Slow decisions whose fast counterpart still has an open position and
    that we haven't acted on yet (no 'slow_*' close in paper_trades)."""
    return pg.execute(
        "SELECT s.decision_id AS slow_id, s.rating AS slow_rating, "
        "       s.confidence AS slow_confidence, "
        "       f.decision_id AS fast_id, f.rating AS fast_rating, "
        "       f.confidence AS fast_confidence, "
        "       pp.position_id, pp.ticker, pp.mode "
        "FROM signals.decisions s "
        "JOIN signals.decisions f ON f.decision_id = s.supersedes "
        "JOIN signals.paper_positions pp ON pp.decision_id = f.decision_id "
        "WHERE s.tier = 'slow' "
        "  AND pp.status = 'open' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM signals.paper_trades pt "
        "    WHERE pt.position_id = pp.position_id "
        "      AND pt.kind = 'close' "
        "      AND pt.metadata->>'reason' LIKE 'slow_%%'"
        "  )"
    )


def _should_close(fast_rating: str, slow_rating: str,
                  fast_conf: float, slow_conf: float, delta: float) -> Optional[str]:
    fd = _direction(fast_rating)
    sd = _direction(slow_rating)
    if fd != sd:
        return "slow_inverted"
    # Same direction (or both hold — but a Hold→Hold case wouldn't have an open
    # position anyway). De-risk if slow confidence has dropped sharply.
    try:
        if fast_conf is not None and slow_conf is not None and (fast_conf - slow_conf) >= delta:
            return "slow_downgraded"
    except (TypeError, ValueError):
        pass
    return None


def run_once(*, clock: Optional[Clock] = None, price_source: Optional[PriceSource] = None) -> dict:
    clock = clock or LiveClock()
    price_source = price_source or LivePriceSource()
    delta = _delta()

    inverted = 0
    downgraded = 0
    for row in _pending_slow_vs_fast():
        try:
            reason = _should_close(
                row.get("fast_rating") or "",
                row.get("slow_rating") or "",
                float(row.get("fast_confidence") or 0),
                float(row.get("slow_confidence") or 0),
                delta,
            )
            if reason is None:
                continue
            realized = close_position(
                position_id=row["position_id"],
                clock=clock,
                price_source=price_source,
                reason=reason,
            )
            if realized is None:
                continue
            if reason == "slow_inverted":
                inverted += 1
            else:
                downgraded += 1
            _log.info(
                "supersede: closed position %s (%s) — fast=%s/%s, slow=%s/%s, reason=%s",
                row["position_id"], row["ticker"],
                row.get("fast_rating"), row.get("fast_confidence"),
                row.get("slow_rating"), row.get("slow_confidence"),
                reason,
            )
        except Exception:
            _log.exception("supersede: failed to close position %s", row.get("position_id"))
    return {"inverted": inverted, "downgraded": downgraded}
