#!/usr/bin/env python3
"""CLI entry point for the parallel event classifier."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dbkit.constants import load_dotenv_files  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify raw events into events.classified")
    parser.add_argument("--workers", type=int, default=6, help="Number of parallel workers (default: 6)")
    parser.add_argument("--retry-failed", action="store_true", help="Reset failed/stuck rows to pending")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    env = load_dotenv_files()
    for k, v in env.items():
        os.environ.setdefault(k, v)

    from classify import run_parallel  # noqa: E402

    stats = run_parallel(num_workers=args.workers, retry_failed=args.retry_failed)
    print(f"\nDone: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
