#!/usr/bin/env python3
"""Dashboard server — HTTP for index.html + WebSocket for replay streaming.

Usage:
    python server.py --tickers SPY QQQ NVDA TSLA --start 2024-10-01 --end 2024-12-31
    # Open http://localhost:8766
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BACKTEST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST_DIR))
sys.path.insert(0, str(REPO_ROOT))

import websockets

from dbkit.constants import load_dotenv_files
from replay_driver import ReplayDriver
from strategies.sonnet_event_strategy import SonnetEventStrategy
from gate_params import default_registry

log = logging.getLogger("dashboard.server")

HTML_PATH = Path(__file__).parent / "index.html"
_driver: ReplayDriver = None


class DashboardHTTPHandler(SimpleHTTPRequestHandler):
    ws_port = 8767

    def address_string(self):
        return self.client_address[0]

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = HTML_PATH.read_text(encoding="utf-8")
            html = html.replace("__WS_PORT__", str(self.ws_port))
            encoded = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def run_http_server(http_port: int, ws_port: int):
    DashboardHTTPHandler.ws_port = ws_port
    httpd = HTTPServer(("localhost", http_port), DashboardHTTPHandler)
    log.info("HTTP server on http://localhost:%d", http_port)
    httpd.serve_forever()


_active_task = None

async def handle_client(websocket):
    global _active_task
    driver = _driver
    log.info("client connected")

    # Cancel any prior replay task (handles reconnection)
    if _active_task and not _active_task.done():
        _active_task.cancel()
        try:
            await _active_task
        except (asyncio.CancelledError, Exception):
            pass

    # Reset driver to start
    driver._state = "paused"
    driver._cursor = 0
    driver._reset_engine()
    # Drain any stale commands
    while not driver._cmd_queue.empty():
        try: driver._cmd_queue.get_nowait()
        except: break

    await websocket.send(json.dumps(driver.get_init_msg(), default=str))

    events_preview = driver.get_events_from_cursor(100)
    await websocket.send(json.dumps({"type": "events_preview", "events": events_preview}, default=str))

    async def send(msg):
        try:
            await websocket.send(json.dumps(msg, default=str))
        except websockets.exceptions.ConnectionClosed:
            raise

    _active_task = asyncio.create_task(driver.run(send))

    try:
        async for message in websocket:
            try:
                cmd = json.loads(message)
            except json.JSONDecodeError:
                continue
            action = cmd.get("cmd")
            log.info("cmd: %s", action)
            if action == "play":
                driver.play()
            elif action == "pause":
                driver.pause()
            elif action == "speed":
                driver.set_speed(cmd.get("value", "1min"))
            elif action == "seek":
                driver.seek_pct(cmd.get("pct", 0))
    except websockets.exceptions.ConnectionClosed:
        log.info("client disconnected")
    finally:
        if _active_task and not _active_task.done():
            _active_task.cancel()


async def run_ws_server(ws_port: int):
    log.info("WebSocket server on ws://localhost:%d", ws_port)
    async with websockets.serve(handle_client, "localhost", ws_port, max_size=10_000_000):
        await asyncio.Future()


def main():
    global _driver

    parser = argparse.ArgumentParser(description="Backtest replay dashboard")
    parser.add_argument("--tickers", nargs="+", default=["SPY", "QQQ"])
    parser.add_argument("--parquet-dir", default=None)
    parser.add_argument("--start", default="2024-10-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--capital", type=float, default=1_000_000, help="Initial portfolio capital")
    parser.add_argument("--mode", choices=["discrete", "rebalance"], default="discrete",
                        help="discrete (original) or rebalance (continuous portfolio)")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    env = load_dotenv_files()
    for k, v in env.items():
        os.environ.setdefault(k, v)

    from config import BacktestConfig

    config = BacktestConfig(tickers=args.tickers, start_date=args.start, end_date=args.end,
                            portfolio_notional=args.capital)
    if args.parquet_dir:
        config.parquet_dir = Path(args.parquet_dir)

    gate_registry = default_registry()
    strategy = SonnetEventStrategy(tickers=args.tickers, cache_only=True,
                                   gate_registry=gate_registry)
    log.info("loaded %d cached classifications, mode=%s, gate_params=%d",
             len(strategy._cache), args.mode, len(gate_registry))
    _driver = ReplayDriver(config, strategies=[strategy],
                           rebalance_mode=(args.mode == "rebalance"))

    ws_port = args.port + 1
    http_thread = threading.Thread(target=run_http_server, args=(args.port, ws_port), daemon=True)
    http_thread.start()

    print(f"\n  Dashboard: http://localhost:{args.port}\n")
    asyncio.run(run_ws_server(ws_port))


if __name__ == "__main__":
    main()
