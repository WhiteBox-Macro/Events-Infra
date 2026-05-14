#!/usr/bin/env python3
"""news_rss.py — generic RSS/Atom poller.

Reads active rows from news.sources WHERE feed_type IN ('rss','atom').
Each source has its own poll_interval_sec. We honour per-source overrides
in metadata: `user_agent` (SEC requires one with a contact email) and any
additional HTTP headers under `headers`.

Layout choices:
  * One process polls all RSS sources serially. Sources with short cadence
    poll more often via a single shared tick loop. This keeps connection
    count low and ordering predictable for the trigger -> dispatcher path.
  * Body content stays in the feed entry — we don't fetch the article HTML
    here. That's a separate enricher's job (and SEC + Fed feeds include
    enough text in `summary` to make a fast-path decision off the headline).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import feedparser  # noqa: E402

from dbkit import pg  # noqa: E402
from dbkit.constants import load_dotenv_files  # noqa: E402
from dbkit.http import request_text  # noqa: E402
from scripts.ingest._common import (  # noqa: E402
    iter_active_sources,
    mark_source_polled,
    persist_raw_payload,
    poll_loop,
    setup_logging,
    sha256_id,
    singleton_lock,
    stop_event,
)

INGESTER_NAME = "news_rss"
DEFAULT_USER_AGENT = "AOTC-Signals/0.1 (contact@example.com)"


def _due(source: dict) -> bool:
    """True when the source hasn't been polled in at least poll_interval_sec."""
    last = source.get("last_polled_at")
    if last is None:
        return True
    interval = source.get("poll_interval_sec") or 60
    return (datetime.now(timezone.utc) - last).total_seconds() >= interval


def _entry_external_id(entry: dict, url: str) -> str:
    """Prefer feed-provided id; fall back to a stable hash of the URL."""
    for key in ("id", "guid"):
        v = entry.get(key)
        if v:
            return str(v).strip()
    return sha256_id(url or entry.get("title", ""))


def _entry_published_at(entry: dict) -> datetime | None:
    """Read published_parsed (struct_time, UTC) and convert to aware datetime."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if not st:
            continue
        try:
            return datetime(*st[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _build_headers(source: dict) -> dict:
    meta = source.get("metadata") or {}
    headers = {
        "User-Agent": meta.get("user_agent") or DEFAULT_USER_AGENT,
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml; q=0.9, */*; q=0.1",
    }
    headers.update(meta.get("headers") or {})
    return headers


def _poll_source(source: dict, log) -> int:
    """Fetch the feed, upsert any new entries. Returns count of new entries."""
    name = source["name"]
    url = source["feed_url"]
    headers = _build_headers(source)

    try:
        text = request_text("GET", url, headers=headers, timeout=15)
    except Exception as exc:
        log.warning("source %s fetch failed: %s", name, exc)
        return 0

    parsed = feedparser.parse(text)
    if parsed.bozo and not parsed.entries:
        log.warning("source %s could not be parsed: %s", name, parsed.bozo_exception)
        return 0

    inserted = 0
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        external_id = _entry_external_id(entry, link)
        published_at = _entry_published_at(entry)

        # The raw payload is the parsed entry dict — keep everything so the
        # enrichment layer can change its mind later about what it cares about.
        raw_payload = {
            "feed_url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "entry": _coerce(entry),
        }
        body_path = persist_raw_payload(name, external_id, raw_payload)

        author = (entry.get("author") or "").strip() or None
        summary = (entry.get("summary") or "").strip() or None

        row = {
            "source_id": source["id"],
            "external_id": external_id,
            "url": link or url,
            "title": title,
            "summary": summary,
            "author": author,
            "body_path": body_path,
            "published_at": published_at,
            "language": (entry.get("language") or "").strip() or None,
            "metadata": {
                "feed_id": entry.get("id"),
                "tags": [t.get("term") for t in (entry.get("tags") or []) if t.get("term")],
            },
        }

        try:
            pg.upsert("news.articles", row, conflict_on=["source_id", "external_id"])
            inserted += 1
        except Exception:
            log.exception("upsert failed for %s/%s", name, external_id)

    mark_source_polled(source["id"])
    if inserted:
        log.info("source %s: %d entries upserted", name, inserted)
    return inserted


def _coerce(obj):
    """Make a feedparser entry JSON-serialisable.

    feedparser uses its own FeedParserDict + time.struct_time which json.dumps
    can't handle natively. The on-disk raw file is for forensics, not for
    parsers — a best-effort string coercion is enough.
    """
    if isinstance(obj, dict):
        return {k: _coerce(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce(v) for v in obj]
    if hasattr(obj, "tm_year"):  # time.struct_time
        return list(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def tick() -> None:
    log = setup_logging(INGESTER_NAME)
    sources = iter_active_sources()
    sources = [s for s in sources if (s.get("feed_type") in ("rss", "atom"))]
    for source in sources:
        if stop_event.is_set():
            return
        if not _due(source):
            continue
        try:
            _poll_source(source, log)
        except Exception:
            log.exception("poll failed for %s", source.get("name"))


def main() -> int:
    load_dotenv_files()
    log = setup_logging(INGESTER_NAME)
    with singleton_lock(INGESTER_NAME):
        log.info("starting news_rss poll loop")
        poll_loop(name=INGESTER_NAME, tick_fn=tick, tick_interval_sec=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
