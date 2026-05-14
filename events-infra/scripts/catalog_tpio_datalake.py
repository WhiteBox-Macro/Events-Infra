#!/usr/bin/env python3
"""Catalog TPIO datalake files into events.raw.

Walks $DB_BASE/events/raw/social/twitter_twitterapiio/ and upserts each
JSON file into events.raw with classify_status='pending'.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import DB_BASE, load_dotenv_files  # noqa: E402

log = logging.getLogger("catalog_tpio")


def parse_tpio_time(created_at: str) -> datetime:
    return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")


def main() -> int:
    env = load_dotenv_files()
    for k, v in env.items():
        os.environ.setdefault(k, v)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    datalake = DB_BASE / "events" / "raw" / "social" / "twitter_twitterapiio"
    raw_root = DB_BASE / "events" / "raw"

    if not datalake.exists():
        log.error("datalake dir not found: %s", datalake)
        return 1

    files = list(datalake.rglob("*.json"))
    log.info("found %d files to catalog", len(files))

    cataloged = 0
    skipped = 0

    for i, fpath in enumerate(files):
        try:
            content = fpath.read_text(encoding="utf-8")
            data = json.loads(content)
            meta = data.get("_meta", {})
            payload = data.get("payload", {})

            external_id = str(meta.get("external_id") or payload.get("id"))
            if not external_id:
                skipped += 1
                continue

            created_at = payload.get("createdAt", "")
            try:
                published_at = parse_tpio_time(created_at)
            except (ValueError, TypeError):
                published_at = None

            file_hash = hashlib.sha256(content.encode()).hexdigest()
            rel_path = str(fpath.relative_to(raw_root))

            metrics = {}
            for k in ("likeCount", "retweetCount", "replyCount", "viewCount", "quoteCount"):
                if k in payload:
                    metrics[k] = payload[k]

            pg.upsert(
                "events.raw",
                {
                    "source_type": "social",
                    "source_channel": "twitter_twitterapiio",
                    "external_id": external_id,
                    "file_path": rel_path,
                    "file_hash": file_hash,
                    "file_size_bytes": len(content.encode()),
                    "published_at": published_at,
                    "metadata": json.dumps({"author": payload.get("author", {}).get("userName"), **metrics}),
                },
                conflict_on=["source_channel", "external_id"],
            )
            cataloged += 1

        except Exception:
            log.exception("failed to catalog %s", fpath)
            skipped += 1

        if (i + 1) % 500 == 0:
            log.info("progress: %d/%d cataloged, %d skipped", cataloged, len(files), skipped)

    log.info("done: %d cataloged, %d skipped out of %d files", cataloged, skipped, len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
