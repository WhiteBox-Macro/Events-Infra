"""Shared bootstrap + loop helpers for scripts/ingest/*.

Every ingester:
  1. sets up logging (one log file per ingester under $DB_BASE/logs/),
  2. enforces a single instance via FileLock,
  3. registers a graceful shutdown handler,
  4. polls its source(s) on a per-source cadence,
  5. writes the raw payload to disk under $DB_BASE/raw/<source>/<yyyy/mm/dd>/,
  6. upserts the row into Postgres via pg.upsert() — the AFTER INSERT trigger
     in 006_event_triggers.sql fires pg_notify, and ON CONFLICT DO UPDATE does
     NOT fire it (Postgres only fires INSERT triggers on actual INSERTs).
"""
from __future__ import annotations

import hashlib
import json
import logging
import signal as _signal
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from dbkit import pg
from dbkit.constants import LOCK_DIR, LOG_DIR, RAW_DIR
from dbkit.filelock import FileLock
from dbkit.shutdown import register_shutdown

# ── Shutdown coordination ─────────────────────────────────────────────────
# All ingesters share the same idiom: a process-wide Event flipped by
# register_shutdown(). Inner loops check stop_event.is_set() to break out.
stop_event = threading.Event()


def _set_stop(signum, frame):  # noqa: ARG001 (signal handler signature)
    stop_event.set()


register_shutdown(_set_stop)


# ── Logging ───────────────────────────────────────────────────────────────
def setup_logging(name: str, *, level: int = logging.INFO) -> logging.Logger:
    """Configure root logger to write to $DB_BASE/logs/<name>.log + stderr.

    Idempotent — calling twice doesn't double-add handlers.
    """
    root = logging.getLogger()
    if getattr(root, "_aotc_configured", False):
        return logging.getLogger(name)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    try:
        log_path = LOG_DIR / f"{name}.log"
        fh = logging.FileHandler(log_path)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        # Falling through to stderr-only is fine — surface a warning.
        root.warning("Could not open log file under %s; continuing with stderr only", LOG_DIR)

    root._aotc_configured = True  # type: ignore[attr-defined]
    return logging.getLogger(name)


# ── Single-instance enforcement ───────────────────────────────────────────
@contextmanager
def singleton_lock(ingester_name: str):
    """Refuse to start if another instance of this ingester is already running.

    BlockingIOError → clean exit with code 3 + message. Avoids two RSS pollers
    racing on the same source.
    """
    lock_path = LOCK_DIR / f"{ingester_name}.lock"
    try:
        with FileLock(lock_path, blocking=False):
            yield
    except BlockingIOError:
        print(
            f"ERROR: another instance of '{ingester_name}' is already running "
            f"(lock: {lock_path}). Refusing to start.",
            file=sys.stderr,
        )
        sys.exit(3)


# ── Raw payload on disk ───────────────────────────────────────────────────
def persist_raw_payload(source_name: str, external_id: str, payload: dict | list) -> str:
    """Write payload to $DB_BASE/raw/<source>/<yyyy/mm/dd>/<sanitized_id>.json.

    Returns the absolute path written. Overwrites silently — duplicates within
    the same day map to the same path, which is intentional (cheaper than
    de-dup logic and idempotent if the ingester reruns).
    """
    now = datetime.now(timezone.utc)
    day_dir = RAW_DIR / source_name / now.strftime("%Y/%m/%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    # Sanitise external_id for filesystem: keep alnum + dash, replace anything
    # else with sha256-prefix to avoid collisions.
    safe = _safe_filename(external_id)
    path = day_dir / f"{safe}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    return str(path)


def _safe_filename(external_id: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in external_id)[:120]
    if cleaned != external_id:
        # Collisions are theoretically possible after substitution; suffix a
        # short hash to disambiguate.
        h = hashlib.sha256(external_id.encode()).hexdigest()[:8]
        return f"{cleaned}__{h}"
    return cleaned


def sha256_id(text: str) -> str:
    """Stable id for sources that don't ship one (RSS items without guid)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── news.sources helpers ──────────────────────────────────────────────────
def iter_active_sources(*, feed_type: Optional[str] = None, category: Optional[str] = None) -> list[dict]:
    """Return active rows from news.sources, optionally filtered."""
    where: dict = {"active": True}
    if feed_type is not None:
        where["feed_type"] = feed_type
    if category is not None:
        where["category"] = category
    return pg.query("news.sources", where=where, order_by="id")


def get_source_by_name(name: str) -> Optional[dict]:
    rows = pg.query("news.sources", where={"name": name}, limit=1)
    return rows[0] if rows else None


def mark_source_polled(source_id: int) -> None:
    pg.update("news.sources", {"last_polled_at": datetime.now(timezone.utc)}, {"id": source_id})


# ── Generic poll loop ─────────────────────────────────────────────────────
def poll_loop(
    *,
    name: str,
    tick_fn,
    tick_interval_sec: float = 5.0,
) -> None:
    """Run `tick_fn()` repeatedly until stop_event is set.

    `tick_fn()` is expected to do its own per-source cadence checks
    (`now - last_polled_at >= poll_interval_sec`). This outer loop just keeps
    the process alive and respects shutdown signals.
    """
    log = logging.getLogger(name)
    log.info("ingester %s starting (tick=%ss)", name, tick_interval_sec)
    while not stop_event.is_set():
        t0 = time.monotonic()
        try:
            tick_fn()
        except Exception:
            log.exception("tick failed; continuing")
        elapsed = time.monotonic() - t0
        sleep = max(0.0, tick_interval_sec - elapsed)
        if sleep > 0:
            stop_event.wait(sleep)
    log.info("ingester %s shutting down", name)
