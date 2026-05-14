"""Cross-platform signal handling.

Renamed from AOTC-DB's `dbkit/signals.py` to avoid name collision with the
`signals` schema concept in this repo.
"""
import signal
import sys


def register_shutdown(handler):
    """Register a graceful shutdown handler that works on Linux and Windows.

    Linux: catches SIGTERM (systemd stop) and SIGINT (Ctrl+C).
    Windows: catches SIGINT (Ctrl+C) and SIGBREAK (NSSM stop).
    """
    signal.signal(signal.SIGINT, handler)
    if sys.platform != 'win32':
        signal.signal(signal.SIGTERM, handler)
    else:
        signal.signal(signal.SIGBREAK, handler)


def kill_process(proc):
    """Terminate a subprocess cross-platform."""
    proc.terminate()
