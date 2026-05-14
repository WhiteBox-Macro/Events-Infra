#!/usr/bin/env python3
"""dispatcher.py — long-running LISTEN/NOTIFY loop.

One dispatcher process owns three Postgres notification channels:

  article_in  ← news.articles      (006_event_triggers.sql)
  post_in     ← social.posts
  macro_in    ← macro.releases

For each notification:
  1. Load the freshly-inserted row.
  2. Resolve tickers (trader.tickers.resolve_tickers).
  3. Intersect with `signals.watchlist WHERE active=TRUE`.
  4. Hand off to the fast-path / slow-path handlers (Phase 3 / Phase 4 plug
     in here; for Phase 2 we just log the decision-ready event).

Architecture notes:

  * The LISTEN connection is held outside the pool in autocommit mode —
    psycopg2's LISTEN requires a dedicated, non-pooled session because
    notifications are bound to the connection.
  * select() with a 1s timeout means SIGINT / SIGTERM is acted on within
    a second even when the channel is quiet.
  * Watchlist is cached in-memory with a 30s TTL — keeps the hot path one
    DB call per tick instead of one per notification.
  * The trader's own clock is the Clock interface (LiveClock in this loop).
    Backtest uses ReplayClock instead and does NOT run this dispatcher loop —
    it calls `handle_event()` directly per article (Phase 6).

Singleton enforcement uses the same FileLock pattern as scripts/ingest/*.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import select
import signal as _signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from psycopg2.extras import RealDictCursor

from dbkit import pg  # noqa: E402
from dbkit.constants import LOCK_DIR, LOG_DIR, load_dotenv_files  # noqa: E402
from dbkit.filelock import FileLock  # noqa: E402
from dbkit.shutdown import register_shutdown  # noqa: E402

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from trader import fast_signal  # noqa: E402
from trader.clock import Clock, LiveClock  # noqa: E402
from trader.prices import LivePriceSource, PriceSource  # noqa: E402
from trader.slow_agent.llm import is_enabled as slow_agent_enabled  # noqa: E402
from trader.slow_agent.runner import run_slow_for_fast_decision  # noqa: E402
from trader.tickers import resolve_tickers  # noqa: E402

LOG = logging.getLogger("dispatcher")

CHANNELS = ("article_in", "post_in", "macro_in")
SELECT_TIMEOUT_SEC = 1.0
WATCHLIST_TTL_SEC = 30.0

# stop_event flips when SIGINT / SIGTERM lands; the main loop checks it.
stop_event = threading.Event()

# Process-singleton clock + price source. Backtest replay (Phase 6) swaps the
# clock for ReplayClock and the price source for HistoricalPriceSource before
# invoking the handlers, so live and backtest share this dispatcher's code.
CLOCK: Clock = LiveClock()
PRICE_SOURCE: PriceSource = LivePriceSource()

# Background pool for slow-agent runs. One worker is plenty for current load
# (each run is 5-30s of mostly LLM I/O wait); raising it past ~3 mostly burns
# Anthropic tokens without improving end-to-end latency. The pool stays alive
# for the dispatcher's lifetime; in-flight runs finish on shutdown.
SLOW_POOL_WORKERS = int(os.environ.get("SLOW_POOL_WORKERS", "1"))
_slow_pool: Optional[ThreadPoolExecutor] = None


def _get_slow_pool() -> ThreadPoolExecutor:
    global _slow_pool
    if _slow_pool is None:
        _slow_pool = ThreadPoolExecutor(
            max_workers=SLOW_POOL_WORKERS,
            thread_name_prefix="slow_agent",
        )
    return _slow_pool


def _fire_slow_agent(decision_ids: list[str]) -> None:
    """Submit each fresh fast decision_id to the slow-agent thread pool.

    Failure paths (slow agent disabled, missing API key, langgraph not
    installed, …) are handled inside runner.run_slow_for_fast_decision so
    this call site stays small and the dispatcher's hot path keeps moving."""
    if not decision_ids or not slow_agent_enabled():
        return
    pool = _get_slow_pool()
    for did in decision_ids:
        pool.submit(_run_slow_safely, did)


