#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quiz AI SQLite backup/verify/restore yordamchisi.

Restore faqat ilova/workerlar to'liq to'xtatilgan paytda bajarilishi kerak.
Bu modul FastAPI orqali ochiq endpoint yaratmaydi.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_TABLES = {"users", "quizzes", "payments"}


def default_db_path() -> Path:
    railway_path = Path("/data/quiz_pilot_v2.db")
    return railway_path if Path("/data").exists() else Path("quiz_pilot_v2.db")


def connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def validate_database(path: Path, full: bool = True) -> tuple[bool, str]:
    if not path.is_file() or path.stat().st_size == 0:
        return False, "Fayl topilmadi yoki bo'sh."
    conn = None
    try:
        conn = connect_readonly(path)
        pragma = "integrity_check" if full else "quick_check"
        rows = conn.execute(f"PRAGMA {pragma}").fetchall()
        if not rows or any(row[0] != "ok" for row in rows):
            return False, f"PRAGMA {pragma} muvaffaqiyatsiz: {rows[:5]}"
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            return False, "Majburiy jadvallar topilmadi: " + ", ".join(sorted(missing))
        return True, "OK"
    except sqlite3.Error as exc:
        return False, f"SQLite tekshiruvi xatosi: {exc}"
    finally:
        if conn is not None:
            conn.close()


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Mavjud fayl ustiga yozilmaydi: {destination}")
    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(destination), timeout=30)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()
    finally:
        dst.close()
        src.close()
    with destination.open("rb") as file_obj:
        os.fsync(file_obj.fileno())


def command_backup(args: argparse.Namespace) -> int:
    db = Path(args.db).resolve()
    if not db.is_file():
        print(f"XATO: baza topilmadi: {db}", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(args.output).resolve() if args.output else db.parent / "backups" / f"quiz_ai_{stamp}.db"
    try:
        sqlite_backup(db, output)
        ok, detail = validate_database(output, full=True)
        if not ok:
            output.unlink(missing_ok=True)
            print(f"XATO: backup tekshiruvdan o'tmadi: {detail}", file=sys.stderr)
            return 3
        print(f"BACKUP_OK: {output}")
        print(f"SIZE_BYTES: {output.stat().st_size}")
        return 0
    except Exception as exc:
        print(f"XATO: backup yaratilmadi: {exc}", file=sys.stderr)
        return 4


def command_verify(args: argparse.Namespace) -> int:
    path = Path(args.file).resolve()
    ok, detail = validate_database(path, full=True)
    if ok:
        print(f"VERIFY_OK: {path}")
        print(f"SIZE_BYTES: {path.stat().st_size}")
        return 0
    print(f"VERIFY_FAILED: {detail}", file=sys.stderr)
    return 5


def command_restore(args: argparse.Namespace) -> int:
    db = Path(args.db).resolve()
    source = Path(args.from_file).resolve()
    if not args.confirm_restore:
        print("XATO: restore uchun --confirm-restore majburiy.", file=sys.stderr)
        return 6
    if source == db or not source.is_file():
        print("XATO: backup fayli noto'g'ri yoki asosiy baza bilan bir xil.", file=sys.stderr)
        return 7
    ok, detail = validate_database(source, full=True)
    if not ok:
        print(f"XATO: backup yaroqsiz: {detail}", file=sys.stderr)
        return 8
    if not db.is_file():
        print("XATO: joriy baza topilmadi; avtomatik yangi baza yaratish taqiqlangan.", file=sys.stderr)
        return 9

    # Restore vaqtida ilova va barcha workerlar to'liq to'xtatilgan bo'lishi shart.
    temp_restore = db.with_name(db.name + ".restore.tmp")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safety_copy = db.with_name(f"{db.name}.pre_restore_{stamp}.db")
    for path in (temp_restore, safety_copy):
        if path.exists():
            print(f"XATO: vaqtinchalik/xavfsizlik fayli allaqachon mavjud: {path}", file=sys.stderr)
            return 10

    live = None
    try:
        # WAL'ni checkpoint qilish: busy bo'lsa restore xavfsiz to'xtatiladi.
        live = sqlite3.connect(str(db), timeout=5)
        checkpoint = live.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and int(checkpoint[0]) != 0:
            raise RuntimeError("WAL checkpoint band: boshqa DB ulanishlari hali faol bo'lishi mumkin.")
        live.close()
        live = None

        sqlite_backup(source, temp_restore)
        ok, detail = validate_database(temp_restore, full=True)
        if not ok:
            raise RuntimeError(f"Restore vaqtinchalik nusxasi yaroqsiz: {detail}")

        # Joriy bazaning alohida rollback nusxasi yaratiladi.
        sqlite_backup(db, safety_copy)
        ok, detail = validate_database(safety_copy, full=True)
        if not ok:
            raise RuntimeError(f"Restore oldi backup yaroqsiz: {detail}")

        # Eski WAL/SHM yangi DB fayliga ulanib qolmasligi uchun olib tashlanadi.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db) + suffix)
            if sidecar.exists():
                sidecar.unlink()

        os.replace(temp_restore, db)
        try:
            dir_fd = os.open(str(db.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

        ok, detail = validate_database(db, full=True)
        if not ok:
            raise RuntimeError(f"Tiklangan baza yakuniy tekshiruvdan o'tmadi: {detail}. Xavfsizlik nusxasi: {safety_copy}")
        print(f"RESTORE_OK: {db}")
        print(f"PRE_RESTORE_BACKUP: {safety_copy}")
        return 0
    except Exception as exc:
        print(f"RESTORE_FAILED: {exc}", file=sys.stderr)
        if temp_restore.exists():
            temp_restore.unlink(missing_ok=True)
        print("Asosiy bazani qo'lda tekshiring. Restore oldi nusxasi yaratilgan bo'lsa, u saqlanadi.", file=sys.stderr)
        return 11
    finally:
        if live is not None:
            live.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quiz AI SQLite backup / verify / restore")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="Izchil backup yaratish")
    backup.add_argument("--db", default=str(default_db_path()))
    backup.add_argument("--output", help="Ixtiyoriy backup fayl yo'li")
    backup.set_defaults(func=command_backup)

    verify = sub.add_parser("verify", help="Backup yaxlitligini tekshirish")
    verify.add_argument("file")
    verify.set_defaults(func=command_verify)

    restore = sub.add_parser("restore", help="Backup'dan tiklash (ilova to'xtatilgan bo'lishi shart)")
    restore.add_argument("--db", default=str(default_db_path()))
    restore.add_argument("--from", dest="from_file", required=True)
    restore.add_argument("--confirm-restore", action="store_true")
    restore.set_defaults(func=command_restore)
    return parser


if __name__ == "__main__":
    arguments = build_parser().parse_args()
    raise SystemExit(arguments.func(arguments))
