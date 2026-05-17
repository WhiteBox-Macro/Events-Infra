#!/usr/bin/env python3
"""Apply pending migrations under events-infra/db/migrations/.

Tracks applied versions in public.schema_migrations (version, applied_at,
checksum). On re-run: skips already-applied versions whose checksum matches;
refuses to proceed if a previously-applied file has been edited (checksum
drift) -- forces a human decision.

Usage:
    python apply_migrations.py [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import load_dotenv_files  # noqa: E402

log = logging.getLogger("apply_migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum    TEXT        NOT NULL
)
"""


def file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_tracking_table() -> None:
    pg.execute(SCHEMA_MIGRATIONS_DDL)


def list_applied() -> dict[str, str]:
    rows = pg.execute("SELECT version, checksum FROM public.schema_migrations")
    return {r["version"]: r["checksum"] for r in rows}


def list_migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_one(path: Path, checksum: str) -> None:
    """Execute the migration's SQL (which owns its own BEGIN/COMMIT), then
    INSERT a tracking row in a separate statement.

    These are intentionally NOT wrapped in pg.transaction(): the .sql file
    contains its own COMMIT;, and an outer transaction would either get
    terminated mid-block by that COMMIT or (worse) split the DDL and the
    tracking-row INSERT across two transactions silently. Keeping them
    explicitly separate makes the failure mode loud and easy to recover
    from.
    """
    sql = path.read_text(encoding="utf-8")
    log.info("applying %s (%d bytes)", path.name, len(sql))

    # 1. Run the migration. The .sql file wraps itself in BEGIN/COMMIT;
    #    use a raw cursor with autocommit-aware semantics.
    conn, pool_ref = pg.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pg.put_conn(conn, pool_ref)

    # 2. Record success in a separate, simple statement. If THIS fails,
    #    the migration has already landed in PG — log loudly so the user
    #    knows to manually INSERT the tracking row before re-running.
    try:
        pg.execute(
            "INSERT INTO public.schema_migrations (version, checksum) VALUES (%s, %s)",
            [path.name, checksum],
        )
    except Exception as e:
        log.error("MIGRATION APPLIED BUT TRACKING INSERT FAILED for %s: %s", path.name, e)
        log.error("manually record with: "
                  "INSERT INTO public.schema_migrations (version, checksum) "
                  "VALUES ('%s', '%s');", path.name, checksum)
        raise


def baseline(versions: list[str]) -> int:
    """Record the given migration versions as already-applied without
    executing the SQL. Use when a schema predates the tracker (e.g. tables
    created by init-db.sh)."""
    ensure_tracking_table()
    applied = list_applied()
    recorded = 0
    for v in versions:
        path = MIGRATIONS_DIR / v
        if not path.exists():
            log.error("baseline: %s not found in %s", v, MIGRATIONS_DIR)
            return 1
        if v in applied:
            log.info("baseline: %s already recorded; skipping", v)
            continue
        cs = file_checksum(path)
        pg.execute(
            "INSERT INTO public.schema_migrations (version, checksum) VALUES (%s, %s)",
            [v, cs],
        )
        log.info("baseline: recorded %s as applied (checksum=%s)", v, cs[:12])
        recorded += 1
    log.info("baseline done: %d new record(s)", recorded)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply pending DB migrations.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List pending migrations without applying.")
    ap.add_argument("--baseline", nargs="+", metavar="VERSION",
                    help="Record the given migration filename(s) as already-applied "
                         "without running them. Use when a schema was created out-of-band "
                         "(e.g. by init-db.sh).")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    load_dotenv_files(str(REPO_ROOT / ".env"))

    if args.baseline:
        return baseline(args.baseline)

    migrations = list_migrations()
    if not migrations:
        log.warning("no .sql files found in %s", MIGRATIONS_DIR)
        return 0

    # In dry-run we MAY connect just enough to check what's applied. Tolerate
    # the case where the tracking table doesn't exist yet.
    try:
        ensure_tracking_table()
        applied = list_applied()
    except Exception as e:
        log.warning("could not read schema_migrations (%s); treating all as pending", e)
        applied = {}

    pending: list[Path] = []
    drift: list[tuple[Path, str, str]] = []
    for path in migrations:
        cs = file_checksum(path)
        if path.name in applied:
            if applied[path.name] != cs:
                drift.append((path, applied[path.name], cs))
        else:
            pending.append(path)

    if drift:
        log.error("checksum drift detected on previously-applied migrations:")
        for path, old, new in drift:
            log.error("  %s: applied=%s  on-disk=%s", path.name, old[:12], new[:12])
        log.error("refusing to proceed. either revert the edit or add a new migration file.")
        return 2

    if not pending:
        log.info("no pending migrations (%d already applied)", len(applied))
        return 0

    log.info("%d pending migration(s):", len(pending))
    for path in pending:
        log.info("  - %s", path.name)

    if args.dry_run:
        log.info("dry-run: not applying")
        return 0

    for path in pending:
        cs = file_checksum(path)
        apply_one(path, cs)
        log.info("  -> applied %s", path.name)

    log.info("done. %d migration(s) applied.", len(pending))
    return 0


if __name__ == "__main__":
    sys.exit(main())
