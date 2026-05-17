# apply_migrations.py

**Purpose:** Applies pending `.sql` files under `events-infra/db/migrations/` against the local Postgres, tracking applied versions in `public.schema_migrations` with sha256 checksums. Replaces ad-hoc `psql -f ...` invocations.

## Key Functions

- `file_checksum(path)` — sha256 of file contents (hex)
- `ensure_tracking_table()` — CREATE TABLE IF NOT EXISTS `public.schema_migrations(version, applied_at, checksum)`
- `list_applied()` — returns `{version: checksum}` for rows in tracking table
- `list_migrations()` — sorted `.sql` paths under `db/migrations/`
- `apply_one(path, checksum)` — TWO-PHASE: (1) raw `pg.get_conn` + cursor runs the migration SQL (each .sql file owns its own `BEGIN; ... COMMIT;`), then `conn.commit()` and `pg.put_conn` in try/finally; (2) SEPARATE `pg.execute(INSERT INTO schema_migrations ...)` records success. If phase 2 fails the migration has already landed in PG — phase 2 logs ERROR with the exact recovery SQL for manual tracking-row INSERT. NOT wrapped in `pg.transaction()` because an outer transaction would either be terminated mid-block by the migration's embedded COMMIT or silently split DDL and tracking across two transactions.
- `baseline(versions)` — record given migration filenames as already-applied without running them. Used for pre-existing schema (e.g. tables created by `init-db.sh`).
- `main()` — CLI: `[--dry-run] [--baseline V1 V2 ...] [-v]`. Default applies; dry-run lists pending without writing; baseline records without writing.

## CLI

```
python events-infra/scripts/apply_migrations.py                              # apply pending
python events-infra/scripts/apply_migrations.py --dry-run                    # list pending only
python events-infra/scripts/apply_migrations.py --baseline 001_raw.sql 002_classified.sql  # mark as already-applied without running
```

## Drift Protection

Each applied migration's checksum is recorded. On re-run, if a previously-applied file's content has changed, the runner refuses to proceed and prints both checksums. Forces a human decision: revert the edit, or add a NEW migration file (don't silently rewrite history).

## Dependencies

- `dbkit.pg` (get_conn, put_conn, execute)
- `dbkit.constants.load_dotenv_files`

## Gotchas

- `apply_migrations.py` itself does NOT create the `signals` schema or any other schemas — that's the migration's job (`004_gate_params.sql` runs `CREATE SCHEMA IF NOT EXISTS signals`)
- `schema_migrations.version` = the basename of the .sql file (e.g. `003_structural_tags.sql`)
- The runner trusts each migration file to wrap its own `BEGIN; ... COMMIT;` block — it does not add transaction boundaries (and explicitly avoids `pg.transaction()` so the .sql file's embedded COMMIT works as authored)
- Exit codes: `0` = success/no-op, `2` = checksum drift detected (manual intervention required)
