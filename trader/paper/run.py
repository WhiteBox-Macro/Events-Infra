#!/usr/bin/env python3
"""trader/paper/run.py — long-running paper-jobs scheduler.

Runs four tasks on their own cadences inside one process:

    mtm        — every MTM_INTERVAL_SEC (default 60s)
    supersede  — every SUPERSEDE_INTERVAL_SEC (default 60s)
    settle     — every SETTLE_INTERVAL_SEC (default 300s)
    reflect    — every REFLECT_INTERVAL_SEC (default 600s)

Single-instance via FileLock, same idiom as scripts/ingest/*. Each tick is
independent; a transient failure in one task doesn't block the others.

Also runnable as one-shot:

    python -m trader.paper.run --once mtm
    python -m trader.paper.run --once settle
    python -m trader.paper.run --once supersede
    python -m trader.paper.run --once reflect

This makes the same code addressable from cron/systemd timers if you prefer
stateless invocations to one long-running process.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit.constants import LOCK_DIR, LOG_DIR, load_dotenv_files  # noqa: E402
from dbkit.filelock import FileLock  # noqa: E402
from dbkit.shutdown import register_shutdown  # noqa: E402

from trader import reflect as reflect_mod  # noqa: E402
from trader.paper import mtm, settle, supersede  # noqa: E402

JOB_NAME = "paper_jobs"
LOG = logging.getLogger(JOB_NAME)


@dataclass
class Task:
    name: str
    fn: Callable[[], dict]
    interval_sec: float
    last_run: float = 0.0


class _Stop:
    def __init__(self) -> None:
        self.flag = False

    def signal(self, *_args) -> None:
        self.flag = True


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


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
            LOG_DIR / f"{JOB_NAME}.log", maxBytes=5_000_000, backupCount=3
        )
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError:
        pass
    return LOG


def _build_tasks() -> list[Task]:
    return [
        Task("mtm",       lambda: mtm.run_once(),        _env_float("MTM_INTERVAL_SEC",       60.0)),
        Task("supersede", lambda: supersede.run_once(),  _env_float("SUPERSEDE_INTERVAL_SEC", 60.0)),
        Task("settle",    lambda: settle.run_once(),     _env_float("SETTLE_INTERVAL_SEC",   300.0)),
        Task("reflect",   lambda: reflect_mod.run_once(),_env_float("REFLECT_INTERVAL_SEC", 600.0)),
    ]


def _run_task(task: Task) -> None:
    t0 = time.monotonic()
    try:
        result = task.fn() or {}
    except Exception:
        LOG.exception("task %s crashed", task.name)
        return
    elapsed = time.monotonic() - t0
    LOG.info("task %s done in %.2fs result=%s", task.name, elapsed, result)


def _one_shot(name: str) -> int:
    load_dotenv_files()
    _configure_logging()
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2
    tasks = {t.name: t for t in _build_tasks()}
    if name not in tasks:
        print(f"ERROR: unknown task '{name}'. Choose from {sorted(tasks)}", file=sys.stderr)
        return 2
    LOG.info("one-shot: %s", name)
    _run_task(tasks[name])
    return 0


def _loop() -> int:
    load_dotenv_files()
    _configure_logging()
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    stop = _Stop()
    register_shutdown(stop.signal)

    lock_path = LOCK_DIR / f"{JOB_NAME}.lock"
    try:
        lock = FileLock(lock_path, blocking=False)
        lock.__enter__()
    except BlockingIOError:
        print(f"ERROR: another '{JOB_NAME}' is running (lock {lock_path}). Refusing to start.",
              file=sys.stderr)
        return 3

    tasks = _build_tasks()
    LOG.info(
        "paper-jobs runner up; cadences: %s",
        ", ".join(f"{t.name}={t.interval_sec}s" for t in tasks),
    )
    try:
        while not stop.flag:
            now = time.monotonic()
            for task in tasks:
                if stop.flag:
                    break
                if now - task.last_run >= task.interval_sec:
                    _run_task(task)
                    task.last_run = time.monotonic()
            # Sleep in small slices so SIGTERM is acted on within ~1s. Use the
            # smallest interval as the sleep step so we wake up roughly when
            # any task is due.
            step = min(1.0, min(t.interval_sec for t in tasks))
            slept = 0.0
            while slept < step and not stop.flag:
                time.sleep(0.25)
                slept += 0.25
    finally:
        try:
            lock.__exit__(None, None, None)
        except Exception:
            pass
        LOG.info("paper-jobs runner exited cleanly")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Paper-trade scheduler: MTM / supersede / settle / reflect")
    ap.add_argument("--once", choices=["mtm", "supersede", "settle", "reflect"],
                    help="Run one task and exit (for cron/systemd timers).")
    args = ap.parse_args()
    if args.once:
        return _one_shot(args.once)
    return _loop()


if __name__ == "__main__":
    sys.exit(main())
