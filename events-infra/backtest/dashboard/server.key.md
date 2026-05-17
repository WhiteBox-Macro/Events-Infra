# server.py

**Purpose:** Dashboard entry point. Runs an HTTP server for index.html and a WebSocket server for live replay streaming. Wires together config, strategy, and ReplayDriver.

## Architecture

- HTTP thread serves index.html on port (default 8766), injecting WS port dynamically
- WebSocket server (port+1) handles one client at a time via `handle_client`
- On client connect: resets driver to start, sends init msg + events preview
- Listens for commands: play, pause, speed, seek -- forwarded to ReplayDriver

## Key Classes/Functions

- **`DashboardHTTPHandler`** — serves index.html with `__WS_PORT__` replaced; 404 for everything else
- **`run_http_server(http_port, ws_port)`** — starts HTTPServer in a daemon thread
- **`handle_client(websocket)`** — WebSocket handler: manages driver lifecycle, cancels prior replay on reconnect, routes commands
- **`run_ws_server(ws_port)`** — async websockets.serve wrapper
- **`main()`** — CLI entry point with argparse (tickers, parquet-dir, start, end, capital, port, verbose)

## Inputs/Outputs

- **CLI args:** --tickers, --parquet-dir, --start, --end, --capital, --port, -v
- **Serves:** HTTP on localhost:port, WS on localhost:port+1
- **Dependencies:** ReplayDriver, SonnetEventStrategy (cache_only=True), BacktestConfig, dbkit.constants

## Gotchas

- Only one active replay task at a time; reconnection cancels the previous one
- Strategy runs in cache_only mode (no live LLM calls)
- `_driver` is a module-level global shared between HTTP and WS threads
- max_size=10MB on WebSocket to handle large batch messages
