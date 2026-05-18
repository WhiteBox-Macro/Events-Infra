"""Batch classification orchestrator with parallel workers.

Uses FOR UPDATE SKIP LOCKED so multiple workers grab non-overlapping rows.
Each worker: claim row → read file → LLM classify → cross-check → write.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import DB_BASE  # noqa: E402

from extract import extract_mechanical, find_discrepancies  # noqa: E402
from prompt import (classify_tweet, reclassify_with_discrepancy,  # noqa: E402
                    DEFAULT_TARGET_TICKERS, ALLOWED_IMPACT_MARKETS,
                    ALLOWED_ROLES, MAX_TICKER_IMPACTS)

log = logging.getLogger("classifier")

RAW_ROOT = DB_BASE / "events" / "raw"

LLM_TIMEOUT_SEC = 60

# Default model for the unified Sonnet path. Override via env or CLI.
DEFAULT_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-sonnet-4-6")


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.classified = 0
        self.failed = 0
        self.skipped = 0
        self.reclassified = 0
        self._start = time.monotonic()

    def inc(self, key: str):
        with self._lock:
            setattr(self, key, getattr(self, key) + 1)

    @property
    def total(self):
        return self.classified + self.failed + self.skipped

    @property
    def elapsed(self):
        return time.monotonic() - self._start

    @property
    def rate(self):
        e = self.elapsed
        return self.total / e if e > 0 else 0

    def summary(self) -> str:
        return (f"classified={self.classified} failed={self.failed} skipped={self.skipped} "
                f"reclassified={self.reclassified} total={self.total} "
                f"elapsed={self.elapsed:.0f}s rate={self.rate:.1f}/s")


def claim_pending_row(source_channel: str = "twitter_twitterapiio") -> dict | None:
    """Atomically claim one pending row using FOR UPDATE SKIP LOCKED."""
    rows = pg.execute(
        "UPDATE events.raw SET classify_status = 'processing' "
        "WHERE raw_id = ("
        "  SELECT raw_id FROM events.raw "
        "  WHERE source_channel = %s AND classify_status = 'pending' "
        "  ORDER BY published_at ASC NULLS LAST "
        "  LIMIT 1 "
        "  FOR UPDATE SKIP LOCKED"
        ") RETURNING raw_id, source_channel, external_id, file_path, published_at, metadata",
        [source_channel],
    )
    return rows[0] if rows else None


def read_raw_file(file_path: str) -> dict | None:
    full_path = RAW_ROOT / file_path
    if not full_path.exists():
        return None
    try:
        return json.loads(full_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _pg_array(lst: list | None) -> str:
    if not lst:
        return "{}"
    escaped = [str(item).replace('"', '\\"') for item in lst]
    return "{" + ",".join(f'"{e}"' for e in escaped) + "}"


def _clamp_ticker_impacts(impacts: list, target_tickers: list[str]) -> list[dict]:
    """Enforce contract: max 3, only universe tickers, valid role enum, 0<=weight<=1.

    The prompt is advisory; this helper is the authoritative boundary.
    Order preserved (LLM's ranking matters).
    """
    if not isinstance(impacts, list):
        return []
    universe = set(target_tickers)
    out = []
    for entry in impacts:
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker")
        weight = entry.get("weight")
        role = entry.get("role")
        if ticker not in universe:
            continue
        if role not in ALLOWED_ROLES:
            continue
        try:
            w = float(weight)
        except (TypeError, ValueError):
            continue
        w = max(0.0, min(1.0, w))
        out.append({"ticker": ticker, "weight": round(w, 4), "role": role})
        if len(out) >= MAX_TICKER_IMPACTS:
            break
    return out


def _filter_markets(markets) -> list[str]:
    """Restrict to the 8-value controlled enum. Drop anything else silently."""
    if not isinstance(markets, list):
        return []
    return [m for m in markets if isinstance(m, str) and m in ALLOWED_IMPACT_MARKETS]


def build_classified_row(raw_row: dict, llm: dict, mechanical: dict,
                          target_tickers: list[str]) -> dict:
    """Map the unified LLM output to the events.classified row dict.

    Post-migration 008, the table has the new ticker_impacts JSONB + sector +
    event_outcome + classifier_version + raw_classification columns alongside
    the legacy ones (which migration 009 will rename/drop).

    This row populates BOTH the new and the legacy columns — the legacy ones
    keep downstream consumers working during the migration window.
    """
    is_regular = bool(llm.get("is_regular", False))
    impacts = _clamp_ticker_impacts(llm.get("ticker_impacts", []), target_tickers)
    markets = _filter_markets(llm.get("impact_markets", []))
    countries = llm.get("countries") if isinstance(llm.get("countries"), list) else []
    sector = llm.get("sector") if isinstance(llm.get("sector"), str) else None

    return {
        "raw_id": raw_row["raw_id"],
        "source_channel": raw_row["source_channel"],
        "publish_time": mechanical["publish_time"] or raw_row.get("published_at") or datetime.now(timezone.utc),
        "headline": llm.get("headline") or mechanical.get("headline"),
        "text_content": llm.get("text_content"),

        # event taxonomy
        "event_category": llm.get("event_category", "other"),
        "event_type": llm.get("event_type", "other"),
        "event_outcome": llm.get("event_outcome"),
        "is_regular": is_regular,

        # opinion — post-009 names
        "tone": llm.get("tone", "neutral"),
        "magnitude": llm.get("magnitude", "minor"),
        "confidence": llm.get("confidence", 0.5),

        # affected entities — unified shape
        "primary_ticker": llm.get("primary_ticker") or mechanical.get("primary_ticker"),
        "ticker_impacts": json.dumps(impacts),
        "sector": sector,
        "impact_markets": _pg_array(markets),
        "countries": _pg_array(countries),

        # scheduled block (only if is_regular)
        "indicator_name": llm.get("indicator_name") if is_regular else None,
        "scheduled_time": None,
        "consensus_value": llm.get("consensus_value") if is_regular else None,
        "actual_value": llm.get("actual_value") if is_regular else None,
        "surprise": llm.get("surprise") if is_regular else None,
        "surprise_z": None,
        "reporting_period": llm.get("reporting_period") if is_regular else None,

        # dedup chains (filled by downstream)
        "dedup_cluster_id": None,
        "cluster_sequence": None,
        "related_event_id": None,

        # provenance
        "classified_by": "sonnet-4.6/unified-v3-trader",
        "classifier_version": 3,
        "raw_classification": json.dumps(llm),
        "metadata": json.dumps({}),
    }


def mark_status(raw_id, status: str, error: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    if error:
        pg.execute(
            "UPDATE events.raw SET classify_status=%s, classified_at=%s, "
            "metadata = metadata || %s WHERE raw_id=%s",
            [status, now, json.dumps({"last_error": error}), raw_id],
        )
    else:
        pg.execute(
            "UPDATE events.raw SET classify_status=%s, classified_at=%s WHERE raw_id=%s",
            [status, now, raw_id],
        )


def classify_one(raw_row: dict, stats: Stats, target_tickers: list[str],
                  model: str = DEFAULT_MODEL) -> None:
    """Classify a single claimed row. Updates stats."""
    ext_id = raw_row["external_id"]

    raw_data = read_raw_file(raw_row["file_path"])
    if not raw_data:
        mark_status(raw_row["raw_id"], "failed", error="raw file missing")
        stats.inc("failed")
        return

    payload = raw_data.get("payload", {})
    text = payload.get("text", "")
    if not text.strip():
        mark_status(raw_row["raw_id"], "skipped", error="empty text")
        stats.inc("skipped")
        return

    mechanical = extract_mechanical(payload)
    pub_str = mechanical["publish_time"].isoformat() if mechanical["publish_time"] else ""

    llm_result = classify_tweet(text, pub_str, target_tickers=target_tickers, model=model)
    if not llm_result:
        mark_status(raw_row["raw_id"], "failed", error="LLM invalid response")
        stats.inc("failed")
        return

    discrepancies = find_discrepancies(llm_result, mechanical)
    if discrepancies:
        log.debug("discrepancies for %s: %s", ext_id, discrepancies)
        llm_result_2 = reclassify_with_discrepancy(
            text, pub_str, llm_result, mechanical, discrepancies,
            target_tickers=target_tickers, model=model,
        )
        if llm_result_2:
            llm_result = llm_result_2
        stats.inc("reclassified")

    llm_result["text_content"] = text
    row = build_classified_row(raw_row, llm_result, mechanical, target_tickers)

    if discrepancies:
        meta = json.loads(row["metadata"])
        meta["cross_check"] = "reclassified"
        meta["reclassified"] = True
        meta["discrepancies"] = discrepancies
        row["metadata"] = json.dumps(meta)

    try:
        pg.upsert("events.classified", row, conflict_on=["raw_id"])
        mark_status(raw_row["raw_id"], "classified")
        stats.inc("classified")
    except Exception:
        log.exception("DB write failed for %s", ext_id)
        mark_status(raw_row["raw_id"], "failed", error="db write error")
        stats.inc("failed")


def worker_loop(worker_id: int, stats: Stats, stop: threading.Event,
                 target_tickers: list[str], model: str = DEFAULT_MODEL) -> None:
    """Single worker: claim rows and classify until none left or stopped."""
    wlog = logging.getLogger(f"worker-{worker_id}")
    wlog.info("started")
    idle_count = 0

    while not stop.is_set():
        row = claim_pending_row()
        if not row:
            idle_count += 1
            if idle_count >= 3:
                wlog.info("no more rows, exiting")
                break
            time.sleep(1)
            continue

        idle_count = 0
        try:
            classify_one(row, stats, target_tickers, model=model)
        except Exception:
            wlog.exception("unhandled error for %s", row.get("external_id"))
            mark_status(row["raw_id"], "failed", error="unhandled exception")
            stats.inc("failed")

    wlog.info("stopped (%s)", stats.summary())


def run_parallel(num_workers: int = 6, *, retry_failed: bool = False,
                  target_tickers: list[str] | None = None,
                  model: str = DEFAULT_MODEL) -> dict:
    tickers = target_tickers or DEFAULT_TARGET_TICKERS
    if retry_failed:
        count = pg.execute(
            "UPDATE events.raw SET classify_status='pending' "
            "WHERE source_channel='twitter_twitterapiio' AND classify_status IN ('failed', 'processing') "
            "RETURNING raw_id"
        )
        log.info("reset %d failed/stuck rows to pending", len(count))

    remaining = pg.execute(
        "SELECT count(*) as n FROM events.raw "
        "WHERE source_channel='twitter_twitterapiio' AND classify_status='pending'"
    )
    pending = remaining[0]["n"] if remaining else 0
    log.info("starting %d workers, %d rows pending, model=%s, universe=%d tickers",
             num_workers, pending, model, len(tickers))

    stats = Stats()
    stop = threading.Event()

    with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="clf") as pool:
        futures = [pool.submit(worker_loop, i, stats, stop, tickers, model)
                   for i in range(num_workers)]

        try:
            while not all(f.done() for f in futures):
                time.sleep(10)
                log.info("[orchestrator] %s", stats.summary())
        except KeyboardInterrupt:
            log.info("interrupted — stopping workers")
            stop.set()
            for f in futures:
                f.result(timeout=30)

    log.info("[FINAL] %s", stats.summary())
    return {
        "classified": stats.classified,
        "failed": stats.failed,
        "skipped": stats.skipped,
        "reclassified": stats.reclassified,
        "elapsed_sec": round(stats.elapsed, 1),
        "rate_per_sec": round(stats.rate, 2),
        "model": model,
        "universe_size": len(tickers),
    }