def _run_slow_safely(decision_id: str) -> None:
    try:
        run_slow_for_fast_decision(decision_id)
    except Exception:
        LOG.exception("slow agent run crashed for fast decision %s", decision_id)


# ── Watchlist cache ─────────────────────────────────────────────────────────
class WatchlistCache:
    """Small TTL cache for active watchlist tickers.

    The watchlist is small (dozens to thousands of rows) but checked on every
    incoming event. Refreshing every 30s is cheap and keeps cron-style
    additions visible quickly.
    """

    def __init__(self, ttl_sec: float = WATCHLIST_TTL_SEC):
        self._ttl = ttl_sec
        self._cached: frozenset[str] = frozenset()
        self._cached_at: float = 0.0

    def active(self) -> frozenset[str]:
        now = time.monotonic()
        if (now - self._cached_at) < self._ttl:
            return self._cached
        try:
            rows = pg.query("signals.watchlist", select=["ticker"], where={"active": True})
        except Exception:
            LOG.exception("watchlist refresh failed; reusing previous snapshot")
            self._cached_at = now
            return self._cached
        self._cached = frozenset(r["ticker"].upper() for r in rows if r.get("ticker"))
        self._cached_at = now
        return self._cached


# ── Event loading ───────────────────────────────────────────────────────────
def load_article(article_id: str) -> Optional[dict]:
    rows = pg.execute(
        "SELECT * FROM news.articles WHERE article_id = %s",
        [article_id],
    )
    return rows[0] if rows else None


def load_post(post_id: str) -> Optional[dict]:
    rows = pg.execute(
        "SELECT * FROM social.posts WHERE post_id = %s",
        [post_id],
    )
    return rows[0] if rows else None


def load_release(release_id: str) -> Optional[dict]:
    rows = pg.execute(
        "SELECT r.*, i.code AS indicator_code, i.name AS indicator_name, "
        "       i.importance AS indicator_importance "
        "FROM macro.releases r "
        "JOIN macro.indicators i ON i.id = r.indicator_id "
        "WHERE r.release_id = %s",
        [release_id],
    )
    return rows[0] if rows else None


# ── Per-channel handlers (Phase 2 stubs; Phase 3/4 will wire signal logic) ──
def handle_article(article_id: str, watchlist: frozenset[str]) -> None:
    article = load_article(article_id)
    if not article:
        LOG.warning("article_in: %s — row missing", article_id)
        return

    candidates = resolve_tickers(article)
    if not candidates:
        LOG.info(
            "article_in id=%s src=%s title=%r → no tickers resolved",
            article_id, article.get("source_id"), _short(article.get("title")),
        )
        return

    matches = sorted(set(candidates) & watchlist)
    if not matches:
        LOG.info(
            "article_in id=%s title=%r → resolved=%s, none on watchlist",
            article_id, _short(article.get("title")), candidates,
        )
        return

    LOG.info(
        "article_in id=%s title=%r → DISPATCH tickers=%s (resolved=%s)",
        article_id, _short(article.get("title")), matches, candidates,
    )
    decisions = fast_signal.handle_article_event(
        article, matches, clock=CLOCK, price_source=PRICE_SOURCE,
    )
    if decisions:
        LOG.info("article_in id=%s → %d fast decision(s) written; firing slow agent", article_id, len(decisions))
        _fire_slow_agent(decisions)


def _tracked_handle_for(post: dict) -> Optional[dict]:
    """Look up the post's author in social.handles. Returns row or None.

    Silently returns None when social.handles doesn't exist yet (migration
    007 not applied) — keeps the dispatcher working on older schemas."""
    author = post.get("author")
    source = post.get("source")
    if not author or not source:
        return None
    try:
        rows = pg.execute(
            "SELECT handle_id, username, category, tags, impact_weight, expected_themes "
            "FROM social.handles "
            "WHERE platform = %s AND lower(username) = lower(%s) AND active = TRUE "
            "LIMIT 1",
            [source, author],
        )
    except Exception:
        return None
    return rows[0] if rows else None


