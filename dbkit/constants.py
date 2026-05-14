"""Constants and shared config for AOTC-Signals.

DB_BASE here defaults to /opt/db-signals so this repo's state/log/lock files
don't collide with AOTC-DB's /opt/db tree. Override via env if you want to
share volumes.
"""
import os
from pathlib import Path

DB_BASE = Path(os.environ.get("DB_BASE", "/opt/db-signals"))
LOG_DIR = DB_BASE / "logs"
LOCK_DIR = DB_BASE / "locks"
STATE_DIR = DB_BASE / "state"
RAW_DIR = DB_BASE / "raw"  # article HTML / API payloads on disk; DB holds pointers

for _d in [LOG_DIR, LOCK_DIR, STATE_DIR, RAW_DIR]:
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def load_dotenv_files(*extra_paths: str) -> dict:
    """Load env vars from .env files. Reads cwd/.env, $DB_BASE/.env, and extras.

    First-wins, then real environment variables take precedence. Bridges
    DATABASE_URL and DB_BASE back into os.environ so pg.py / constants.py
    can see them even if the caller doesn't capture the return value.
    """
    env: dict = {}
    paths = [".env", str(DB_BASE / ".env")]
    paths.extend(extra_paths)
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except OSError:
                pass
    for k in list(env.keys()):
        if k in os.environ:
            env[k] = os.environ[k]
    for key in ("DATABASE_URL", "DB_BASE"):
        if key in env and key not in os.environ:
            os.environ[key] = env[key]
    return env
