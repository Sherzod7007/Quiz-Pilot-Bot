# -*- coding: utf-8 -*-
"""
Quiz Pilot Professional Hybrid Database V2
SQLite = MASTER / source of truth
PostgreSQL = asynchronous MIRROR
"""
import logging
import os
import re
import sqlite3
import threading
import time
from psycopg2 import pool, sql

PG_URL = os.getenv("HYBRID_DATABASE_URL", "").strip()
SYNC_INTERVAL = max(2, int(os.getenv("HYBRID_SYNC_INTERVAL", "5")))
BATCH_SIZE = max(10, min(1000, int(os.getenv("HYBRID_SYNC_BATCH", "200"))))
PG_POOL_MAX = max(1, min(20, int(os.getenv("HYBRID_PG_POOL_MAX", "5"))))
MAX_BACKOFF = max(10, min(3600, int(os.getenv("HYBRID_MAX_BACKOFF", "300"))))

_ENABLED = bool(PG_URL)
_pg_pool = None
_worker_started = False
_stop = threading.Event()
_lock = threading.Lock()

TYPE_MAP = {
    "INT": "BIGINT", "CHAR": "TEXT", "CLOB": "TEXT", "TEXT": "TEXT",
    "BLOB": "BYTEA", "REAL": "DOUBLE PRECISION", "FLOA": "DOUBLE PRECISION",
    "DOUB": "DOUBLE PRECISION", "NUMERIC": "NUMERIC", "DECIMAL": "NUMERIC",
    "BOOL": "BOOLEAN", "DATE": "TIMESTAMP", "TIME": "TIMESTAMP",
}

def _log(level, message, *args):
    getattr(logging, level)("Hybrid V2 | " + message, *args)

def _pg():
    global _pg_pool
    if not _ENABLED:
        return None
    if _pg_pool is None:
        with _lock:
            if _pg_pool is None:
                _pg_pool = pool.ThreadedConnectionPool(1, PG_POOL_MAX, dsn=PG_URL)
    return _pg_pool

def _sqlite_connect(path):
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def _tables(conn):
    rows = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'hybrid_%'
        ORDER BY name
    """).fetchall()
    return [r[0] for r in rows]

def _columns(conn, table):
    return conn.execute('PRAGMA table_info("{}")'.format(table.replace('"', '""'))).fetchall()

def _pk_columns(columns):
    rows = [c for c in columns if c[5]]
    rows.sort(key=lambda c: c[5])
    return [c[1] for c in rows]

def _single_pk(columns):
    pks = _pk_columns(columns)
    return pks[0] if len(pks) == 1 else None

def _pg_type(sqlite_type):
    t = (sqlite_type or "TEXT").upper()
    for key, value in TYPE_MAP.items():
        if key in t:
            return value
    return "TEXT"


def _value_requires_text(value):
    """True when a SQLite value cannot safely be represented by a numeric PostgreSQL type."""
    if value is None or isinstance(value, (int, float, bool)):
        return False
    if isinstance(value, (bytes, bytearray, memoryview)):
        return False
    text = str(value).strip()
    if not text:
        return True
    # Accept integer/decimal/exponent forms only. IDs like q_..., fc_..., ts_... require TEXT.
    return re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text) is None

def _mirror_type(sqlite_conn, table, column):
    """
    SQLite is dynamically typed. Some legacy Quiz Pilot tables declare an ID as INTEGER
    but contain text IDs (q_..., fc_..., ts_..., etc.). PostgreSQL is strict, so infer
    TEXT from real SQLite values when necessary.
    """
    declared = _pg_type(column[2])
    if declared in ("BIGINT", "DOUBLE PRECISION", "NUMERIC"):
        name = column[1].replace('"', '""')
        tbl = table.replace('"', '""')
        rows = sqlite_conn.execute(
            'SELECT "{}" FROM "{}" WHERE "{}" IS NOT NULL LIMIT 1000'.format(name, tbl, name)
        ).fetchall()
        if any(_value_requires_text(r[0]) for r in rows):
            return "TEXT"
    return declared

def _postgres_column_types(cur, table):
    cur.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
    """, (table,))
    return {row[0]: row[1] for row in cur.fetchall()}

