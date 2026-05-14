#!/usr/bin/env python3
"""trader/backtest/replay.py — historical event replay.

Walks news.articles + social.posts + macro.releases in `published_at` /
`posted_at` / `released_at` order across the requested window, advances a
ReplayClock to each event's timestamp, and feeds the row through the same
fast-signal + (optional) slow-agent path used in live mode. Decisions are
tagged with `mode='backtest'` and the supplied `experiment_key` so the
report job can group by run.

Paper-trade jobs (mtm + supersede + settle) run inline against the synthetic
clock. They use the same modules as live (trader.paper.*); we just call
`run_once(clock=…, price_source=…)` between events at chosen cadences.

Slow agent is OFF by default — burning the deep model on every historical
event is expensive. `--include-slow` opts in.

CLI:
    python -m trader.backtest.replay \\
        --from 2025-01-01 --to 2026-05-13 \\
        --tickers AAPL,MSFT,NVDA \\
        --experiment-key earnings_v1 \\
        [--include-slow] \\
        [--clean]                 # delete prior data for this experiment_key
        [--price-tolerance-min 1440]   # how stale a cached price can be
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import LOG_DIR, load_dotenv_files  # noqa: E402

from trader import fast_signal  # noqa: E402
from trader import reflect as reflect_mod  # noqa: E402
from trader.clock import Clock, ReplayClock  # noqa: E402
from trader.paper import mtm, settle, supersede  # noqa: E402
from trader.prices import HistoricalPriceSource  # noqa: E402
from trader.slow_agent.llm import is_enabled as slow_enabled  # noqa: E402
from trader.slow_agent.runner import run_slow_for_fast_decision  # noqa: E402
from trader.tickers import resolve_tickers  # noqa: E402

LOG = logging.getLogger("replay")


@dataclass
class Event:
    kind: str            # 'article' | 'post' | 'release'
    ts: datetime
    row: dict


# ── Loading historical events ──────────────────────────────────────────────
def _load_articles(start: datetime, end: datetime, tickers: list[str]) -> list[Event]:
    """Articles for the universe in the window."""
    rows = pg.execute(
        "SELECT * FROM news.articles "
        "WHERE published_at >= %s AND published_at < %s "
        "  AND tickers && %s::text[] "
        "ORDER BY published_at ASC",
        [start, end, tickers],
    )
    return [Event("article", r["published_at"], r) for r in rows if r.get("published_at")]


def _load_posts(start: datetime, end: datetime, tickers: list[str]) -> list[Event]:
    rows = pg.execute(
        "SELECT * FROM social.posts "
        "WHERE posted_at >= %s AND posted_at < %s "
        "  AND tickers && %s::text[] "
        "ORDER BY posted_at ASC",
        [start, end, tickers],
    )
    return [Event("post", r["posted_at"], r) for r in rows if r.get("posted_at")]


def _load_releases(start: datetime, end: datetime) -> list[Event]:
    """All macro releases in the window (macro events fan to every watchlist
    ticker — no ticker filter)."""
    rows = pg.execute(
        "SELECT r.*, i.code AS indicator_code, i.name AS indicator_name, "
        "       i.importance AS indicator_importance "
        "FROM macro.releases r "
        "JOIN macro.indicators i ON i.id = r.indicator_id "
        "WHERE r.released_at >= %s AND r.released_at < %s "
        "ORDER BY r.released_at ASC",
        [start, end],
    )
    return [Event("release", r["released_at"], r) for r in rows if r.get("released_at")]


def _merge_events(*streams: list[Event]) -> list[Event]:
    return sorted((e for stream in streams for e in stream), key=lambda e: e.ts)


# ── Watchlist filter (snapshot at run start so a mid-replay edit doesn't
# silently change the universe) ────────────────────────────────────────────
def _watchlist_tickers(restrict: Optional[list[str]] = None) -> frozenset[str]:
    rows = pg.query("signals.watchlist", select=["ticker"], where={"active": True})
    active = {r["ticker"].upper() for r in rows if r.get("ticker")}
    if restrict:
        active &= {t.upper() for t in restrict}
    return frozenset(active)


# ── Per-event dispatch (no LISTEN/NOTIFY, no thread pool) ─────────────────
def _dispatch_article(event: Event, watchlist: frozenset[str], *,
                      clock: Clock, price_source, experiment_key: str,
                      include_slow: bool) -> list[str]:
    article = event.row
    candidates = resolve_tickers(article, llm_fallback=False)  # LLM disabled in replay
    if not candidates:
        return []
    matches = sorted(set(candidates) & watchlist)
    if not matches:
        return []
    decisions = fast_signal.handle_article_event(
        article, matches,
        clock=clock, price_source=price_source,
        mode="backtest", experiment_key=experiment_key,
    )
    if decisions and include_slow:
        for d in decisions:
            run_slow_for_fast_decision(d, mode="backtest", experiment_key=experiment_key)
    return decisions


def _dispatch_post(event: Event, watchlist: frozenset[str], *,
                   clock: Clock, price_source, experiment_key: str,
                   include_slow: bool) -> list[str]:
    post = event.row
    candidates = resolve_tickers(post, text_fields=("body",), llm_fallback=False)
    if not candidates:
        return []
    matches = sorted(set(candidates) & watchlist)
    if not matches:
        return []
    decisions = fast_signal.handle_post_event(
        post, matches,
        clock=clock, price_source=price_source,
        mode="backtest", experiment_key=experiment_key,
    )
    if decisions and include_slow:
        for d in decisions:
            run_slow_for_fast_decision(d, mode="backtest", experiment_key=experiment_key)
    return decisions


def _dispatch_release(event: Event, watchlist: frozenset[str], *,
                      clock: Clock, price_source, experiment_key: str,
                      include_slow: bool) -> list[str]:
    decisions = fast_signal.handle_release_event(
        event.row, sorted(watchlist),
        clock=clock, price_source=price_source,
        mode="backtest", experiment_key=experiment_key,
    )
    if decisions and include_slow:
        for d in decisions:
            run_slow_for_fast_decision(d, mode="backtest", experiment_key=experiment_key)
    return decisions


# ── Cleanup (used by --clean) ──────────────────────────────────────────────
def _clean_experiment(experiment_key: str) -> dict:
    """Delete prior backtest data tagged with this experiment_key."""
    counts = {"trades": 0, "positions": 0, "mtm": 0, "decisions": 0, "benchmarks": 0}
    with pg.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM signals.mtm_history WHERE position_id IN ("
                "  SELECT pp.position_id FROM signals.paper_positions pp "
                "  JOIN signals.decisions d ON d.decision_id = pp.decision_id "
                "  WHERE d.mode='backtest' AND d.experiment_key = %s)",
                [experiment_key],
            )
            counts["mtm"] = cur.rowcount
            cur.execute(
                "DELETE FROM signals.paper_trades WHERE position_id IN ("
                "  SELECT pp.position_id FROM signals.paper_positions pp "
                "  JOIN signals.decisions d ON d.decision_id = pp.decision_id "
                "  WHERE d.mode='backtest' AND d.experiment_key = %s)",
                [experiment_key],
            )
            counts["trades"] = cur.rowcount
            cur.execute(
                "DELETE FROM signals.paper_positions WHERE decision_id IN ("
                "  SELECT decision_id FROM signals.decisions "
                "  WHERE mode='backtest' AND experiment_key = %s)",
                [experiment_key],
            )
            counts["positions"] = cur.rowcount
            cur.execute(
                "DELETE FROM signals.decisions WHERE mode='backtest' AND experiment_key = %s",
                [experiment_key],
            )
            counts["decisions"] = cur.rowcount
            cur.execute(
                "DELETE FROM signals.benchmark_marks WHERE mode='backtest'"
            )
            counts["benchmarks"] = cur.rowcount
            cur.execute(
                "DELETE FROM signals.experiments WHERE experiment_key = %s",
                [experiment_key],
            )
    return counts


# ── Paper-jobs cadence inside the replay ───────────────────────────────────
@dataclass
class PaperCadence:
    mtm_sec: int = 60 * 60          # mark once per synthetic hour
    supersede_sec: int = 60 * 60
    settle_sec: int = 60 * 60       # check horizon every synthetic hour
    last_mtm: Optional[datetime] = None
    last_supersede: Optional[datetime] = None
    last_settle: Optional[datetime] = None


def _maybe_run_paper(now: datetime, cadence: PaperCadence, *, clock: Clock, price_source) -> None:
    def _due(prev: Optional[datetime], interval: int) -> bool:
        return prev is None or (now - prev).total_seconds() >= interval

    if _due(cadence.last_mtm, cadence.mtm_sec):
        mtm.run_once(clock=clock, price_source=price_source)
        cadence.last_mtm = now
    if _due(cadence.last_supersede, cadence.supersede_sec):
        supersede.run_once(clock=clock, price_source=price_source)
        cadence.last_supersede = now
    if _due(cadence.last_settle, cadence.settle_sec):
        settle.run_once(clock=clock, price_source=price_source)
        cadence.last_settle = now


# ── Main driver ────────────────────────────────────────────────────────────
def replay(*, start: datetime, end: datetime, tickers: list[str],
           experiment_key: str, include_slow: bool, clean: bool,
           price_tolerance_min: int) -> dict:
    if clean:
        cleaned = _clean_experiment(experiment_key)
        LOG.info("clean: %s", cleaned)

    watchlist = _watchlist_tickers(restrict=tickers)
    if not watchlist:
        LOG.warning("watchlist intersection is empty — no signals.watchlist rows match --tickers")
        return {"events": 0, "decisions": 0}

    LOG.info("watchlist (intersected with --tickers): %s", sorted(watchlist))

    events = _merge_events(
        _load_articles(start, end, list(watchlist)),
        _load_posts(start, end, list(watchlist)),
        _load_releases(start, end),
    )
    LOG.info("loaded %d events across %s → %s", len(events), start.date(), end.date())
    if not events:
        return {"events": 0, "decisions": 0}

    clock = ReplayClock(start=events[0].ts)
    price_source = HistoricalPriceSource(
        tolerance_minutes=price_tolerance_min,
        source_tag=None,  # accept any source — backfill or live cache
    )
    cadence = PaperCadence()

    total_decisions = 0
    by_kind = {"article": 0, "post": 0, "release": 0}
    started = time.monotonic()

    for i, evt in enumerate(events):
        clock.advance_to(evt.ts)
        _maybe_run_paper(evt.ts, cadence, clock=clock, price_source=price_source)
        try:
            if evt.kind == "article":
                ds = _dispatch_article(evt, watchlist, clock=clock, price_source=price_source,
                                        experiment_key=experiment_key, include_slow=include_slow)
            elif evt.kind == "post":
                ds = _dispatch_post(evt, watchlist, clock=clock, price_source=price_source,
                                     experiment_key=experiment_key, include_slow=include_slow)
            else:
                ds = _dispatch_release(evt, watchlist, clock=clock, price_source=price_source,
                                        experiment_key=experiment_key, include_slow=include_slow)
            total_decisions += len(ds)
            by_kind[evt.kind] += len(ds)
        except Exception:
            LOG.exception("event %d/%d (%s @ %s) dispatch failed",
                          i + 1, len(events), evt.kind, evt.ts)
        if (i + 1) % 500 == 0:
            LOG.info("progress: %d/%d events; %d decisions so far",
                     i + 1, len(events), total_decisions)

    # ── Wind down: advance clock past horizon to flush remaining positions
    # via settle. We pick max(horizon_hours) across the decisions we wrote
    # so every position gets a chance to close in-window.
    LOG.info("flushing open positions through final settle pass")
    horizon = pg.execute(
        "SELECT COALESCE(MAX(horizon_hours), 24) AS h "
        "FROM signals.decisions WHERE mode='backtest' AND experiment_key = %s",
        [experiment_key],
    )
    max_h = int(horizon[0]["h"]) if horizon else 24
    final_time = events[-1].ts + timedelta(hours=max_h + 1)
    clock.advance_to(final_time)
    settle.run_once(clock=clock, price_source=price_source)
    mtm.run_once(clock=clock, price_source=price_source)
    settle.run_once(clock=clock, price_source=price_source)  # second pass picks up supersede-closes

    elapsed = time.monotonic() - started
    LOG.info(
        "replay done: %d events, %d decisions (article=%d post=%d release=%d) in %.1fs",
        len(events), total_decisions,
        by_kind["article"], by_kind["post"], by_kind["release"], elapsed,
    )

    if include_slow and slow_enabled():
        # Backfill reflections so the report can read them. In live this
        # runs on a cadence; in backtest we just sweep at the end.
        try:
            reflect_mod.run_once(limit=10_000)
        except Exception:
            LOG.exception("reflect sweep failed; report will skip reflection_md")

    return {
        "events": len(events),
        "decisions": total_decisions,
        "by_kind": by_kind,
        "elapsed_sec": round(elapsed, 1),
    }


# ── CLI scaffolding ────────────────────────────────────────────────────────
def _configure_logging() -> None:
    if LOG.handlers:
        return
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_DIR / "backtest_replay.log", maxBytes=5_000_000, backupCount=3
        )
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay historical news/social/macro events through the same agent path")
    ap.add_argument("--from", dest="from_date", required=True, help="ISO date inclusive")
    ap.add_argument("--to", dest="to_date", required=True, help="ISO date exclusive")
    ap.add_argument("--tickers", required=True, help="comma-separated tickers to scope to (must overlap signals.watchlist)")
    ap.add_argument("--experiment-key", required=True, help="tag stored on every decision row; required for the report")
    ap.add_argument("--include-slow", action="store_true",
                    help="also run the slow LangGraph agent on every fast decision (expensive)")
    ap.add_argument("--no-clean", action="store_true",
                    help="skip deletion of prior data for this experiment_key")
    ap.add_argument("--price-tolerance-min", type=int, default=24 * 60,
                    help="how stale a cached price can be (default 1440 = 24h)")
    args = ap.parse_args()

    _configure_logging()
    load_dotenv_files()
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    start = datetime.fromisoformat(args.from_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.to_date).replace(tzinfo=timezone.utc)
    if start >= end:
        print("ERROR: --from must precede --to", file=sys.stderr)
        return 2
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        print("ERROR: --tickers cannot be empty", file=sys.stderr)
        return 2

    result = replay(
        start=start, end=end, tickers=tickers,
        experiment_key=args.experiment_key,
        include_slow=args.include_slow,
        clean=not args.no_clean,
        price_tolerance_min=args.price_tolerance_min,
    )
    LOG.info("result: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
