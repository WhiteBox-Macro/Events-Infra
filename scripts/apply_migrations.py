#!/usr/bin/env python3
"""apply_migrations.py — apply pending SQL migrations from db/migrations/.

State is tracked in signals_meta.applied_migrations (created by 001_meta.sql).
Migrations run in lexical filename order. Each migration runs inside its
own transaction; on failure, the migration rolls back and the runner aborts.

Bootstrap: signals_meta.applied_migrations is created by 001_meta.sql
itself. The runner pre-checks existence before SELECTing from it.

Usage:
    python scripts/apply_migrations.py              # apply all pending
    python scripts/apply_migrations.py --dry-run    # show what would run
    python scripts/apply_migrations.py --status     # list applied + pending
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg
from dbkit.constants import load_dotenv_files

MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


def _ledger_exists() -> bool:
    rows = pg.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'signals_meta' AND table_name = 'applied_migrations'"
    )
    return bool(rows)


def _applied_set() -> set[str]:
    if not _ledger_exists():
        return set()
    rows = pg.execute("SELECT filename FROM signals_meta.applied_migrations")
    return {r["filename"] for r in rows}


def _discover_migrations() -> list[Path]:
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(p for p in MIGRATIONS_DIR.iterdir() if p.suffix == ".sql")


def _apply_one(path: Path) -> None:
    """Apply one migration. Migration SQL + ledger insert run in one transaction.

    Bootstrap quirk: 001_meta.sql creates signals_meta.applied_migrations
    itself, then we insert into it in the same transaction. Works because
    CREATE TABLE + INSERT in one transaction is fine in Postgres.
    """
    sql = path.read_text()
    checksum = hashlib.sha256(sql.encode()).hexdigest()[:16]
    applied_by = f"{getpass.getuser()}@{os.uname().nodename}" if hasattr(os, "uname") else getpass.getuser()

    with pg.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO signals_meta.applied_migrations "
                "(filename, checksum, applied_by) VALUES (%s, %s, %s) "
                "ON CONFLICT (filename) DO NOTHING",
                [path.name, checksum, applied_by],
            )


def cmd_status() -> int:
    applied = _applied_set()
    files = _discover_migrations()
    if not files:
        print(f"No migration files in {MIGRATIONS_DIR}")
        return 0
    print(f"{'STATE':<10} {'FILENAME'}")
    for p in files:
        state = "applied" if p.name in applied else "pending"
        print(f"{state:<10} {p.name}")
    return 0


def cmd_apply(dry_run: bool) -> int:
    files = _discover_migrations()
    if not files:
        print(f"No migration files in {MIGRATIONS_DIR}")
        return 0
    applied = _applied_set()
    pending = [p for p in files if p.name not in applied]
    if not pending:
        print(f"Up to date — {len(applied)} migration(s) already applied.")
        return 0

    print(f"Found {len(pending)} pending migration(s):")
    for p in pending:
        print(f"  {p.name}")

    if dry_run:
        print("\n--dry-run: not applying.")
        return 0

    for p in pending:
        print(f"\nApplying {p.name} ...")
        try:
            _apply_one(p)
        except Exception as e:
            print(f"  FAILED: {e}")
            return 1
        print(f"  ok")
    print(f"\nApplied {len(pending)} migration(s).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    load_dotenv_files()
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set. Copy .env.example to .env and edit.", file=sys.stderr)
        return 2

    if args.status:
        return cmd_status()
    return cmd_apply(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
