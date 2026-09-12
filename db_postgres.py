# PostgreSQL compatibility layer for Quiz Pilot
import os, re, threading
from psycopg2 import pool, IntegrityError, OperationalError, DatabaseError
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL Railway Variable topilmadi')

_lock = threading.Lock()
_pool = None

def _get_pool():
    global _pool
    with _lock:
        if _pool is None:
            _pool = pool.ThreadedConnectionPool(
                minconn=max(1, int(os.getenv('PG_POOL_MIN','2'))),
                maxconn=max(4, int(os.getenv('PG_POOL_MAX','20'))),
                dsn=DATABASE_URL,
                connect_timeout=int(os.getenv('PG_CONNECT_TIMEOUT','10')),
            )
        return _pool

class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

class Cursor:
    def __init__(self, conn): self.conn=conn; self.cur=None; self._fake=None
    def execute(self, sql, params=None):
        self._fake=None
        s=sql.strip()
        upper=s.upper()
        if upper.startswith('PRAGMA '):
            if 'TABLE_INFO' in upper:
                m=re.search(r'table_info\(([^)]+)\)', s, re.I)
                table=m.group(1).strip('"` ') if m else ''
                c=self.conn.raw.cursor(cursor_factory=RealDictCursor)
                c.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",(table,))
                rows=c.fetchall(); c.close()
                self._fake=[(i,r['column_name'],r['data_type'],0,None,0) for i,r in enumerate(rows)]
            return self
        s=re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b','BIGSERIAL PRIMARY KEY',s,flags=re.I)
        s=re.sub(r'\bAUTOINCREMENT\b','',s,flags=re.I)
        # SQLite INSERT OR IGNORE -> PostgreSQL
        if re.match(r'^INSERT\s+OR\s+IGNORE\s+INTO',s,re.I):
            s=re.sub(r'^INSERT\s+OR\s+IGNORE\s+INTO','INSERT INTO',s,flags=re.I)
            if not re.search(r'ON\s+CONFLICT',s,re.I): s=s.rstrip().rstrip(';')+' ON CONFLICT DO NOTHING'
        # known teacher variant replace
        if re.match(r'^INSERT\s+OR\s+REPLACE\s+INTO\s+teacher_variants',s,re.I):
            s=re.sub(r'^INSERT\s+OR\s+REPLACE\s+INTO','INSERT INTO',s,flags=re.I).rstrip().rstrip(';')
            s+=' ON CONFLICT (quiz_id, variant_code) DO UPDATE SET variant_json=EXCLUDED.variant_json, created_at=EXCLUDED.created_at'
        # SQLite placeholders
        s=s.replace('?', '%s')
        self.cur=self.conn.raw.cursor(cursor_factory=RealDictCursor)
        self.cur.execute(s, params)
        return self
    def fetchone(self):
        if self._fake is not None: return self._fake.pop(0) if self._fake else None
        r=self.cur.fetchone() if self.cur else None
        return Row(r) if r is not None else None
    def fetchall(self):
        if self._fake is not None: return self._fake
        rows=self.cur.fetchall() if self.cur else []
        return [Row(r) for r in rows]
    def close(self):
        if self.cur: self.cur.close(); self.cur=None

class Connection:
    def __init__(self, raw): self.raw=raw; self.row_factory=None; self._cursors=[]
    def cursor(self):
        c=Cursor(self); self._cursors.append(c); return c
    def execute(self, sql, params=None):
        c=self.cursor(); return c.execute(sql,params)
    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self):
        try:
            for c in self._cursors: c.close()
        finally:
            _get_pool().putconn(self.raw)
    def __enter__(self): return self
    def __exit__(self,*a): self.close()

def connect(*args, **kwargs):
    raw=_get_pool().getconn()
    raw.autocommit=False
    return Connection(raw)

def backup(*args, **kwargs):
    raise OperationalError('PostgreSQL rejimida SQLite backup() ishlatilmaydi')

def pool_status():
    p=_get_pool(); return {'min':p.minconn,'max':p.maxconn}