def handle_post(post_id: str, watchlist: frozenset[str]) -> None:
    post = load_post(post_id)
    if not post:
        LOG.warning("post_in: %s — row missing", post_id)
        return

    handle = _tracked_handle_for(post)
    handle_weight = float(handle["impact_weight"]) if handle else 1.0

    candidates = resolve_tickers(post, text_fields=("body",))
    matches = sorted(set(candidates) & watchlist) if candidates else []

    # ── Path A: cashtag-tagged post (whether the author is tracked or not).
    # Tracked authors get their impact_weight rolled into the score; untracked
    # ones still flow through the regular fast scorer.
    if matches:
        LOG.info(
            "post_in id=%s src=%s author=%s tracked=%s → DISPATCH tickers=%s",
            post_id, post.get("source"), post.get("author"), bool(handle), matches,
        )
        decisions = fast_signal.handle_post_event(
            post, matches, clock=CLOCK, price_source=PRICE_SOURCE,
            handle_weight=handle_weight,
        )
        if decisions:
            LOG.info("post_in id=%s → %d fast decision(s) written; firing slow agent",
                     post_id, len(decisions))
            _fire_slow_agent(decisions)
        return

    # ── Path B: post from a tracked handle but no cashtag intersects the
    # watchlist. Route to LLM theme-inference so a Trump tweet about Iran
    # still produces decisions on defense / oil / etc.
    if handle:
        try:
            from trader.social_inference import handle_influencer_post
        except Exception:
            LOG.exception("post_in id=%s: failed to import social_inference; dropping", post_id)
            return
        LOG.info(
            "post_in id=%s @%s (tracked, no cashtag) → LLM theme inference",
            post_id, post.get("author"),
        )
        try:
            decisions = handle_influencer_post(
                post, clock=CLOCK, price_source=PRICE_SOURCE,
            )
        except Exception:
            LOG.exception("post_in id=%s: social_inference crashed", post_id)
            return
        if decisions:
            LOG.info("post_in id=%s → %d inference decision(s); firing slow agent",
                     post_id, len(decisions))
            _fire_slow_agent(decisions)
        return

    # ── Path C: no cashtag, no tracked handle. Drop.
    if not candidates:
        LOG.debug(
            "post_in id=%s src=%s author=%s → no tickers, untracked author; dropped",
            post_id, post.get("source"), post.get("author"),
        )
    else:
        LOG.debug(
            "post_in id=%s src=%s → resolved=%s, none on watchlist; dropped",
            post_id, post.get("source"), candidates,
        )


def handle_release(release_id: str, watchlist: frozenset[str]) -> None:
    release = load_release(release_id)
    if not release:
        LOG.warning("macro_in: %s — row missing", release_id)
        return
    # Macro events don't filter by ticker — every watchlist ticker is a
    # potential consumer. The fast path decides which names actually react
    # based on sector / regime.
    LOG.info(
        "macro_in id=%s code=%s importance=%s value=%s surprise=%s released_at=%s → DISPATCH",
        release_id,
        release.get("indicator_code"),
        release.get("indicator_importance"),
        release.get("value"),
        release.get("surprise"),
        release.get("released_at"),
    )
    decisions = fast_signal.handle_release_event(
        release, sorted(watchlist), clock=CLOCK, price_source=PRICE_SOURCE,
    )
    if decisions:
        LOG.info("macro_in id=%s → %d fast decision(s) written; firing slow agent", release_id, len(decisions))
        _fire_slow_agent(decisions)


# ── Dispatch table + main loop ──────────────────────────────────────────────
HANDLERS = {
    "article_in": handle_article,
    "post_in": handle_post,
    "macro_in": handle_release,
}


