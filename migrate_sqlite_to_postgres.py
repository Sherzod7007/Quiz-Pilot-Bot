#!/usr/bin/env python3
"""Quiz Pilot SQLite -> PostgreSQL safe migration.
Usage: python migrate_sqlite_to_postgres.py /path/to/quiz_pilot_v2.db
Run only after DATABASE_URL is set. Existing PostgreSQL data is never silently overwritten.
"""
import os, sys, sqlite3, re
import psycopg2
from psycopg2.extras import execute_values

DB_URL=os.getenv('DATABASE_URL')
if not DB_URL: raise SystemExit('ERROR: DATABASE_URL topilmadi')
SRC=sys.argv[1] if len(sys.argv)>1 else os.getenv('SQLITE_SOURCE_DB','quiz_pilot_v2.db')
if not os.path.isfile(SRC): raise SystemExit(f'ERROR: SQLite backup topilmadi: {SRC}')

src=sqlite3.connect(SRC); src.row_factory=sqlite3.Row
pg=psycopg2.connect(DB_URL); pg.autocommit=False
try:
    sc=src.cursor(); pc=pg.cursor()
    tables=[r[0] for r in sc.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print('Tables:', ', '.join(tables))
    # Refuse accidental merge into non-empty target
    for t in tables:
        pc.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s)",(t,))
        exists=pc.fetchone()[0]
        if exists:
            pc.execute(f'SELECT COUNT(*) FROM "{t}"')
            if pc.fetchone()[0]>0: raise RuntimeError(f'PostgreSQL table {t} bo\'sh emas. Migration to\'xtatildi.')
    for t in tables:
        sql=sc.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone()[0]
        sql=re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b','BIGSERIAL PRIMARY KEY',sql,flags=re.I)
        sql=re.sub(r'\bAUTOINCREMENT\b','',sql,flags=re.I)
        pc.execute(sql)
    for t in tables:
        cols=[r['name'] for r in sc.execute(f'PRAGMA table_info("{t}")')]
        rows=sc.execute(f'SELECT * FROM "{t}"').fetchall()
        if rows:
            values=[tuple(r[c] for c in cols) for r in rows]
            q=f'INSERT INTO "{t}" ('+','.join(f'"{c}"' for c in cols)+') VALUES %s'
            execute_values(pc,q,values,page_size=1000)
        pc.execute(f'SELECT COUNT(*) FROM "{t}"'); dst=pc.fetchone()[0]
        if dst!=len(rows): raise RuntimeError(f'{t}: verification FAILED {len(rows)} != {dst}')
        print(f'OK {t}: {len(rows)} rows')
        # reset BIGSERIAL sequence if table has id sequence
        if 'id' in cols:
            try:
                pc.execute("SELECT pg_get_serial_sequence(%s,'id')",(t,)); seq=pc.fetchone()[0]
                if seq: pc.execute("SELECT setval(%s, COALESCE((SELECT MAX(id) FROM \"%s\"),1), true)" % ('%s',t),(seq,))
            except Exception: pg.rollback(); pg.autocommit=False; raise
    pg.commit(); print('MIGRATION SUCCESS: all table counts verified')
except Exception:
    pg.rollback(); raise
finally:
    src.close(); pg.close()