def _postgres_type_matches(current, desired):
    current = (current or "").lower()
    desired = desired.upper()
    aliases = {
        "BIGINT": {"bigint"},
        "TEXT": {"text", "character varying", "character"},
        "BYTEA": {"bytea"},
        "DOUBLE PRECISION": {"double precision", "real"},
        "NUMERIC": {"numeric", "decimal"},
        "BOOLEAN": {"boolean"},
        "TIMESTAMP": {"timestamp without time zone", "timestamp with time zone"},
    }
    return current in aliases.get(desired, {desired.lower()})

def _reconcile_postgres_column_types(cur, sqlite_conn, table, columns):
    """
    Upgrade an existing MIRROR schema when an old deployment created a strict numeric
    PostgreSQL column for a dynamically typed SQLite column containing text IDs.
    Mirror data is disposable/rebuildable from SQLite MASTER.
    """
    existing = _postgres_column_types(cur, table)
    for c in columns:
        name = c[1]
        desired = _mirror_type(sqlite_conn, table, c)
        if name not in existing or _postgres_type_matches(existing[name], desired):
            continue
        # This migration intentionally changes only to TEXT. SQLite's dynamic typing
        # can legitimately contain q_/fc_/ts_ identifiers in an INTEGER-declared column.
        # Other declared-type differences are left unchanged to avoid destructive casts.
        if desired != "TEXT":
            continue
        _log("warning", "migrating PostgreSQL mirror column %s.%s from %s to TEXT",
             table, name, existing[name])
        cur.execute(sql.SQL(
            "ALTER TABLE {} ALTER COLUMN {} TYPE TEXT USING {}::text"
        ).format(
            sql.Identifier(table),
            sql.Identifier(name),
            sql.Identifier(name),
        ))

def _index_name(table, pks):
    return re.sub(r"[^A-Za-z0-9_]+", "_", "hybrid_uq_%s_%s" % (table, "_".join(pks)))[:60]

def _deduplicate_postgres_keys(cur, table, pks):
    """
    PostgreSQL mirror may contain duplicate rows left by an older Hybrid version.
    SQLite is the MASTER, so removing duplicate mirror rows is safe.
    Keep one copy; current SQLite rows are re-upserted afterwards.
    """
    if not pks:
        return 0

    conditions = sql.SQL(" AND ").join(
        sql.SQL("a.{} IS NOT DISTINCT FROM b.{}").format(
            sql.Identifier(pk), sql.Identifier(pk)
        )
        for pk in pks
    )

    query = sql.SQL(
        "DELETE FROM {table} AS a USING {table} AS b "
        "WHERE a.ctid < b.ctid AND ({conditions})"
    ).format(
        table=sql.Identifier(table),
        conditions=conditions,
    )

    cur.execute(query)
    removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if removed:
        _log(
            "warning",
            "removed %s duplicate mirror row(s) from %s before UNIQUE index",
            removed,
            table,
        )
    return removed


def ensure_postgres_schema(sqlite_conn):
    pg = _pg()
    pg_conn = pg.getconn()
    try:
        with pg_conn.cursor() as cur:
            for table in _tables(sqlite_conn):
                columns = _columns(sqlite_conn, table)
                if not columns:
                    continue

                mirror_types = {c[1]: _mirror_type(sqlite_conn, table, c) for c in columns}
                defs = [
                    sql.SQL("{} {}").format(
                        sql.Identifier(c[1]),
                        sql.SQL(mirror_types[c[1]])
                    )
                    for c in columns
                ]

                cur.execute(sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} ({})"
                ).format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(defs)
                ))

                for c in columns:
                    cur.execute(sql.SQL(
                        "ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}"
                    ).format(
                        sql.Identifier(table),
                        sql.Identifier(c[1]),
                        sql.SQL(mirror_types[c[1]])
                    ))

                # Existing PostgreSQL mirror tables may have been created by an older
                # version using SQLite's declared type only. Reconcile them with actual
                # SQLite values before any upsert/index operation.
                _reconcile_postgres_column_types(cur, sqlite_conn, table, columns)

                pks = _pk_columns(columns)
                if pks:
                    # Older mirror versions could leave duplicate rows in PostgreSQL.
                    # SQLite is MASTER, so deduplicating the MIRROR is safe.
                    _deduplicate_postgres_keys(cur, table, pks)

                    cur.execute(sql.SQL(
                        "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})"
                    ).format(
                        sql.Identifier(_index_name(table, pks)),
                        sql.Identifier(table),
                        sql.SQL(", ").join(map(sql.Identifier, pks))
                    ))

        pg_conn.commit()
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        pg.putconn(pg_conn)


