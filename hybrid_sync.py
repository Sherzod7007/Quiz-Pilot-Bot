# -*- coding: utf-8 -*-
import os, sqlite3, threading, time, logging, re
from psycopg2 import pool, sql

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SYNC_INTERVAL = max(2, int(os.getenv("HYBRID_SYNC_INTERVAL", "5")))
BATCH_SIZE = max(10, min(1000, int(os.getenv("HYBRID_SYNC_BATCH", "100"))))
MAX_ATTEMPTS = max(1, int(os.getenv("HYBRID_SYNC_MAX_ATTEMPTS", "100")))
_ENABLED = bool(DATABASE_URL)
_pg_pool = None
_worker_started = False
_stop = threading.Event()
_lock = threading.Lock()
TYPE_MAP={"INTEGER":"BIGINT","INT":"BIGINT","TEXT":"TEXT","REAL":"DOUBLE PRECISION","BLOB":"BYTEA","NUMERIC":"NUMERIC"}

def _pg():
    global _pg_pool
    if not _ENABLED: return None
    if _pg_pool is None:
        with _lock:
            if _pg_pool is None:
                _pg_pool=pool.ThreadedConnectionPool(1,max(2,int(os.getenv("HYBRID_PG_POOL_MAX","5"))),DATABASE_URL)
    return _pg_pool
def _tables(c):
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'hybrid_%'").fetchall()]
def _cols(c,t): return c.execute(f'PRAGMA table_info("{t}")').fetchall()
def _pk(cols): return next((c[1] for c in cols if c[5]),None)
def _typ(t):
    u=(t or "TEXT").upper()
    return next((v for k,v in TYPE_MAP.items() if k in u),"TEXT")

def ensure_postgres_schema(sc):
    if not _ENABLED: return
    p=_pg(); pc=p.getconn()
    try:
        with pc.cursor() as cur:
            for t in _tables(sc):
                cols=_cols(sc,t)
                defs=[sql.SQL("{} {}").format(sql.Identifier(c[1]),sql.SQL(_typ(c[2]))) for c in cols]
                cur.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(sql.Identifier(t),sql.SQL(", ").join(defs)))
                for c in cols:
                    cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(sql.Identifier(t),sql.Identifier(c[1]),sql.SQL(_typ(c[2]))))
        pc.commit()
    except Exception:
        pc.rollback(); raise
    finally: p.putconn(pc)

def _install_outbox(c):
    c.execute("CREATE TABLE IF NOT EXISTS hybrid_sync_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, pk_name TEXT NOT NULL, pk_value TEXT NOT NULL, operation TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_hybrid_outbox_id ON hybrid_sync_outbox(id)")
    for t in _tables(c):
        pk=_pk(_cols(c,t))
        if not pk: continue
        safe=re.sub(r'[^a-zA-Z0-9_]','_',t)
        for op,ref in (("INSERT","NEW"),("UPDATE","NEW"),("DELETE","OLD")):
            trig=f"hybrid_{safe}_{op.lower()}"
            q=f'''CREATE TRIGGER IF NOT EXISTS "{trig}" AFTER {op} ON "{t}" BEGIN
            INSERT INTO hybrid_sync_outbox(table_name,pk_name,pk_value,operation,attempts,created_at)
            VALUES ('{t}','{pk}',CAST({ref}."{pk}" AS TEXT),'{op}',0,CAST(strftime('%s','now') AS INTEGER)); END'''
            c.execute(q)
    c.commit()

def _initial_enqueue(c):
    c.execute("CREATE TABLE IF NOT EXISTS hybrid_sync_state (key TEXT PRIMARY KEY, value TEXT)")
    if c.execute("SELECT 1 FROM hybrid_sync_state WHERE key='initial_enqueue_v1'").fetchone(): return
    now=int(time.time())
    for t in _tables(c):
        pk=_pk(_cols(c,t))
        if not pk: continue
        rows=c.execute(f'SELECT "{pk}" FROM "{t}"').fetchall()
        c.executemany("INSERT INTO hybrid_sync_outbox(table_name,pk_name,pk_value,operation,attempts,created_at) VALUES (?,?,?,?,0,?)",[(t,pk,str(r[0]),"UPSERT",now) for r in rows])
    c.execute("INSERT OR REPLACE INTO hybrid_sync_state(key,value) VALUES('initial_enqueue_v1','1')"); c.commit()

def _sync_one(sc,cur,item):
    _,t,pk,pv,op=item
    # Mirror is deliberately DELETE+INSERT instead of ON CONFLICT so it also works
    # with pre-existing PostgreSQL tables that were created without PK/UNIQUE constraints.
    cur.execute(sql.SQL("DELETE FROM {} WHERE {}=%s").format(sql.Identifier(t),sql.Identifier(pk)),(pv,))
    if op=="DELETE": return
    row=sc.execute(f'SELECT * FROM "{t}" WHERE "{pk}"=?',(pv,)).fetchone()
    if row is None: return
    names=[d[0] for d in sc.execute(f'SELECT * FROM "{t}" LIMIT 0').description]
    vals=list(row)
    q=sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(t),sql.SQL(", ").join(map(sql.Identifier,names)),
        sql.SQL(", ").join([sql.SQL("%s")]*len(names)))
    cur.execute(q,vals)

def sync_once(path):
    if not _ENABLED: return False
    sc=sqlite3.connect(path,timeout=30); sc.row_factory=sqlite3.Row; p=_pg(); pc=None
    try:
        rows=sc.execute("SELECT id,table_name,pk_name,pk_value,operation FROM hybrid_sync_outbox ORDER BY id LIMIT ?",(BATCH_SIZE,)).fetchall()
        if not rows: return True
        pc=p.getconn()
        try:
            with pc.cursor() as cur:
                for r in rows: _sync_one(sc,cur,r)
            pc.commit()
            sc.executemany("DELETE FROM hybrid_sync_outbox WHERE id=?",[(r[0],) for r in rows]); sc.commit(); return True
        except Exception as e:
            pc.rollback(); logging.warning("Hybrid PostgreSQL sync retry: %s",e)
            sc.executemany("UPDATE hybrid_sync_outbox SET attempts=attempts+1 WHERE id=?",[(r[0],) for r in rows]); sc.execute("DELETE FROM hybrid_sync_outbox WHERE attempts>?",(MAX_ATTEMPTS,)); sc.commit(); return False
    finally:
        if pc is not None: p.putconn(pc)
        sc.close()

def _worker(path):
    logging.info("Hybrid DB worker started | SQLite MASTER | PostgreSQL MIRROR | interval=%ss",SYNC_INTERVAL)
    while not _stop.wait(SYNC_INTERVAL):
        try: sync_once(path)
        except Exception as e: logging.warning("Hybrid DB worker error: %s",e)

def start(path):
    global _worker_started
    if not _ENABLED:
        logging.info("Hybrid DB disabled: DATABASE_URL not set; SQLite standalone."); return False
    try:
        sc=sqlite3.connect(path,timeout=30); _install_outbox(sc); ensure_postgres_schema(sc); _initial_enqueue(sc); sc.close()
    except Exception as e:
        logging.exception("Hybrid DB init failed; SQLite continues normally: %s",e); return False
    with _lock:
        if not _worker_started:
            _worker_started=True; threading.Thread(target=_worker,args=(path,),daemon=True,name="HybridPostgresMirror").start()
    return True
