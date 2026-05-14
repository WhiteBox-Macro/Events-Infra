"""dbkit/pg.py — unified Postgres connection gateway.

Direct psycopg2 connection pool for local Postgres. Uses
ThreadedConnectionPool for thread safety.

SECURITY NOTE: table/column names in query(), upsert(), update(), delete()
are interpolated as f-strings. These MUST be trusted strings from application
code, NEVER from user input. For untrusted identifiers, use psycopg2.sql.Identifier.

COMPLEX QUERIES: For patterns not covered by the helper functions (ILIKE, OR
conditions, comparison operators, subqueries, JOINs, aggregations), use
execute() with raw SQL. Example:
    rows = execute(
        "SELECT * FROM news.articles WHERE title ILIKE %s OR sentiment_score <= %s",
        ["%fed%", -0.5]
    )
"""
import logging
import os
import time
import threading
from contextlib import contextmanager
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor, Json

_log = logging.getLogger(__name__)

_pool = None
_pool_lock = threading.Lock()


def _adapt_params(params):
    """Auto-wrap dict/list values with psycopg2 Json() for jsonb columns."""
    if params is None:
        return None
    return [Json(v) if isinstance(v, (dict, list)) else v for v in params]


def get_pool():
    """Lazy-init connection pool from DATABASE_URL. Thread-safe with double-check locking.
    Waits up to 120s for Postgres to become available (Docker startup).
    """
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            url = os.environ.get("DATABASE_URL")
            assert url, "DATABASE_URL not set — check .env"
            for attempt in range(60):
                try:
                    _pool = ThreadedConnectionPool(1, 10, url)
                    if attempt > 0:
                        _log.info("Postgres connection established after %d retries", attempt)
                    break
                except psycopg2.OperationalError:
                    if attempt == 59:
                        _log.error("Failed to connect to Postgres after 120s")
                        raise
                    if attempt % 10 == 0:
                        _log.warning("Waiting for Postgres... attempt %d/60", attempt + 1)
                    time.sleep(2)
    return _pool


def _invalidate_pool():
    """Invalidate pool on connection failure. Next call recreates it."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None
            _log.warning("Connection pool invalidated — will recreate on next call")


def get_conn():
    """Get a connection from pool. Returns (conn, pool_ref) tuple."""
    pool = get_pool()
    return pool.getconn(), pool


def put_conn(conn, pool_ref):
    """Return connection to pool, but only if the pool hasn't been invalidated."""
    if _pool is pool_ref:
        try:
            pool_ref.putconn(conn)
        except Exception:
            pass


def query(table: str, *, select: list = None, where: dict = None,
          where_in: dict = None, where_not_null: list = None,
          order_by: str = None, limit: int = None, offset: int = None) -> list:
    """SELECT with filter support.

    Args:
        table: Schema-qualified table name, e.g. "news.articles"
        select: Column list. None = all.
        where: Equality filters, e.g. {"source_id": 1}
        where_in: IN filters, e.g. {"ticker": ["AAPL", "MSFT"]}
        where_not_null: NOT NULL filters
        order_by: e.g. "published_at DESC"
        limit, offset: pagination
    """
    cols = ", ".join(select) if select else "*"
    sql = f"SELECT {cols} FROM {table}"
    params = []
    clauses = []

    if where:
        for k, v in where.items():
            clauses.append(f"{k} = %s")
            params.append(v)
    if where_in:
        for k, vals in where_in.items():
            if not vals:
                return []
            placeholders = ", ".join(["%s"] * len(vals))
            clauses.append(f"{k} IN ({placeholders})")
            params.extend(vals)
    if where_not_null:
        for col in where_not_null:
            clauses.append(f"{col} IS NOT NULL")

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    if offset is not None:
        sql += " OFFSET %s"
        params.append(offset)

    return execute(sql, params)


def execute(sql: str, params: list = None) -> list:
    """Execute raw SQL, return list of dicts.

    Auto-commits. For multi-step atomic operations, use transaction() instead.
    On OperationalError (e.g. Docker restart), invalidates pool for auto-recovery.
    """
    conn, pool_ref = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, _adapt_params(params))
            if cur.description:
                rows = [dict(row) for row in cur.fetchall()]
            else:
                rows = []
            conn.commit()
            return rows
    except psycopg2.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        _invalidate_pool()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn, pool_ref)


