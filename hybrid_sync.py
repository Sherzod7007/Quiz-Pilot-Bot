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

                defs = [
                    sql.SQL("{} {}").format(
                        sql.Identifier(c[1]),
                        sql.SQL(_pg_type(c[2]))
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
                        sql.SQL(_pg_type(c[2]))
                    ))

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
    if not _ENABLED:
        return {"synced":0,"failed":0,"pending":0}
    sc = _sqlite_connect(path)
    pg = _pg()
    pc = None
    synced = failed = 0
    failure_details = {}
    try:
        rows = sc.execute("""
            SELECT table_name,pk_name,pk_value,operation,attempts,updated_at,next_retry_at
            FROM hybrid_sync_outbox_v2
            WHERE next_retry_at<=?
            ORDER BY updated_at LIMIT ?
        """, (int(time.time()), BATCH_SIZE)).fetchall()
        if not rows:
            return {"synced":0,"failed":0,"pending":0}
        pc = pg.getconn()
        for item in rows:
            try:
                _sync_item(sc, pc, item)
                pc.commit()
                _mark_success(sc, item)
                synced += 1
            except Exception as exc:
                pc.rollback()
                _mark_failure(sc, item, exc)
                failed += 1

                # Railway logda asl PostgreSQL/psycopg2 xatosini ko'rsatish uchun
                # bir xil xatolarni bitta batch ichida jamlaymiz.
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

        sc.commit()
        pending = sc.execute("SELECT COUNT(*) FROM hybrid_sync_outbox_v2").fetchone()[0]

        if failure_details:
            for (table, pk_name, operation, exc_type, error_text), detail in list(failure_details.items())[:10]:
                _log(
                    "warning",
                    "sync item failed | table=%s | %s=%s | op=%s | attempts=%s | count=%s | %s: %s",
                    table, pk_name, detail["pk_value"], operation, detail["attempts"],
                    detail["count"], exc_type, error_text
                )
            if len(failure_details) > 10:
                _log("warning", "sync failure summary truncated: %s different error group(s)", len(failure_details))

        if synced or failed:
            level = "info" if failed == 0 else "warning"
            _log(level, "sync summary | synced=%s failed=%s pending=%s", synced, failed, pending)
        return {"synced":synced,"failed":failed,"pending":pending}
    finally:
        if pc is not None:
            pg.putconn(pc)
        sc.close()

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