def _drop_v1_triggers(conn):
    rows = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='trigger' AND name LIKE 'hybrid_%'
    """).fetchall()
    for row in rows:
        conn.execute('DROP TRIGGER IF EXISTS "{}"'.format(row[0].replace('"', '""')))

def _migrate_outbox(conn):
    _drop_v1_triggers(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hybrid_sync_outbox_v2 (
            table_name TEXT NOT NULL,
            pk_name TEXT NOT NULL,
            pk_value TEXT NOT NULL,
            operation TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL,
            next_retry_at INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            PRIMARY KEY (table_name, pk_name, pk_value)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hybrid_v2_ready
        ON hybrid_sync_outbox_v2(next_retry_at, updated_at)
    """)
    has_v1 = conn.execute("""
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='hybrid_sync_outbox'
    """).fetchone()
    if has_v1:
        rows = conn.execute("""
            SELECT table_name, pk_name, pk_value, operation, created_at
            FROM hybrid_sync_outbox ORDER BY id
        """).fetchall()
        for r in rows:
            conn.execute("""
                INSERT INTO hybrid_sync_outbox_v2
                (table_name,pk_name,pk_value,operation,attempts,updated_at,next_retry_at,last_error)
                VALUES (?,?,?,?,0,?,0,NULL)
                ON CONFLICT(table_name,pk_name,pk_value) DO UPDATE SET
                    operation=excluded.operation, attempts=0,
                    updated_at=excluded.updated_at, next_retry_at=0,last_error=NULL
            """, (r[0], r[1], str(r[2]), r[3], r[4] or int(time.time())))
        conn.execute("DELETE FROM hybrid_sync_outbox")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hybrid_sync_state (
            key TEXT PRIMARY KEY, value TEXT
        )
    """)

def _install_v2_triggers(conn):
    for table in _tables(conn):
        pk = _single_pk(_columns(conn, table))
        if not pk:
            _log("warning", "table %s skipped: no single-column primary key", table)
            continue
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", table)[:40]
        for op, ref in (("INSERT","NEW"),("UPDATE","NEW"),("DELETE","OLD")):
            trigger = "hybrid_v2_%s_%s" % (safe, op.lower())
            statement = """
                CREATE TRIGGER IF NOT EXISTS "{trigger}"
                AFTER {op} ON "{table}"
                BEGIN
                    INSERT INTO hybrid_sync_outbox_v2
                    (table_name,pk_name,pk_value,operation,attempts,updated_at,next_retry_at,last_error)
                    VALUES ('{table_lit}','{pk_lit}',CAST({ref}."{pk}" AS TEXT),
                            '{op}',0,CAST(strftime('%s','now') AS INTEGER),0,NULL)
                    ON CONFLICT(table_name,pk_name,pk_value) DO UPDATE SET
                        operation=excluded.operation, attempts=0,
                        updated_at=excluded.updated_at,next_retry_at=0,last_error=NULL;
                END
            """.format(
                trigger=trigger, op=op,
                table=table.replace('"','""'), table_lit=table.replace("'","''"),
                pk=pk.replace('"','""'), pk_lit=pk.replace("'","''"), ref=ref)
            conn.execute(statement)

def _initial_enqueue_v2(conn):
    if conn.execute("SELECT value FROM hybrid_sync_state WHERE key='initial_enqueue_v2'").fetchone():
        return 0
    now = int(time.time())
    total = 0
    for table in _tables(conn):
        pk = _single_pk(_columns(conn, table))
        if not pk:
            continue
        rows = conn.execute('SELECT "{}" FROM "{}"'.format(
            pk.replace('"','""'), table.replace('"','""'))).fetchall()
        conn.executemany("""
            INSERT INTO hybrid_sync_outbox_v2
            (table_name,pk_name,pk_value,operation,attempts,updated_at,next_retry_at,last_error)
            VALUES (?,?,?,'UPSERT',0,?,0,NULL)
            ON CONFLICT(table_name,pk_name,pk_value) DO UPDATE SET
                operation='UPSERT',attempts=0,updated_at=excluded.updated_at,
                next_retry_at=0,last_error=NULL
        """, [(table, pk, str(r[0]), now) for r in rows])
        total += len(rows)
    conn.execute("""
        INSERT OR REPLACE INTO hybrid_sync_state(key,value)
        VALUES('initial_enqueue_v2','1')
    """)
    return total

def _row_for_sync(sqlite_conn, item):
    table, pk, value = item["table_name"], item["pk_name"], item["pk_value"]
    names = [c[1] for c in _columns(sqlite_conn, table)]
    row = sqlite_conn.execute('SELECT * FROM "{}" WHERE "{}"=?'.format(
        table.replace('"','""'), pk.replace('"','""')), (value,)).fetchone()
    return names, None if row is None else [row[n] for n in names]

def _upsert_one(cur, table, pk_name, names, values):
    updates = [n for n in names if n != pk_name]
    action = (sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(
        sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(n), sql.Identifier(n))
        for n in updates)) if updates else sql.SQL("DO NOTHING")
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) {}").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, names)),
        sql.SQL(", ").join(sql.Placeholder() for _ in names),
        sql.Identifier(pk_name), action)
    cur.execute(query, values)

def _sync_item(sqlite_conn, pg_conn, item):
    table, pk, value, op = item["table_name"], item["pk_name"], item["pk_value"], item["operation"]
    with pg_conn.cursor() as cur:
        if op == "DELETE":
            cur.execute(sql.SQL("DELETE FROM {} WHERE {}=%s").format(
                sql.Identifier(table), sql.Identifier(pk)), (value,))
            return
        names, values = _row_for_sync(sqlite_conn, item)
        if values is None:
            cur.execute(sql.SQL("DELETE FROM {} WHERE {}=%s").format(
                sql.Identifier(table), sql.Identifier(pk)), (value,))
            return
        _upsert_one(cur, table, pk, names, values)

def _mark_success(conn, item):
    conn.execute("""
        DELETE FROM hybrid_sync_outbox_v2
        WHERE table_name=? AND pk_name=? AND pk_value=?
    """, (item["table_name"], item["pk_name"], item["pk_value"]))

def _mark_failure(conn, item, error):
    attempts = int(item["attempts"] or 0) + 1
    delay = min(MAX_BACKOFF, 2 ** min(attempts, 8))
    conn.execute("""
        UPDATE hybrid_sync_outbox_v2
        SET attempts=?,next_retry_at=?,last_error=?
        WHERE table_name=? AND pk_name=? AND pk_value=?
    """, (attempts, int(time.time()) + delay, str(error)[:1000],
          item["table_name"], item["pk_name"], item["pk_value"]))

def sync_once(path):
    """Sync one bounded batch with one PostgreSQL transaction.

    SQLite remains the MASTER. PostgreSQL work is asynchronous. Each item gets
    its own SAVEPOINT so one bad row does not abort the whole batch, while one
    final COMMIT greatly reduces PostgreSQL round-trips under load.

    If the process dies after PostgreSQL COMMIT but before the SQLite outbox is
    cleared, replay is safe because mirror rows are upserted/deleted by their
    primary key and therefore the operation is idempotent.
    """
    if not _ENABLED:
        return {"synced": 0, "failed": 0, "pending": 0}

    sc = _sqlite_connect(path)
    pg = _pg()
    pc = None
    synced = failed = 0
    success_items = []
    failure_details = {}

    try:
        rows = sc.execute("""
            SELECT table_name,pk_name,pk_value,operation,attempts,updated_at,next_retry_at
            FROM hybrid_sync_outbox_v2
            WHERE next_retry_at<=?
            ORDER BY updated_at LIMIT ?
        """, (int(time.time()), BATCH_SIZE)).fetchall()

        if not rows:
            pending = sc.execute(
                "SELECT COUNT(*) FROM hybrid_sync_outbox_v2"
            ).fetchone()[0]
            return {"synced": 0, "failed": 0, "pending": pending}

        pc = pg.getconn()
        try:
            with pc.cursor() as cur:
                # Explicit durability: committed mirror data should survive a
                # PostgreSQL restart. This is the default, but keeping it explicit
                # prevents a future connection-level setting from weakening it.
                cur.execute("SET LOCAL synchronous_commit = on")

                for item in rows:
                    try:
                        # Isolate a bad row while keeping the rest of this batch.
                        cur.execute("SAVEPOINT hybrid_item")
                        _sync_item(sc, pc, item)
                        cur.execute("RELEASE SAVEPOINT hybrid_item")
                        success_items.append(item)
                        synced += 1
                    except Exception as exc:
                        try:
                            cur.execute("ROLLBACK TO SAVEPOINT hybrid_item")
                            cur.execute("RELEASE SAVEPOINT hybrid_item")
                        except Exception:
                            # Connection-level failure: abort the entire PG
                            # transaction and let the worker retry later.
                            pc.rollback()
                            raise

                        _mark_failure(sc, item, exc)
                        failed += 1

                        error_text = " ".join(str(exc).split())[:1000]
                        key = (
                            item["table_name"], item["pk_name"], item["operation"],
                            type(exc).__name__, error_text
                        )
                        if key not in failure_details:
                            failure_details[key] = {
                                "count": 0,
                                "pk_value": item["pk_value"],
                                "attempts": int(item["attempts"] or 0) + 1,
                            }
                        failure_details[key]["count"] += 1

            # One PostgreSQL COMMIT for the whole bounded batch.
            pc.commit()
        except Exception as exc:
            pc.rollback()
            # PostgreSQL did not commit, so SQLite must not acknowledge any
            # supposedly successful rows. Their outbox records remain/revert.
            success_items = []
            synced = 0
            raise exc

        # Acknowledge only after PostgreSQL has durably committed.
        # If the process dies between these deletes and the SQLite COMMIT, the
        # rows are retried and the PostgreSQL upsert/delete remains idempotent.
        for item in success_items:
            _mark_success(sc, item)
        sc.commit()

        pending = sc.execute(
            "SELECT COUNT(*) FROM hybrid_sync_outbox_v2"
        ).fetchone()[0]

        if failure_details:
            for (table, pk_name, operation, exc_type, error_text), detail in list(failure_details.items())[:10]:
                _log(
                    "warning",
                    "sync item failed | table=%s | %s=%s | op=%s | attempts=%s | count=%s | %s: %s",
                    table, pk_name, detail["pk_value"], operation,
                    detail["attempts"], detail["count"], exc_type, error_text
                )
            if len(failure_details) > 10:
                _log(
                    "warning",
                    "sync failure summary truncated: %s different error group(s)",
                    len(failure_details),
                )

        if synced or failed:
            level = "info" if failed == 0 else "warning"
            _log(
                level,
                "sync summary | synced=%s failed=%s pending=%s",
                synced, failed, pending,
            )

        return {"synced": synced, "failed": failed, "pending": pending}

    except Exception as exc:
        _log(
            "warning",
            "batch sync transaction failed; SQLite remains MASTER: %s",
            exc,
        )
        try:
            sc.rollback()
        except Exception:
            pass
        try:
            pending = sc.execute(
                "SELECT COUNT(*) FROM hybrid_sync_outbox_v2"
            ).fetchone()[0]
        except Exception:
            pending = -1
        return {"synced": 0, "failed": failed, "pending": pending}

    finally:
        if pc is not None:
            pg.putconn(pc)
        sc.close()


def diagnostic_snapshot(path, table_name=None, pk_value=None):
    """
    Read-only Hybrid V2 diagnostic.
    Compares SQLite MASTER and PostgreSQL MIRROR table row counts and,
    optionally, one record identified by its single-column primary key.
    """
    result = {
        "enabled": bool(_ENABLED),
        "mode": "SQLite MASTER / PostgreSQL MIRROR",
        "sqlite": {},
        "postgresql": {},
        "tables": [],
        "mismatches": [],
        "outbox": {},
    }
    sc = _sqlite_connect(path)
    try:
        result["outbox"] = {
            "pending": sc.execute(
                "SELECT COUNT(*) FROM hybrid_sync_outbox_v2"
            ).fetchone()[0] if sc.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hybrid_sync_outbox_v2'"
            ).fetchone() else 0,
            "failed": sc.execute(
                "SELECT COUNT(*) FROM hybrid_sync_outbox_v2 WHERE attempts > 0"
            ).fetchone()[0] if sc.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hybrid_sync_outbox_v2'"
            ).fetchone() else 0,
        }

        tables = _tables(sc)
        if table_name:
            if table_name not in tables:
                raise ValueError("SQLite table not found: %s" % table_name)
            tables = [table_name]

        result["sqlite"]["database"] = path
        result["sqlite"]["table_count"] = len(tables)

        if not _ENABLED:
            result["postgresql"]["connected"] = False
            for table in tables:
                count = sc.execute(
                    'SELECT COUNT(*) FROM "{}"'.format(table.replace('"','""'))
                ).fetchone()[0]
                result["tables"].append({
                    "table": table, "sqlite_count": count,
                    "postgresql_count": None, "match": False
                })
            return result

        pg = _pg()
        pc = pg.getconn()
        try:
            result["postgresql"]["connected"] = True
            with pc.cursor() as cur:
                for table in tables:
                    sqlite_count = sc.execute(
                        'SELECT COUNT(*) FROM "{}"'.format(table.replace('"','""'))
                    ).fetchone()[0]
                    pg_count = None
                    pg_error = None
                    try:
                        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(
                            sql.Identifier(table)
                        ))
                        pg_count = cur.fetchone()[0]
                    except Exception as exc:
                        pc.rollback()
                        pg_error = str(exc)

                    match = (pg_error is None and sqlite_count == pg_count)
                    row = {
                        "table": table,
                        "sqlite_count": sqlite_count,
                        "postgresql_count": pg_count,
                        "match": match,
                    }
                    if pg_error:
                        row["postgresql_error"] = pg_error
                    result["tables"].append(row)
                    if not match:
                        result["mismatches"].append(table)

                if table_name and pk_value is not None:
                    columns = _columns(sc, table_name)
                    pk = _single_pk(columns)
                    if not pk:
                        result["record"] = {
                            "checked": False,
                            "reason": "Table has no single-column primary key"
                        }
                    else:
                        sqlite_row = sc.execute(
                            'SELECT * FROM "{}" WHERE "{}"=?'.format(
                                table_name.replace('"','""'), pk.replace('"','"')
                            ), (str(pk_value),)
                        ).fetchone()
                        try:
                            cur.execute(sql.SQL(
                                "SELECT * FROM {} WHERE {}=%s"
                            ).format(sql.Identifier(table_name), sql.Identifier(pk)),
                            (str(pk_value),))
                            pg_row = cur.fetchone()
                            pg_columns = [d[0] for d in cur.description] if cur.description else []
                            pg_data = dict(zip(pg_columns, pg_row)) if pg_row else None
                            result["record"] = {
                                "checked": True,
                                "table": table_name,
                                "pk": pk,
                                "pk_value": str(pk_value),
                                "sqlite_found": sqlite_row is not None,
                                "postgresql_found": pg_row is not None,
                                "sqlite_data": dict(sqlite_row) if sqlite_row else None,
                                "postgresql_data": pg_data,
                            }
                        except Exception as exc:
                            pc.rollback()
                            result["record"] = {
                                "checked": False,
                                "table": table_name,
                                "error": str(exc),
                            }
        finally:
            pg.putconn(pc)
    finally:
        sc.close()
    return result

def _worker(path):
    _log("info", "worker started | SQLite MASTER | PostgreSQL MIRROR | interval=%ss | batch=%s",
         SYNC_INTERVAL, BATCH_SIZE)
    while not _stop.wait(SYNC_INTERVAL):
        try:
            sync_once(path)
        except Exception as exc:
            _log("warning", "worker error (SQLite continues normally): %s", exc)

def start(path):
    global _worker_started
    if not _ENABLED:
        _log("info", "disabled: HYBRID_DATABASE_URL not set; SQLite standalone.")
        return False
    try:
        sc = _sqlite_connect(path)
        try:
            _migrate_outbox(sc)
            _install_v2_triggers(sc)
            initial = _initial_enqueue_v2(sc)
            sc.commit()
            ensure_postgres_schema(sc)
        finally:
            sc.close()
        _log("info", "enabled | SQLite MASTER | PostgreSQL MIRROR | initial_pending=%s", initial)
    except Exception as exc:
        _log("exception", "initialization failed; SQLite continues normally: %s", exc)
        return False
    with _lock:
        if not _worker_started:
            _worker_started = True
            threading.Thread(target=_worker, args=(path,), daemon=True,
                             name="QuizPilotHybridV2").start()
    return True
