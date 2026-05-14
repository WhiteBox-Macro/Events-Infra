#!/usr/bin/env python3
"""Batch pre-classify events for the backtest strategy.

Splits events into N batches and sends each batch as a single LLM call.
This gives the LLM context about related events for consistent labeling,
and is much faster than one-at-a-time classification.

Output: events_classified_cache.json — used by the strategy at runtime.

Usage:
    python preclassify.py --start 2024-10-01 --end 2024-12-31 --batches 5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import load_dotenv_files  # noqa: E402

log = logging.getLogger("preclassify")

CACHE_PATH = Path(__file__).resolve().parent / "events_classified_cache.json"

BATCH_PROMPT = """\
You are a financial event tagger. Classify each headline below into a category.

Target tickers for impact assessment: {tickers}

For EACH event, output a JSON object with:
- "event_id": (the ID provided)
- "event_category": Broad category. Use CONSISTENT labels across all events:
  "fed_policy", "earnings_data", "trade_policy", "geopolitical_conflict",
  "corporate_action", "economic_data", "regulatory", "energy_commodity",
  "tech_sector", "labor_market", "fiscal_policy", "defense_military",
  "market_structure", "other"
- "sub_category": More specific (e.g., "rate_decision", "tariff_escalation", "q3_earnings")
- "affected_tickers": Which of [{tickers}] are likely relevant. Can be empty.

Output a JSON array of objects, one per event. Use CONSISTENT category labels —
the same type of event must always get the same category across all batches.

Events to classify:
{events_block}

Output ONLY the JSON array. No markdown, no commentary."""


def fetch_events(start_date: str | None, end_date: str | None) -> list[dict]:
    sql = ("SELECT event_id, publish_time, headline, text_content, "
           "is_regular, surprise, indicator_name "
           "FROM events.classified ORDER BY publish_time ASC")
    rows = pg.execute(sql)

    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        rows = [r for r in rows if r["publish_time"] >= start]
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + \
              __import__("datetime").timedelta(days=1)
        rows = [r for r in rows if r["publish_time"] < end]

    return rows


def format_event_block(events: list[dict]) -> str:
    lines = []
    for ev in events:
        headline = (ev.get("headline") or ev.get("text_content") or "")[:150]
        surprise = ev.get("surprise")
        surprise_str = f", surprise={surprise}" if surprise is not None else ""
        lines.append(f'ID={ev["event_id"]} | {ev["publish_time"].strftime("%Y-%m-%d %H:%M")} | '
                     f'{headline}{surprise_str}')
    return "\n".join(lines)


def classify_batch(client: anthropic.Anthropic, events: list[dict],
                   tickers: str, model: str, batch_num: int, total_batches: int) -> list[dict]:
    events_block = format_event_block(events)
    prompt = BATCH_PROMPT.format(tickers=tickers, events_block=events_block)

    log.info("batch %d/%d: classifying %d events...", batch_num, total_batches, len(events))

    try:
        resp = client.messages.create(
            model=model, max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.error("batch %d failed: %s", batch_num, e)
        return []

    raw = resp.content[0].text if resp.content else ""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        results = json.loads(cleaned)
        if isinstance(results, list):
            log.info("batch %d: got %d classifications", batch_num, len(results))
            return results
    except json.JSONDecodeError:
        log.error("batch %d: failed to parse JSON response", batch_num)

    return []


def load_existing_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            log.info("loaded existing cache with %d entries", len(data))
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch pre-classify events")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--tickers", nargs="+", default=["SPY", "QQQ"])
    parser.add_argument("--batches", type=int, default=5)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    env = load_dotenv_files()
    for k, v in env.items():
        os.environ.setdefault(k, v)

    events = fetch_events(args.start, args.end)
    log.info("fetched %d events for %s to %s", len(events), args.start, args.end)

    cache = load_existing_cache()
    uncached = [e for e in events if str(e["event_id"]) not in cache]
    log.info("already cached: %d, need classification: %d", len(events) - len(uncached), len(uncached))

    if not uncached:
        log.info("all events already cached")
        return 0

    client = anthropic.Anthropic(
        base_url=os.environ.get("CLASSIFIER_LLM_BASE_URL", "http://192.168.1.10:9210"),
        api_key=os.environ.get("CLASSIFIER_LLM_API_KEY", "event_classifier"),
    )

    tickers_str = ", ".join(args.tickers)
    batch_size = max(1, len(uncached) // args.batches)
    batches = []
    for i in range(0, len(uncached), batch_size):
        batches.append(uncached[i:i + batch_size])

    total_classified = 0
    for i, batch in enumerate(batches):
        results = classify_batch(client, batch, tickers_str, args.model, i + 1, len(batches))

        for r in results:
            eid = str(r.get("event_id", ""))
            if eid:
                cache[eid] = {
                    "event_category": r.get("event_category", "other"),
                    "sub_category": r.get("sub_category", ""),
                    "affected_tickers": r.get("affected_tickers", []),
                }
                total_classified += 1

        # Save after each batch (resume-safe)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, default=str)
        log.info("cache saved: %d total entries", len(cache))

        if i < len(batches) - 1:
            time.sleep(2)

    log.info("done: %d newly classified, %d total in cache", total_classified, len(cache))
    return 0


if __name__ == "__main__":
    sys.exit(main())
