"""Replay driver — clean state machine with no race conditions.

State: PAUSED → (play) → PLAYING → (pause) → PAUSED
       Any state → (seek) → seeking → PAUSED
       PLAYING → end of timeline → FINISHED

All state transitions go through a command queue consumed by run().
No shared mutable state between tasks.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Awaitable

import numpy as np

import sys
BACKTEST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST_DIR))
sys.path.insert(0, str(BACKTEST_DIR.parent.parent))

from config import BacktestConfig
from tick import BarTick, EventTick
from timeline import TimelineMerger
from engine import StrategyContext
from order_manager import OrderManager
from portfolio_allocator import PortfolioAllocator

log = logging.getLogger("dashboard.replay")

SPEED_MAP = {
    "1min": 60, "5min": 300, "15min": 900, "1hr": 3600,
    "4hr": 14400, "1day": 23400, "max": 0,
}


class ReplayDriver:
    def __init__(self, config: BacktestConfig, strategies: list = None,
                 rebalance_mode: bool = False):
        self.config = config
        self.strategies = strategies or []
        self.rebalance_mode = rebalance_mode

        log.info("loading timeline...")
        self.timeline = TimelineMerger(config)

        log.info("building lightweight index (vectorized)...")
        self._timestamps, self._group_bars, self._group_events, self._event_list = \
            self._build_index_fast(self.timeline)

        log.info("ready: %d groups, %d events", len(self._timestamps), len(self._event_list))

        self._all_events = []
        for ev in self._event_list:
            self._all_events.append({
                "t": int(ev.publish_time.timestamp()),
                "headline": ev.headline,
                "event_type": ev.event_type,
                "tone": ev.tone,
                "is_regular": ev.is_regular,
                "surprise": float(ev.surprise) if ev.surprise is not None else None,
                "tickers": [t["ticker"] for t in ev.ticker_impacts] if ev.ticker_impacts else [],
            })

        self._cmd_queue: asyncio.Queue = asyncio.Queue()
        self._cursor = 0
        self._speed = "1min"
        self._state = "paused"
        self._reset_engine()

    @property
    def _num_groups(self):
        return len(self._timestamps)

    def _get_group(self, idx: int) -> tuple:
        """Reconstruct (timestamp, bars, events) on-the-fly from lightweight index."""
        ts = self._timestamps[idx]
        bars = []
        for ticker, row_idx in self._group_bars[idx]:
            df = self.timeline.bar_dfs[ticker]
            row = df.iloc[row_idx]
            bar_ts = row["timestamp"]
            if hasattr(bar_ts, "to_pydatetime"):
                bar_ts = bar_ts.to_pydatetime()
            if bar_ts.tzinfo is None:
                from datetime import timezone as tz
                bar_ts = bar_ts.replace(tzinfo=tz.utc)
            bars.append(BarTick(
                ticker=ticker, timestamp=bar_ts,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=int(row["volume"]), bar_index=int(row_idx),
            ))
        events = [self._event_list[i] for i in self._group_events[idx]]
        return ts, bars, events

    def _reset_engine(self):
        self.ctx = StrategyContext(self.timeline.bar_dfs, self.config.portfolio_notional)
        if self.rebalance_mode:
            self.allocator = PortfolioAllocator(
                tickers=self.config.tickers,
                notional=self.config.portfolio_notional,
                slippage_bps=self.config.slippage_bps,
            )
            self.order_mgr = None
        else:
            self.order_mgr = OrderManager(
                slippage_bps=self.config.slippage_bps,
                portfolio_notional=self.config.portfolio_notional,
            )
            self.allocator = None
        self._last_bar: dict[str, BarTick] = {}
        self._wf_sim_start: datetime | None = self._timestamps[0] if self._timestamps else None
        self._wf_last_refit: datetime | None = None
        self._wf_embargo_until: datetime | None = None
        for strat in self.strategies:
            if hasattr(strat, 'reset'):
                strat.reset()

    @staticmethod
    def _build_index_fast(timeline: TimelineMerger):
        """Build lightweight index via numpy vectorized merge instead of Python iteration."""
        import time as _time
        t0 = _time.monotonic()

        ticker_list = sorted(timeline.bar_dfs.keys())
        ticker_to_id = {t: i for i, t in enumerate(ticker_list)}

        chunks = []
        for ticker in ticker_list:
            df = timeline.bar_dfs[ticker]
            n = len(df)
            if n == 0:
                continue
            ts_ns = df["timestamp"].values.astype("int64")
            ticker_ids = np.full(n, ticker_to_id[ticker], dtype=np.int32)
            row_indices = np.arange(n, dtype=np.int32)
            priority = np.zeros(n, dtype=np.int8)
            event_idx = np.full(n, -1, dtype=np.int32)
            chunks.append(np.column_stack([
                ts_ns.view(np.int64),
                priority.astype(np.int64),
                ticker_ids.astype(np.int64),
                row_indices.astype(np.int64),
                event_idx.astype(np.int64),
            ]))

        event_list = []
        for i, ev in enumerate(timeline.events):
            # Delegate to the timeline's _make_event_tick so all callers
            # produce the same shape (one place to maintain when EventTick
            # changes). The replay_driver historically rolled its own; that
            # was a duplicate that drifted from timeline's logic.
            tick = timeline._make_event_tick(ev)
            event_list.append(tick)
            ts_ns = np.int64(int(tick.publish_time.timestamp() * 1e9))
            chunks.append(np.array([[ts_ns, 1, -1, -1, i]], dtype=np.int64))

        all_data = np.concatenate(chunks, axis=0)
        order = np.lexsort((all_data[:, 2], all_data[:, 1], all_data[:, 0]))
        all_data = all_data[order]

        ts_col = all_data[:, 0]
        breaks = np.where(np.diff(ts_col) != 0)[0] + 1
        groups = np.split(np.arange(len(all_data)), breaks)

        timestamps = []
        group_bars = []
        group_events = []

        for grp_idx in groups:
            rows = all_data[grp_idx]
            ts_ns_val = rows[0, 0]
            ts_dt = datetime.fromtimestamp(ts_ns_val / 1e9, tz=timezone.utc)
            timestamps.append(ts_dt)

            bars = []
            evts = []
            for row in rows:
                if row[1] == 0:
                    bars.append((ticker_list[row[2]], int(row[3])))
                else:
                    evts.append(int(row[4]))
            group_bars.append(bars)
            group_events.append(evts)

        elapsed = _time.monotonic() - t0
        log.info("vectorized index: %d groups in %.2fs (%.0f groups/s)",
                 len(timestamps), elapsed, len(timestamps) / elapsed if elapsed > 0 else 0)

        return timestamps, group_bars, group_events, event_list

    def _maybe_refit(self, ts: datetime) -> dict | None:
        if not self.strategies or not self._wf_sim_start:
            return None
        wf = self.config.walk_forward
        elapsed_days = (ts - self._wf_sim_start).total_seconds() / 86400
        if elapsed_days < wf.initial_train_days:
            return None
        if self._wf_last_refit is not None:
            days_since = (ts - self._wf_last_refit).total_seconds() / 86400
            if days_since < wf.refit_interval_days:
                return None
        self._wf_last_refit = ts
        self._wf_embargo_until = ts + timedelta(hours=wf.embargo_hours)
        for strat in self.strategies:
            strat.refit(self._wf_sim_start, ts, self.ctx)
        return {
            "type": "refit",
            "t": int(ts.timestamp()),
            "train_start": self._wf_sim_start.isoformat(),
            "train_end": ts.isoformat(),
            "embargo_until": self._wf_embargo_until.isoformat(),
        }

    @property
    def progress_pct(self) -> float:
        return (self._cursor / self._num_groups * 100) if self._timestamps else 0

    # ── Commands (called from listener task, safe via queue) ──

    def play(self): self._cmd_queue.put_nowait(("play",))
    def pause(self): self._cmd_queue.put_nowait(("pause",))
    def set_speed(self, s: str): self._cmd_queue.put_nowait(("speed", s))
    def seek_pct(self, pct: float): self._cmd_queue.put_nowait(("seek", pct))

    def _drain_commands(self) -> list[tuple]:
        cmds = []
        while not self._cmd_queue.empty():
            try:
                cmds.append(self._cmd_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return cmds

    # ── Init messages ──

    def get_init_msg(self) -> dict:
        first_ts = self._timestamps[0] if self._timestamps else None
        last_ts = self._timestamps[-1] if self._timestamps else None
        return {
            "type": "init",
            "tickers": self.config.tickers,
            "start": first_ts.isoformat() if first_ts else None,
            "end": last_ts.isoformat() if last_ts else None,
            "total_groups": self._num_groups,
            "total_events": len(self._all_events),
            "portfolio_notional": self.config.portfolio_notional,
            "mode": "rebalance" if self.rebalance_mode else "discrete",
        }

    def get_events_from_cursor(self, n: int = 100) -> list[dict]:
        result = []
        count = 0
        for i in range(self._cursor, self._num_groups):
            if count >= n:
                break
            for ei in self._group_events[i]:
                t = self._event_list[ei]
                if True:
                    result.append({
                        "t": int(t.publish_time.timestamp()),
                        "headline": t.headline,
                        "event_type": t.event_type,
                        "tone": t.tone,
                        "is_regular": t.is_regular,
                        "surprise": float(t.surprise) if t.surprise is not None else None,
                        "tickers": [x["ticker"] for x in t.ticker_impacts] if t.ticker_impacts else [],
                    })
                    count += 1
        return result

    # ── Main run loop (single coroutine, no concurrency issues) ──

    async def run(self, send: Callable[[dict], Awaitable]):
        batch_buf = []
        prev_sim_ts = None
        last_flush_ts = None

        while True:
            # Process any pending commands
            for cmd in self._drain_commands():
                if cmd[0] == "play":
                    self._state = "playing"
                    log.info("→ playing")
                elif cmd[0] == "pause":
                    self._state = "paused"
                    log.info("→ paused")
                elif cmd[0] == "speed":
                    self._speed = cmd[1] if cmd[1] in SPEED_MAP else self._speed
                    log.info("→ speed %s", self._speed)
                elif cmd[0] == "seek":
                    pct = cmd[1]
                    target = int(self._num_groups * max(0, min(100, pct)) / 100)
                    log.info("→ seek to %d (%.1f%%)", target, pct)
                    self._reset_engine()
                    # Fast-forward silently
                    for i in range(min(target, self._num_groups)):
                        ts, bars, events = self._get_group(i)
                        self._ff_group(ts, bars, events)
                    self._cursor = min(target, self._num_groups)
                    self._state = "paused"
                    batch_buf = []
                    last_flush_ts = None
                    prev_sim_ts = self._timestamps[self._cursor] if self._cursor < self._num_groups else None
                    # Send state reset + upcoming events
                    await send({"type": "seek_done",
                                "progress_pct": round(self.progress_pct, 1),
                                "sim_time": prev_sim_ts.isoformat() if prev_sim_ts else None})
                    events = self.get_events_from_cursor(100)
                    await send({"type": "events_preview", "events": events})
                    log.info("seek done at cursor %d", self._cursor)

            # Finished?
            if self._cursor >= self._num_groups:
                await send({"type": "playback", "state": "finished",
                            "sim_time": prev_sim_ts.isoformat() if prev_sim_ts else None,
                            "progress_pct": 100.0})
                # Wait for commands (seek to restart)
                await asyncio.sleep(0.5)
                continue

            # Paused — wait for commands
            if self._state == "paused":
                await send({"type": "playback", "state": "paused",
                            "sim_time": prev_sim_ts.isoformat() if prev_sim_ts else None,
                            "progress_pct": round(self.progress_pct, 1)})
                # Poll for commands every 100ms (no race-prone Event)
                while self._state == "paused" and self._cmd_queue.empty():
                    await asyncio.sleep(0.1)
                continue

            # Playing — process next group
            ts, bars, events = self._get_group(self._cursor)
            self._cursor += 1
            if self.rebalance_mode:
                msgs = self._process_group_rebal(ts, bars, events)
            else:
                msgs = self._process_group(ts, bars, events)

            speed_secs = SPEED_MAP.get(self._speed, 60)
            has_important = any(m["type"] in ("event", "fill", "exit", "rebal", "allocation") for m in msgs)

            if speed_secs == 0:  # MAX
                batch_buf.extend(msgs)
                if has_important or last_flush_ts is None or (ts - last_flush_ts).total_seconds() >= 300:
                    if batch_buf:
                        await send({"type": "batch", "msgs": batch_buf})
                        batch_buf = []
                    last_flush_ts = ts
                    await asyncio.sleep(0.02)
            elif speed_secs >= 3600:
                batch_buf.extend(msgs)
                if has_important or last_flush_ts is None or (ts - last_flush_ts).total_seconds() >= 60:
                    if batch_buf:
                        await send({"type": "batch", "msgs": batch_buf})
                        batch_buf = []
                    last_flush_ts = ts
                    if prev_sim_ts:
                        gap = (ts - prev_sim_ts).total_seconds()
                        await asyncio.sleep(min(gap / speed_secs, 0.3))
            else:
                if msgs:
                    await send({"type": "batch", "msgs": msgs})
                if prev_sim_ts:
                    gap = (ts - prev_sim_ts).total_seconds()
                    delay = gap / speed_secs
                    if delay > 0.005:
                        await asyncio.sleep(min(delay, 2.0))

            prev_sim_ts = ts

            if self._cursor % 500 == 0:
                await send({"type": "playback", "state": "playing", "speed": self._speed,
                            "sim_time": ts.isoformat(), "progress_pct": round(self.progress_pct, 1)})

    def _ff_group(self, ts, bars, events):
        """Fast-forward one group (no messages, no sleep)."""
        self.ctx.set_time(ts)
        self._maybe_refit(ts)

        if self.rebalance_mode:
            for bar in bars:
                self.ctx.advance_cursor(bar.ticker, bar.bar_index, bar.close)
                self._last_bar[bar.ticker] = bar
            if not self.allocator.initialized and all(t in self._last_bar for t in self.allocator.tickers):
                all_prices = {t: self._last_bar[t].close for t in self.allocator.tickers
                              if t in self._last_bar}
                self.allocator.initialize_positions(all_prices, ts)
            self.ctx.set_positions({})
            for bar in bars:
                for strat in self.strategies:
                    strat.on_bar(bar, self.ctx)
            if self.allocator.initialized:
                self.allocator.decay_tilts(dt_bars=1)
                self.allocator.mark_to_market(
                    {t: self._last_bar[t].close for t in self.allocator.tickers if t in self._last_bar})
            for event in events:
                for strat in self.strategies:
                    if hasattr(strat, 'compute_tilts'):
                        tilts = strat.compute_tilts(event, self.ctx)
                        if tilts and self.allocator.initialized:
                            self.allocator.apply_event_tilts(tilts)
            if self.allocator.initialized and events:
                current_prices = {t: self._last_bar[t].close for t in self.allocator.tickers
                                  if t in self._last_bar}
                rebal_orders = self.allocator.get_rebal_orders(current_prices)
                if rebal_orders:
                    self.allocator.execute_rebal(rebal_orders, current_prices, ts)
        else:
            for bar in bars:
                self.order_mgr.process_bar(bar, ts)
                self.order_mgr.mark_positions(bar)
                self.ctx.advance_cursor(bar.ticker, bar.bar_index, bar.close)
                self._last_bar[bar.ticker] = bar
            self.ctx.set_positions(self.order_mgr.positions)
            for bar in bars:
                for strat in self.strategies:
                    strat.on_bar(bar, self.ctx)
            for event in events:
                for strat in self.strategies:
                    strat.on_event(event, self.ctx)

    def _process_group_rebal(self, ts: datetime, bars: list, events: list) -> list[dict]:
        self.ctx.set_time(ts)
        msgs = []

        refit_msg = self._maybe_refit(ts)
        if refit_msg:
            msgs.append(refit_msg)

        in_embargo = self._wf_embargo_until and ts < self._wf_embargo_until

        prices = {}
        for bar in bars:
            self.ctx.advance_cursor(bar.ticker, bar.bar_index, bar.close)
            self._last_bar[bar.ticker] = bar
            prices[bar.ticker] = bar.close
            msgs.append({"type": "bar", "ticker": bar.ticker, "t": int(ts.timestamp()),
                         "o": round(bar.open, 2), "h": round(bar.high, 2),
                         "l": round(bar.low, 2), "c": round(bar.close, 2), "v": bar.volume})

        if not self.allocator.initialized and all(t in self._last_bar for t in self.allocator.tickers):
            all_prices = {t: self._last_bar[t].close for t in self.allocator.tickers
                          if t in self._last_bar}
            init_orders = self.allocator.initialize_positions(all_prices, ts)
            for o in init_orders:
                msgs.append({"type": "rebal", "ticker": o.ticker, "side": o.side,
                             "qty": round(o.qty, 4), "price": round(all_prices.get(o.ticker, 0), 2),
                             "target_w": round(o.target_weight, 4), "current_w": 0.0,
                             "reason": "initial_alloc", "t": int(ts.timestamp())})

        self.ctx.set_positions({})
        for bar in bars:
            for strat in self.strategies:
                strat.on_bar(bar, self.ctx)

        if self.allocator.initialized:
            self.allocator.decay_tilts(dt_bars=1)
            self.allocator.mark_to_market(
                {t: self._last_bar[t].close for t in self.allocator.tickers if t in self._last_bar})

        for event in events:
            msgs.append({"type": "event", "id": event.event_id,
                         "t": int(event.publish_time.timestamp()),
                         "headline": event.headline, "event_type": event.event_type,
                         "tone": event.tone, "magnitude": event.magnitude,
                         "tickers": [x["ticker"] for x in event.ticker_impacts] if event.ticker_impacts else [],
                         "surprise": float(event.surprise) if event.surprise is not None else None,
                         "is_regular": event.is_regular})
            for strat in self.strategies:
                if hasattr(strat, 'compute_tilts'):
                    tilts = strat.compute_tilts(event, self.ctx)
                    if not in_embargo and tilts and self.allocator.initialized:
                        self.allocator.apply_event_tilts(tilts)

                if hasattr(strat, 'last_decisions') and strat.last_decisions:
                    decs = strat.last_decisions
                    first = decs[0]
                    ticker_evals = []
                    for dec in decs:
                        d = {**dec}
                        if in_embargo and d.get("tilt", 0) != 0:
                            d["tilt"] = 0.0
                            d["reason"] = "embargo active (post-refit)"
                        ticker_evals.append({
                            "ticker": d.get("ticker", ""),
                            "weight": d.get("weight", 0.5),
                            "impact_stats": d.get("impact_stats"),
                            "decision": d.get("decision"),
                            "tilt": d.get("tilt", 0.0),
                            "confidence": d.get("confidence", 0),
                            "reason": d.get("reason", ""),
                        })
                    msgs.append({
                        "type": "decision", "event_id": first.get("event_id"),
                        "headline": first.get("headline", ""),
                        "classification": first.get("classification"),
                        "tickers": ticker_evals, "best_side": None,
                        "t": int(ts.timestamp()),
                    })

        if self.allocator.initialized and events:
            current_prices = {t: self._last_bar[t].close for t in self.allocator.tickers
                              if t in self._last_bar}
            rebal_orders = self.allocator.get_rebal_orders(current_prices)
            if rebal_orders:
                fills = self.allocator.execute_rebal(rebal_orders, current_prices, ts)
                for f in fills:
                    msgs.append({"type": "rebal", "ticker": f["ticker"], "side": f["side"],
                                 "qty": f["qty"], "price": f["price"],
                                 "target_w": f["target_w"], "current_w": f["current_w"],
                                 "reason": f["reason"], "t": int(ts.timestamp())})

        has_rebal = any(m.get("type") == "rebal" for m in msgs)
        if (has_rebal or self._cursor % 30 == 0) and self.allocator.initialized:
            msgs.append({
                "type": "allocation",
                "weights": {t: round(w, 4) for t, w in self.allocator.get_weights().items()},
                "targets": {t: round(w, 4) for t, w in self.allocator.get_target_weights().items()},
                "tilts": {t: round(v, 6) for t, v in self.allocator.get_tilts().items()},
                "value": round(self.allocator.nav, 2),
                "cash": round(self.allocator.cash, 2),
                "t": int(ts.timestamp()),
            })

        return msgs

    def _process_group(self, ts: datetime, bars: list, events: list) -> list[dict]:
        self.ctx.set_time(ts)
        msgs = []

        refit_msg = self._maybe_refit(ts)
        if refit_msg:
            msgs.append(refit_msg)

        in_embargo = self._wf_embargo_until and ts < self._wf_embargo_until

        for bar in bars:
            exit_fills = self.order_mgr.process_bar(bar, ts)
            for fill in exit_fills:
                for cp in self.order_mgr.closed_positions:
                    if cp.ticker == fill.ticker and cp.exit_time == ts:
                        msgs.append({"type": "exit", "ticker": cp.ticker, "side": fill.side,
                                     "price": round(cp.exit_price, 2),
                                     "pnl_pct": round(cp.realized_pnl * 100, 4) if cp.realized_pnl else 0,
                                     "t": int(ts.timestamp())})
                        for strat in self.strategies:
                            if hasattr(strat, "record_exit"):
                                strat.record_exit(cp.ticker, cp.realized_pnl, ts)
                        break

        for bar in bars:
            self.order_mgr.mark_positions(bar)
            self.ctx.advance_cursor(bar.ticker, bar.bar_index, bar.close)
            self._last_bar[bar.ticker] = bar
            msgs.append({"type": "bar", "ticker": bar.ticker, "t": int(ts.timestamp()),
                         "o": round(bar.open, 2), "h": round(bar.high, 2),
                         "l": round(bar.low, 2), "c": round(bar.close, 2), "v": bar.volume})

        self.ctx.set_positions(self.order_mgr.positions)
        for bar in bars:
            for strat in self.strategies:
                strat.on_bar(bar, self.ctx)

        for event in events:
            msgs.append({"type": "event", "id": event.event_id, "t": int(event.publish_time.timestamp()),
                         "headline": event.headline, "event_type": event.event_type,
                         "tone": event.tone, "magnitude": event.magnitude,
                         "tickers": [x["ticker"] for x in event.ticker_impacts] if event.ticker_impacts else [],
                         "surprise": float(event.surprise) if event.surprise is not None else None,
                         "is_regular": event.is_regular})
            for strat in self.strategies:
                orders = strat.on_event(event, self.ctx)
                if in_embargo:
                    orders = []
                for order in orders:
                    lb = self._last_bar.get(order.ticker)
                    if lb:
                        fill = self.order_mgr.fill_at_close(order, lb, ts)
                        if fill:
                            self.order_mgr.schedule_exit(order.strategy, order.ticker, 15)
                            msgs.append({"type": "fill", "strategy": order.strategy,
                                         "ticker": order.ticker, "side": order.side,
                                         "qty": round(fill.qty, 4), "price": round(fill.price, 2),
                                         "reason": order.reason, "t": int(ts.timestamp())})
                if hasattr(strat, 'last_decisions') and strat.last_decisions:
                    decs = strat.last_decisions
                    first = decs[0]
                    ticker_evals = []
                    best_side = None
                    for dec in decs:
                        d = {**dec}
                        if in_embargo and d.get("decision") is not None:
                            d["decision"] = None
                            d["reason"] = "embargo active (post-refit)"
                        if d.get("decision") is not None:
                            best_side = d["decision"]
                        ticker_evals.append({
                            "ticker": d.get("ticker", ""),
                            "weight": d.get("weight", 0.5),
                            "impact_stats": d.get("impact_stats"),
                            "decision": d.get("decision"),
                            "confidence": d.get("confidence", 0),
                            "reason": d.get("reason", ""),
                        })
                    msgs.append({
                        "type": "decision",
                        "event_id": first.get("event_id"),
                        "headline": first.get("headline", ""),
                        "classification": first.get("classification"),
                        "tickers": ticker_evals,
                        "best_side": best_side,
                        "t": int(ts.timestamp()),
                    })

        has_trade = any(m["type"] in ("fill", "exit") for m in msgs)
        if has_trade or self._cursor % 30 == 0:
            positions = []
            for pos in self.order_mgr.positions.values():
                positions.append({"ticker": pos.ticker, "side": pos.side, "qty": round(pos.qty, 4),
                                  "entry": round(pos.entry_price, 2),
                                  "pnl_pct": round(pos.unrealized_pnl * 100, 4)})
            total_pnl = sum(p.unrealized_pnl * p.qty * p.entry_price
                           for p in self.order_mgr.positions.values())
            msgs.append({"type": "portfolio", "t": int(ts.timestamp()),
                         "value": round(self.config.portfolio_notional + total_pnl, 2),
                         "positions": positions})

        return msgs