def dispatch(channel: str, payload: str, watchlist: WatchlistCache) -> None:
    handler = HANDLERS.get(channel)
    if handler is None:
        LOG.warning("unknown channel %s (payload=%s)", channel, payload)
        return
    active = watchlist.active()
    t0 = time.monotonic()
    try:
        handler(payload, active)
    except Exception:
        LOG.exception("handler for %s failed (payload=%s)", channel, payload)
    elapsed_ms = (time.monotonic() - t0) * 1000
    if elapsed_ms > 50:
        LOG.info("dispatch %s payload=%s elapsed=%.1fms", channel, payload, elapsed_ms)


def _open_listen_connection() -> psycopg2.extensions.connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        for ch in CHANNELS:
            cur.execute(f"LISTEN {ch}")
    return conn


def run(*, inject: Optional[tuple[str, str]] = None) -> int:
    """Main loop. `inject=(channel, payload)` skips LISTEN and runs one
    synthetic dispatch, then exits — used by tests / `--inject` smoke runs."""
    log = _configure_logging()
    watchlist = WatchlistCache()

    if inject is not None:
        ch, payload = inject
        log.info("inject: dispatching synthetic %s payload=%s", ch, payload)
        dispatch(ch, payload, watchlist)
        return 0

    conn = _open_listen_connection()
    log.info("dispatcher started; LISTENing on %s", ", ".join(CHANNELS))

    try:
        while not stop_event.is_set():
            r, _, _ = select.select([conn], [], [], SELECT_TIMEOUT_SEC)
            if stop_event.is_set():
                break
            if not r:
                continue
            conn.poll()
            while conn.notifies:
                n = conn.notifies.pop(0)
                LOG.debug("notify: channel=%s payload=%s pid=%s",
                          n.channel, n.payload, n.pid)
                dispatch(n.channel, n.payload, watchlist)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        # Drain in-flight slow-agent runs so we don't lose a half-written
        # decision row. wait=True blocks until all queued runs complete;
        # cancel_futures=False because we want each one to finish or fail
        # cleanly rather than abort mid-LLM-call.
        global _slow_pool
        if _slow_pool is not None:
            log.info("draining slow-agent pool…")
            _slow_pool.shutdown(wait=True, cancel_futures=False)
            _slow_pool = None
    log.info("dispatcher stopped cleanly")
    return 0


# ── Process scaffolding (matches scripts/ingest/_common patterns) ───────────
def _configure_logging() -> logging.Logger:
    if LOG.handlers:
        return LOG
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_DIR / "dispatcher.log", maxBytes=5_000_000, backupCount=3
        )
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError:
        pass
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return LOG


def _short(s: Optional[str], n: int = 80) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _on_signal(signum, _frame):
    stop_event.set()


def main() -> int:
    load_dotenv_files()
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(description="LISTEN/NOTIFY trading-event dispatcher")
    ap.add_argument(
        "--inject", metavar="CHANNEL:UUID",
        help="dispatch one synthetic event (no LISTEN). For smoke tests.",
    )
    args = ap.parse_args()

    register_shutdown(_on_signal)

    inject_pair: Optional[tuple[str, str]] = None
    if args.inject:
        if ":" not in args.inject:
            print("ERROR: --inject expects CHANNEL:UUID", file=sys.stderr)
            return 2
        ch, payload = args.inject.split(":", 1)
        if ch not in CHANNELS:
            print(f"ERROR: --inject channel must be one of {CHANNELS}", file=sys.stderr)
            return 2
        inject_pair = (ch, payload)

    lock_path = LOCK_DIR / "dispatcher.lock"
    try:
        # No FileLock for --inject (we want to be able to inject while a live
        # dispatcher is running).
        if inject_pair is not None:
            return run(inject=inject_pair)
        with FileLock(lock_path, blocking=False):
            return run()
    except BlockingIOError:
        print(
            f"ERROR: another dispatcher is already running (lock {lock_path})",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
