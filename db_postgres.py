"""Quiz Pilot PostgreSQL production database adapter.
No SQLite database is opened or used. This module only preserves the existing
main.py call style while executing every query against PostgreSQL.
"""
import os, re, threading
from psycopg2 import pool as _pool_mod, IntegrityError, OperationalError, DatabaseError, InterfaceError
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL Railway Variables ichida topilmadi")

_MIN = max(1, int(os.getenv("PG_POOL_MIN", "2")))
_MAX = max(_MIN, int(os.getenv("PG_POOL_MAX", "30")))
_CONNECT_TIMEOUT = max(3, int(os.getenv("PG_CONNECT_TIMEOUT", "10")))
_lock = threading.Lock()
_pool = None

# sqlite3 compatibility symbols used by existing exception handlers/row_factory lines.
class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

def _get_pool():
    global _pool
    with _lock:
        if _pool is None:
            _pool = _pool_mod.ThreadedConnectionPool(
                minconn=_MIN, maxconn=_MAX,
                dsn=DATABASE_URL, connect_timeout=_CONNECT_TIMEOUT,
                application_name="quiz-pilot-bot",
            )
        return _pool

def _translate(sql: str) -> str:
    s = sql.strip()
    up = s.upper()
    # SQLite PRAGMA is handled by Cursor before translation.
    s = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bAUTOINCREMENT\b", "", s, flags=re.I)
    # SQLite placeholders.
    s = s.replace("?", "%s")
    # SQLite INSERT OR IGNORE.
    if re.match(r"^INSERT\s+OR\s+IGNORE\s+INTO", s, re.I):
        s = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, flags=re.I)
        if not re.search(r"\bON\s+CONFLICT\b", s, re.I):
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # Existing app uses OR REPLACE only for teacher_variants.
    if re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+teacher_variants", s, re.I):
        s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I).rstrip().rstrip(";")
        s += " ON CONFLICT (quiz_id, variant_code) DO UPDATE SET variant_json=EXCLUDED.variant_json, created_at=EXCLUDED.created_at"
    return s

class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.cur = None
        self._fake = None

    def execute(self, sql, params=None):
        self._fake = None
        raw = str(sql).strip()
        upper = raw.upper()
        # PostgreSQL equivalents for SQLite PRAGMA used by this application.
        if upper.startswith("PRAGMA"):
            if "TABLE_INFO" in upper:
                m = re.search(r"table_info\(([^)]+)\)", raw, re.I)
                table = m.group(1).strip(" '\"`") if m else ""
                c = self.connection.raw.cursor(cursor_factory=RealDictCursor)
                try:
                    c.execute("""SELECT column_name, data_type, is_nullable, column_default
                                 FROM information_schema.columns
                                 WHERE table_schema=current_schema() AND table_name=%s
                                 ORDER BY ordinal_position""", (table,))
                    rows = c.fetchall()
                finally:
                    c.close()
                self._fake = [(i, r["column_name"], r["data_type"], 0 if r["is_nullable"] == "YES" else 1, r["column_default"], 0)
                              for i, r in enumerate(rows)]
            # journal_mode, busy_timeout and foreign_keys are SQLite-only and intentionally no-op.
            return self
        # SQLite-only rowid repair from an old schema helper is never valid on PostgreSQL.
        if "ROWID" in upper:
            return self
        translated = _translate(raw)
        self.cur = self.connection.raw.cursor(cursor_factory=RealDictCursor)
        self.connection._cursors.append(self)
        self.cur.execute(translated, params)
        return self

    def fetchone(self):
        if self._fake is not None:
            return self._fake.pop(0) if self._fake else None
        if not self.cur:
            return None
        row = self.cur.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        if self._fake is not None:
            rows, self._fake = self._fake, []
            return rows
        if not self.cur:
            return []
        return [Row(r) for r in self.cur.fetchall()]

    def close(self):
        if self.cur is not None:
            try: self.cur.close()
            finally: self.cur = None

class Connection:
    def __init__(self, raw):
        self.raw = raw
        self.row_factory = None
        self._cursors = []
        self._closed = False

    def cursor(self):
        return Cursor(self)

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
                    # Never return an aborted transaction to the pool.
                    self.raw.rollback()
            except Exception:
                pass
            _get_pool().putconn(self.raw)

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        if exc_type: self.rollback()
        self.close()
        return False

def connect(*args, **kwargs):
    raw = _get_pool().getconn()
    raw.autocommit = False
    return Connection(raw)

def pool_status():
    p = _get_pool()
    return {"minconn": _MIN, "maxconn": _MAX, "used": len(p._used)}

def close_pool():
    global _pool
    with _lock:
        if _pool is not None:
            _pool.closeall(); _pool = None
