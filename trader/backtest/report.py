#!/usr/bin/env python3
"""trader/backtest/report.py — backtest metrics roll-up.

Reads decisions tagged with `mode='backtest' AND experiment_key=X`, computes
the standard backtest metrics, and:
  1. UPSERTs a row into signals.experiments,
  2. writes a markdown report to $DB_BASE/reports/<experiment_key>.md.

Metrics:
  n_decisions           — how many rows the experiment produced
  n_filled              — how many actually opened a paper position
  n_settled             — how many were settled (raw_return non-null)
  hit_rate              — fraction of settled rows with alpha_return > 0
  avg_alpha             — mean alpha across settled rows
  median_alpha          — median (less sensitive to one big winner)
  alpha_stdev           — population stdev (>= 2 rows)
  sharpe                — avg_alpha / alpha_stdev (no risk-free adjustment;
                          we're scoring per-event alpha not annual returns)
  max_drawdown          — worst cumulative drawdown in alpha if we'd held
                          every signal sequentially in chronological order
  avg_holding_hours     — settled rows only

Breakdowns:
  by_tier               — fast vs slow
  by_kind               — article / post / macro_release (from source_event.kind)
  by_ticker             — alpha by ticker

Plain-Python implementation; no pandas dep needed for what we compute here.

CLI:
    python -m trader.backtest.report --experiment-key earnings_v1
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import DB_BASE, LOG_DIR, load_dotenv_files  # noqa: E402
from psycopg2.extras import Json  # noqa: E402

LOG = logging.getLogger("backtest_report")
REPORT_DIR = DB_BASE / "reports"


def _load_decisions(experiment_key: str) -> list[dict]:
    return pg.execute(
        "SELECT d.decision_id, d.ticker, d.tier, d.rating, d.confidence, "
        "       d.horizon_hours, d.raw_return, d.alpha_return, d.holding_hours, "
        "       d.pending, d.source_event, d.created_at, "
        "       pp.position_id, pp.status, pp.realized_pnl, pp.entry_at "
        "FROM signals.decisions d "
        "LEFT JOIN signals.paper_positions pp ON pp.decision_id = d.decision_id "
        "WHERE d.mode = 'backtest' AND d.experiment_key = %s "
        "ORDER BY d.created_at ASC",
        [experiment_key],
    )


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _max_drawdown(alphas_chronological: list[float]) -> Optional[float]:
    """Max drawdown of the running cumulative-alpha series."""
    if not alphas_chronological:
        return None
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for a in alphas_chronological:
        cumulative += a
        if cumulative > peak:
            peak = cumulative
        dd = cumulative - peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


def compute_metrics(rows: list[dict]) -> dict:
    n_decisions = len(rows)
    n_filled = sum(1 for r in rows if r.get("position_id"))
    settled = [r for r in rows if not r.get("pending") and r.get("alpha_return") is not None]
    n_settled = len(settled)

    alphas = sorted(((_to_float(r["alpha_return"]) or 0.0), r["created_at"]) for r in settled)
    alpha_values = [a for a, _ in alphas]
    chrono_alphas = [
        _to_float(r["alpha_return"]) or 0.0
        for r in sorted(settled, key=lambda r: r["created_at"])
    ]

    hit_rate = sum(1 for a in alpha_values if a > 0) / n_settled if n_settled else None
    avg_alpha = statistics.fmean(alpha_values) if alpha_values else None
    median_alpha = statistics.median(alpha_values) if alpha_values else None
    alpha_stdev = statistics.pstdev(alpha_values) if len(alpha_values) >= 2 else None
    sharpe = (avg_alpha / alpha_stdev) if (avg_alpha is not None and alpha_stdev) else None
    max_dd = _max_drawdown(chrono_alphas)
    avg_hold = statistics.fmean([
        _to_float(r["holding_hours"]) or 0.0 for r in settled if r.get("holding_hours") is not None
    ]) if settled else None

    # Breakdowns
    def _avg(values: list[float]) -> Optional[float]:
        return statistics.fmean(values) if values else None

    by_tier: dict[str, list[float]] = defaultdict(list)
    by_kind: dict[str, list[float]] = defaultdict(list)
    by_ticker: dict[str, list[float]] = defaultdict(list)
    for r in settled:
        a = _to_float(r["alpha_return"]) or 0.0
        by_tier[r.get("tier") or "?"].append(a)
        kind = (r.get("source_event") or {}).get("kind", "?") if isinstance(r.get("source_event"), dict) else "?"
        by_kind[kind].append(a)
        by_ticker[(r.get("ticker") or "?")].append(a)

    return {
        "n_decisions": n_decisions,
        "n_filled": n_filled,
        "n_settled": n_settled,
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "avg_alpha": round(avg_alpha, 5) if avg_alpha is not None else None,
        "median_alpha": round(median_alpha, 5) if median_alpha is not None else None,
        "alpha_stdev": round(alpha_stdev, 5) if alpha_stdev is not None else None,
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "max_drawdown": round(max_dd, 5) if max_dd is not None else None,
        "avg_holding_hours": round(avg_hold, 2) if avg_hold is not None else None,
        "by_tier": {k: {
            "n": len(v),
            "avg_alpha": round(_avg(v), 5) if _avg(v) is not None else None,
            "hit_rate": round(sum(1 for a in v if a > 0) / len(v), 4) if v else None,
        } for k, v in by_tier.items()},
        "by_kind": {k: {
            "n": len(v),
            "avg_alpha": round(_avg(v), 5) if _avg(v) is not None else None,
            "hit_rate": round(sum(1 for a in v if a > 0) / len(v), 4) if v else None,
        } for k, v in by_kind.items()},
        "by_ticker": {k: {
            "n": len(v),
            "avg_alpha": round(_avg(v), 5) if _avg(v) is not None else None,
            "hit_rate": round(sum(1 for a in v if a > 0) / len(v), 4) if v else None,
        } for k, v in sorted(by_ticker.items())},
    }


def _experiment_window(rows: list[dict]) -> tuple[Optional[str], Optional[str], list[str]]:
    if not rows:
        return None, None, []
    starts = [r["created_at"] for r in rows if r.get("created_at")]
    if not starts:
        return None, None, []
    tickers = sorted({r["ticker"] for r in rows if r.get("ticker")})
    return min(starts).date().isoformat(), max(starts).date().isoformat(), tickers


def _upsert_experiment(experiment_key: str, metrics: dict, window: tuple) -> None:
    window_from, window_to, tickers = window
    row = {
        "experiment_key": experiment_key,
        "description": f"backtest replay ({window_from} → {window_to})",
        "agent_hash": "mixed",   # fast_signal_v1 + slow_agent_v1
        "started_at": None,      # let DB default stand on insert; preserved on update
        "finished_at": None,
        "window_from": window_from,
        "window_to": window_to,
        "tickers": tickers,
        "n_decisions": metrics["n_decisions"],
        "hit_rate": metrics["hit_rate"],
        "avg_alpha": metrics["avg_alpha"],
        "sharpe": metrics["sharpe"],
        "max_drawdown": metrics["max_drawdown"],
        "metrics": Json(metrics),
    }
    # Strip Nones for fields we don't want to overwrite on rerun.
    row = {k: v for k, v in row.items() if v is not None or k in {"experiment_key"}}
    # Use raw SQL because pg.upsert overwrites EVERY column; we want to keep
    # started_at from the first run.
    cols = list(row.keys())
    sql_cols = ", ".join(cols)
    placeholders = ", ".join(f"%({c})s" for c in cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "experiment_key")
    sql = (
        f"INSERT INTO signals.experiments ({sql_cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (experiment_key) DO UPDATE SET {updates}, finished_at = NOW()"
    )
    pg.execute(sql, row)


def _render_markdown(experiment_key: str, metrics: dict, window: tuple) -> str:
    window_from, window_to, tickers = window
    lines = [
        f"# Backtest report — `{experiment_key}`",
        "",
        f"- **Window**: {window_from} → {window_to}",
        f"- **Tickers**: {', '.join(tickers) if tickers else '—'}",
        "",
        "## Headline",
        "",
        f"| metric | value |",
        f"|---|---|",
        f"| decisions | {metrics['n_decisions']} |",
        f"| filled    | {metrics['n_filled']} |",
        f"| settled   | {metrics['n_settled']} |",
        f"| hit rate  | {_fmt_pct(metrics['hit_rate'])} |",
        f"| avg α     | {_fmt_pct(metrics['avg_alpha'])} |",
        f"| median α  | {_fmt_pct(metrics['median_alpha'])} |",
        f"| α stdev   | {_fmt_pct(metrics['alpha_stdev'])} |",
        f"| sharpe (per-event) | {metrics['sharpe']} |",
        f"| max drawdown      | {_fmt_pct(metrics['max_drawdown'])} |",
        f"| avg holding hours | {metrics['avg_holding_hours']} |",
        "",
        "## By tier",
        "",
        "| tier | n | avg α | hit rate |",
        "|---|---|---|---|",
    ]
    for tier, m in metrics["by_tier"].items():
        lines.append(f"| {tier} | {m['n']} | {_fmt_pct(m['avg_alpha'])} | {_fmt_pct(m['hit_rate'])} |")
    lines += [
        "",
        "## By event kind",
        "",
        "| kind | n | avg α | hit rate |",
        "|---|---|---|---|",
    ]
    for kind, m in metrics["by_kind"].items():
        lines.append(f"| {kind} | {m['n']} | {_fmt_pct(m['avg_alpha'])} | {_fmt_pct(m['hit_rate'])} |")
    lines += [
        "",
        "## By ticker",
        "",
        "| ticker | n | avg α | hit rate |",
        "|---|---|---|---|",
    ]
    for ticker, m in metrics["by_ticker"].items():
        lines.append(f"| {ticker} | {m['n']} | {_fmt_pct(m['avg_alpha'])} | {_fmt_pct(m['hit_rate'])} |")
    lines += [
        "",
        "## Raw metrics JSON",
        "",
        "```json",
        json.dumps(metrics, indent=2),
        "```",
    ]
    return "\n".join(lines)


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value)*100:+.3f}%"
    except (TypeError, ValueError):
        return "—"


def report(experiment_key: str) -> dict:
    rows = _load_decisions(experiment_key)
    if not rows:
        LOG.warning("no decisions found for experiment_key=%s", experiment_key)
        return {"experiment_key": experiment_key, "n_decisions": 0}

    metrics = compute_metrics(rows)
    window = _experiment_window(rows)

    _upsert_experiment(experiment_key, metrics, window)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{experiment_key}.md"
    out.write_text(_render_markdown(experiment_key, metrics, window), encoding="utf-8")

    LOG.info(
        "report: experiment=%s decisions=%d settled=%d hit=%s avg_alpha=%s sharpe=%s",
        experiment_key, metrics["n_decisions"], metrics["n_settled"],
        _fmt_pct(metrics["hit_rate"]), _fmt_pct(metrics["avg_alpha"]),
        metrics["sharpe"],
    )
    LOG.info("markdown written to %s", out)
    return metrics


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
            LOG_DIR / "backtest_report.log", maxBytes=5_000_000, backupCount=3
        )
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Roll up backtest metrics for one experiment_key")
    ap.add_argument("--experiment-key", required=True)
    args = ap.parse_args()

    _configure_logging()
    load_dotenv_files()
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    report(args.experiment_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
