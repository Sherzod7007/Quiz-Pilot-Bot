"""Quiz Pilot native PostgreSQL database layer.
Only PostgreSQL is used at runtime. DATABASE_URL is mandatory.
"""
import os
import re
import threading
from psycopg2 import pool, OperationalError, IntegrityError, DatabaseError, InterfaceError
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL Railway Variables ichida topilmadi")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

MIN_CONN = max(1, int(os.getenv("PG_POOL_MIN", "2")))
MAX_CONN = max(MIN_CONN, int(os.getenv("PG_POOL_MAX", "30")))
CONNECT_TIMEOUT = max(3, int(os.getenv("PG_CONNECT_TIMEOUT", "10")))
_pool = None
_lock = threading.Lock()

class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

def _pool_instance():
    global _pool
    with _lock:
        if _pool is None:
            _pool = pool.ThreadedConnectionPool(
                MIN_CONN, MAX_CONN,
                dsn=DATABASE_URL,
                connect_timeout=CONNECT_TIMEOUT,
                application_name="quiz-pilot"
            )
        return _pool

def _normalize(sql: str) -> str:
    q = str(sql)
    # Existing application statements are normalized to PostgreSQL parameter style.
    q = q.replace("?", "%s")
    q = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", q, flags=re.I)
    q = re.sub(r"\bAUTOINCREMENT\b", "", q, flags=re.I)
    if re.match(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", q, re.I):
        q = re.sub(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", q, flags=re.I).rstrip().rstrip(";")
        q += " ON CONFLICT DO NOTHING"
    return q

class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self._cursor = None
        self._rows = None

    def execute(self, sql, params=None):
        self.close()
        self._rows = None
        self._cursor = self.connection.raw.cursor(cursor_factory=RealDictCursor)
        self._cursor.execute(_normalize(sql), params)
        return self

    def fetchone(self):
        if self._rows is not None:
            return self._rows.pop(0) if self._rows else None
        row = self._cursor.fetchone() if self._cursor else None
        return Row(row) if row is not None else None

    def fetchall(self):
        if self._rows is not None:
            rows, self._rows = self._rows, []
            return rows
        return [Row(r) for r in (self._cursor.fetchall() if self._cursor else [])]

    def close(self):
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:
                pass
            self._cursor = None

class Connection:
    def __init__(self, raw):
        self.raw = raw
        self.row_factory = None
        self._cursors = []
        self._closed = False

    def cursor(self):
        c = Cursor(self)
        self._cursors.append(c)
        return c

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            for c in self._cursors:
                c.close()
        finally:
            try:
                if not self.raw.closed:
                    self.raw.rollback()
            except Exception:
                pass
            _pool_instance().putconn(self.raw)

    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        self.close()
        return False

def connect():
    raw = _pool_instance().getconn()
    raw.autocommit = False
    return Connection(raw)

def table_columns(connection, table_name: str):
    cur = connection.raw.cursor()
    try:
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_schema=current_schema() AND table_name=%s
                       ORDER BY ordinal_position""", (table_name,))
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()

def table_info_rows(connection, table_name: str):
    cur = connection.raw.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT ordinal_position, column_name, data_type, is_nullable, column_default
                       FROM information_schema.columns
                       WHERE table_schema=current_schema() AND table_name=%s
                       ORDER BY ordinal_position""", (table_name,))
        rows = cur.fetchall()
        return [(r['ordinal_position']-1, r['column_name'], r['data_type'],
                 0 if r['is_nullable']=='YES' else 1, r['column_default'], 0) for r in rows]
    finally:
        cur.close()

def healthcheck():
    conn = connect()
    try:
        cur = conn.cursor(); cur.execute("SELECT 1 AS ok")
        return bool(cur.fetchone())
    finally:
        conn.close()
