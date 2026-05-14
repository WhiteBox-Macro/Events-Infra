"""Backtest configuration — all knobs in one place."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path


@dataclass
class RiskLimits:
    max_positions_per_strategy: int = 5
    max_total_positions: int = 15
    max_exposure_pct: float = 0.50
    max_single_position_pct: float = 0.10


@dataclass
class WalkForwardConfig:
    refit_interval_days: int = 30
    initial_train_days: int = 90
    expanding_window: bool = True
    embargo_hours: int = 24


@dataclass
class BacktestConfig:
    tickers: list[str] = field(default_factory=lambda: ["SPY", "QQQ"])
    parquet_dir: Path = field(default_factory=lambda: Path("events-infra/market-data/1m-parquet"))
    portfolio_notional: float = 100_000.0
    slippage_bps: float = 5.0
    risk: RiskLimits = field(default_factory=RiskLimits)
    walk_forward: WalkForwardConfig = field(default_factory=WalkForwardConfig)
    mtm_interval_bars: int = 60
    output_dir: Path = field(default_factory=lambda: Path("events-infra/backtest/output"))
    start_date: str | None = None  # YYYY-MM-DD, inclusive
    end_date: str | None = None    # YYYY-MM-DD, inclusive