@contextmanager
def transaction():
    """Context manager for multi-step atomic operations.

    Usage:
        with transaction() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("INSERT INTO news.articles ...", [...])
                cur.execute("UPDATE news.sources SET ...", [...])
    """
    conn, pool_ref = get_conn()
    try:
        yield conn
        conn.commit()
    except psycopg2.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        _invalidate_pool()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn, pool_ref)


def upsert(table: str, data: dict, conflict_on: list,
           returning: list = None) -> dict | None:
    """INSERT ... ON CONFLICT UPDATE.

    Args:
        table: Schema-qualified table name
        data: Column-value dict to insert/update
        conflict_on: Unique constraint columns for ON CONFLICT
        returning: Columns to return. None = no return.
    """
    cols = list(data.keys())
    vals = _adapt_params(list(data.values()))
    placeholders = ", ".join(["%s"] * len(vals))
    col_str = ", ".join(cols)
    update_cols = [c for c in cols if c not in conflict_on]
    conflict_str = ", ".join(conflict_on)

    sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})"
    if update_cols:
        update_str = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        sql += f" ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}"
    else:
        sql += f" ON CONFLICT ({conflict_str}) DO NOTHING"

    if returning:
        sql += f" RETURNING {', '.join(returning)}"

    conn, pool_ref = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, vals)
            row = cur.fetchone() if returning and cur.description else None
            result = dict(row) if row is not None else None
            conn.commit()
            return result
    except psycopg2.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        _invalidate_pool()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn, pool_ref)


def insert_many(table: str, rows: list[dict]) -> int:
    """Batch INSERT using execute_values (10-50x faster than row-at-a-time).
    All rows must have identical keys.
    """
    if not rows:
        return 0
    from psycopg2.extras import execute_values
    cols = list(rows[0].keys())
    expected = set(cols)
    for i, row in enumerate(rows[1:], 1):
        if set(row.keys()) != expected:
            raise ValueError(f"insert_many row {i} keys {set(row.keys())} != expected {expected}")
    col_str = ", ".join(cols)
    template = "(" + ", ".join([f"%({c})s" for c in cols]) + ")"

    adapted_rows = [{k: Json(v) if isinstance(v, (dict, list)) else v for k, v in row.items()} for row in rows]

    conn, pool_ref = get_conn()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"INSERT INTO {table} ({col_str}) VALUES %s",
                adapted_rows,
                template=template,
                page_size=1000,
            )
            conn.commit()
            return len(rows)
    except psycopg2.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        _invalidate_pool()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn, pool_ref)


def count(table: str, where: dict = None) -> int:
    """SELECT COUNT(*) with optional filters."""
    sql = f"SELECT COUNT(*) as n FROM {table}"
    params = []
    if where:
        clauses = [f"{k} = %s" for k in where]
        sql += " WHERE " + " AND ".join(clauses)
        params = list(where.values())
    rows = execute(sql, params)
    return rows[0]["n"] if rows else 0


def update(table: str, data: dict, where: dict) -> int:
    """UPDATE ... SET ... WHERE. Returns affected row count."""
    if not data or not where:
        raise ValueError("update() requires both data and where")
    set_clauses = [f"{k} = %s" for k in data]
    where_clauses = [f"{k} = %s" for k in where]
    params = list(data.values()) + list(where.values())

    sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}"

    conn, pool_ref = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            affected = cur.rowcount
            conn.commit()
            return affected
    except psycopg2.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        _invalidate_pool()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn, pool_ref)


def delete(table: str, where: dict) -> int:
    """DELETE FROM ... WHERE. Returns affected row count."""
    if not where:
        raise ValueError("delete() requires where — refusing to delete without filter")
    where_clauses = [f"{k} = %s" for k in where]
    params = list(where.values())

    sql = f"DELETE FROM {table} WHERE {' AND '.join(where_clauses)}"

    conn, pool_ref = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            affected = cur.rowcount
            conn.commit()
            return affected
    except psycopg2.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        _invalidate_pool()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn, pool_ref)
