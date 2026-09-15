# -*- coding: utf-8 -*-
"""
Quiz Pilot - P2P Payment AI Worker
SQLite remains MASTER. This module is deliberately isolated from Generation AI.

Payment OCR uses only PAYMENT_GEMINI_API_KEYS. It never falls back to
GOOGLE_API_KEYS, so paid Generation traffic cannot be affected by P2P receipts.
"""
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone, timedelta

from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field

UZ_TZ = timezone(timedelta(hours=5))

PAYMENT_GEMINI_API_KEYS = [
    k.strip() for k in os.getenv("PAYMENT_GEMINI_API_KEYS", "").split(",") if k.strip()
]
PAYMENT_GEMINI_MODEL = os.getenv("PAYMENT_GEMINI_MODEL", "gemini-2.5-flash").strip()
PAYMENT_OCR_MAX_CONCURRENT = max(1, int(os.getenv("PAYMENT_OCR_MAX_CONCURRENT", "2")))
PAYMENT_OCR_QUEUE_MAX = max(PAYMENT_OCR_MAX_CONCURRENT, int(os.getenv("PAYMENT_OCR_QUEUE_MAX", "100")))
PAYMENT_OCR_MAX_RETRIES = max(1, min(3, int(os.getenv("PAYMENT_OCR_MAX_RETRIES", "2"))))
PAYMENT_OCR_MAX_IMAGE_MB = max(1, int(os.getenv("PAYMENT_OCR_MAX_IMAGE_MB", "8")))
PAYMENT_OCR_DAILY_MAX_REQUESTS = max(1, int(os.getenv("PAYMENT_OCR_DAILY_MAX_REQUESTS", "200")))
PAYMENT_OCR_MONTHLY_MAX_REQUESTS = max(1, int(os.getenv("PAYMENT_OCR_MONTHLY_MAX_REQUESTS", "5000")))
PAYMENT_OCR_MONTHLY_BUDGET_USD = max(0.0, float(os.getenv("PAYMENT_OCR_MONTHLY_BUDGET_USD", "5")))
PAYMENT_OCR_INPUT_USD_PER_1M = max(0.0, float(os.getenv("PAYMENT_OCR_INPUT_USD_PER_1M", "0")))
PAYMENT_OCR_OUTPUT_USD_PER_1M = max(0.0, float(os.getenv("PAYMENT_OCR_OUTPUT_USD_PER_1M", "0")))

_queue = queue.Queue(maxsize=PAYMENT_OCR_QUEUE_MAX)
_semaphore = threading.BoundedSemaphore(PAYMENT_OCR_MAX_CONCURRENT)
_started = False
_start_lock = threading.Lock()
_key_lock = threading.Lock()
_key_index = 0
_db_path = None
_bot = None
_admin_id = None


class ReceiptAIResult(BaseModel):
    is_receipt: bool = False
    amount: float = 0
    currency: str = ""
    transaction_id: str = ""
    transaction_date: str = ""
    transaction_time: str = ""
    recipient_card_last4: str = ""
    recipient_name: str = ""
    bank_or_app: str = ""
    confidence: float = 0
    reason: str = ""


def configure(db_path, bot, admin_id):
    global _db_path, _bot, _admin_id
    _db_path = db_path
    _bot = bot
    _admin_id = admin_id


def _now():
    return int(time.time())


def _periods(ts=None):
    dt = datetime.fromtimestamp(ts or _now(), UZ_TZ)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")


def _connect():
    import sqlite3
    conn = sqlite3.connect(_db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = _connect()
    conn.execute("""CREATE TABLE IF NOT EXISTS p2p_receipts (
        receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        tx_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        telegram_file_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        amount REAL DEFAULT 0,
        currency TEXT DEFAULT '',
        transaction_id TEXT DEFAULT '',
        transaction_date TEXT DEFAULT '',
        transaction_time TEXT DEFAULT '',
        recipient_card_last4 TEXT DEFAULT '',
        recipient_name TEXT DEFAULT '',
        bank_or_app TEXT DEFAULT '',
        confidence REAL DEFAULT 0,
        reason TEXT DEFAULT '',
        ai_key_index INTEGER DEFAULT -1,
        ai_input_tokens INTEGER DEFAULT 0,
        ai_output_tokens INTEGER DEFAULT 0,
        estimated_cost_usd REAL DEFAULT 0,
        created_at INTEGER NOT NULL,
        processed_at INTEGER DEFAULT 0,
        UNIQUE(tx_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS p2p_ai_budget (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        day_key TEXT DEFAULT '',
        month_key TEXT DEFAULT '',
        daily_requests INTEGER DEFAULT 0,
        monthly_requests INTEGER DEFAULT 0,
        monthly_spend_usd REAL DEFAULT 0,
        updated_at INTEGER DEFAULT 0
    )""")
    conn.execute("INSERT OR IGNORE INTO p2p_ai_budget (id, updated_at) VALUES (1, ?)", (_now(),))
    # Safe schema migration for existing production databases.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(p2p_receipts)").fetchall()]
    if "attempts" not in cols:
        conn.execute("ALTER TABLE p2p_receipts ADD COLUMN attempts INTEGER DEFAULT 0")
    if "last_error" not in cols:
        conn.execute("ALTER TABLE p2p_receipts ADD COLUMN last_error TEXT DEFAULT ''")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_p2p_receipts_status ON p2p_receipts(status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_p2p_receipts_tx ON p2p_receipts(transaction_id)")
    conn.commit()
    conn.close()


def _budget_reserve():
    day, month = _periods()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute("SELECT * FROM p2p_ai_budget WHERE id=1").fetchone()
        if not row:
            cur.execute("INSERT INTO p2p_ai_budget (id,day_key,month_key) VALUES (1,?,?)", (day, month))
            daily = monthly = 0
            spend = 0.0
        else:
            daily = row["daily_requests"] or 0
            monthly = row["monthly_requests"] or 0
            spend = row["monthly_spend_usd"] or 0.0
            if row["day_key"] != day:
                daily = 0
            if row["month_key"] != month:
                monthly = 0
                spend = 0.0

        if daily >= PAYMENT_OCR_DAILY_MAX_REQUESTS:
            conn.rollback()
            return False, "daily_request_limit"
        if monthly >= PAYMENT_OCR_MONTHLY_MAX_REQUESTS:
            conn.rollback()
            return False, "monthly_request_limit"
        if PAYMENT_OCR_MONTHLY_BUDGET_USD > 0 and spend >= PAYMENT_OCR_MONTHLY_BUDGET_USD:
            conn.rollback()
            return False, "monthly_budget_limit"

        cur.execute("""UPDATE p2p_ai_budget
                       SET day_key=?, month_key=?, daily_requests=?, monthly_requests=?, updated_at=?
                       WHERE id=1""", (day, month, daily + 1, monthly + 1, _now()))
        conn.commit()
        return True, "ok"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _budget_add_cost(cost):
    if cost <= 0:
        return
    conn = _connect()
    try:
        conn.execute("UPDATE p2p_ai_budget SET monthly_spend_usd=COALESCE(monthly_spend_usd,0)+?, updated_at=? WHERE id=1", (cost, _now()))
        conn.commit()
    finally:
        conn.close()


def budget_status():
    day, month = _periods()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM p2p_ai_budget WHERE id=1").fetchone()
        if not row:
            return {"daily_requests": 0, "monthly_requests": 0, "monthly_spend_usd": 0.0}
        daily = row["daily_requests"] or 0
        monthly = row["monthly_requests"] or 0
        spend = row["monthly_spend_usd"] or 0.0
        if row["day_key"] != day:
            daily = 0
        if row["month_key"] != month:
            monthly = 0
            spend = 0.0
        return {
            "daily_requests": daily,
            "daily_limit": PAYMENT_OCR_DAILY_MAX_REQUESTS,
            "monthly_requests": monthly,
            "monthly_request_limit": PAYMENT_OCR_MONTHLY_MAX_REQUESTS,
            "monthly_spend_usd": spend,
            "monthly_budget_usd": PAYMENT_OCR_MONTHLY_BUDGET_USD,
        }
    finally:
        conn.close()


def enqueue_receipt(tx_id, user_id, file_id):
    conn = _connect()
    try:
        row = conn.execute("SELECT status FROM p2p_receipts WHERE tx_id=?", (tx_id,)).fetchone()
        if row and row["status"] in ("analyzed", "approved", "rejected", "review"):
            return False, "already_processed"
        conn.execute("INSERT OR IGNORE INTO p2p_receipts (tx_id,user_id,telegram_file_id,status,created_at) VALUES (?,?,?,?,?)",
                     (tx_id, user_id, file_id, "queued", _now()))
        conn.execute("UPDATE p2p_receipts SET telegram_file_id=?, status='queued', reason='', last_error='' WHERE tx_id=? AND status IN ('waiting_capacity','error')",
                     (file_id, tx_id))
        conn.commit()
    finally:
        conn.close()
    try:
        _queue.put_nowait((tx_id, int(user_id), file_id))
        return True, "queued"
    except queue.Full:
        conn = _connect()
        try:
            conn.execute("UPDATE p2p_receipts SET status='waiting_capacity', reason=? WHERE tx_id=?", ("OCR queue is full", tx_id))
            conn.commit()
        finally:
            conn.close()
        return False, "queue_full"


def _next_key():
    global _key_index
    if not PAYMENT_GEMINI_API_KEYS:
        return -1, None
    with _key_lock:
        idx = _key_index % len(PAYMENT_GEMINI_API_KEYS)
        _key_index = (_key_index + 1) % len(PAYMENT_GEMINI_API_KEYS)
    return idx, PAYMENT_GEMINI_API_KEYS[idx]


def _extract_usage(response):
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return 0, 0
    inp = getattr(usage, "prompt_token_count", 0) or 0
    out = getattr(usage, "candidates_token_count", 0) or 0
    return int(inp), int(out)


def _estimate_cost(inp, out):
    return (inp / 1_000_000) * PAYMENT_OCR_INPUT_USD_PER_1M + (out / 1_000_000) * PAYMENT_OCR_OUTPUT_USD_PER_1M


def _parse_response(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("Gemini bo'sh javob qaytardi")
    try:
        return ReceiptAIResult.model_validate_json(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return ReceiptAIResult.model_validate(json.loads(text[start:end + 1]))
        raise


def _vision(file_bytes, mime_type):
    if not PAYMENT_GEMINI_API_KEYS:
        raise RuntimeError("PAYMENT_GEMINI_API_KEYS sozlanmagan")
    prompt = """
You analyze a payment receipt image. Extract only visible facts; never invent missing values.
Return JSON matching the requested schema.
Rules:
- amount: numeric payment amount, 0 if unknown.
- currency: visible currency, e.g. UZS.
- transaction_id: visible transaction/operation/reference ID; empty if absent.
- transaction_date/time: visible date/time as text.
- recipient_card_last4: ONLY the last 4 digits of the recipient card if visible; empty otherwise.
- recipient_name and bank_or_app: visible values only.
- confidence: 0..1 reflecting extraction confidence, not proof of real payment.
- is_receipt=false if this does not clearly look like a payment receipt.
- reason: concise explanation of missing/ambiguous data.
Do not claim that a receipt is authentic. OCR is not bank verification.
"""
    last_error = None
    for attempt in range(PAYMENT_OCR_MAX_RETRIES):
        key_idx, api_key = _next_key()
        if not api_key:
            break
        acquired = _semaphore.acquire(timeout=120)
        if not acquired:
            last_error = "OCR concurrency limit"
            continue
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=PAYMENT_GEMINI_MODEL,
                contents=[prompt, genai_types.Part.from_bytes(data=file_bytes, mime_type=mime_type)],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ReceiptAIResult,
                    temperature=0,
                ),
            )
            result = _parse_response(response.text)
            inp, out = _extract_usage(response)
            return result, key_idx, inp, out
        except Exception as exc:
            last_error = str(exc)
            logging.warning("P2P Gemini Vision xatosi | key=%s | attempt=%s | %s", key_idx, attempt + 1, exc)
            time.sleep(min(4, 0.75 * (2 ** attempt)))
        finally:
            _semaphore.release()
    raise RuntimeError(last_error or "Gemini Vision ishlamadi")


def _mark(receipt_id, **fields):
    if not fields:
        return
    conn = _connect()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [receipt_id]
        conn.execute(f"UPDATE p2p_receipts SET {sets} WHERE receipt_id=?", vals)
        conn.commit()
    finally:
        conn.close()


def _payment_row(tx_id):
    conn = _connect()
    try:
        return conn.execute("SELECT tx_id,user_id,tariff_name,tariff_price,status FROM payments WHERE tx_id=?", (tx_id,)).fetchone()
    finally:
        conn.close()


def _parse_amount(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def _expected_amount(price):
    digits = "".join(ch for ch in (price or "") if ch.isdigit())
    return float(digits) if digits else 0.0


def _notify_user(user_id, text):
    try:
        if _bot:
            _bot.send_message(user_id, text)
    except Exception as exc:
        logging.warning("P2P user notification failed: %s", exc)


def _notify_admin(text):
    try:
        if _bot and _admin_id:
            _bot.send_message(_admin_id, text)
    except Exception as exc:
        logging.warning("P2P admin notification failed: %s", exc)


def _approve(tx_id, user_id):
    """Reuse the same Premium semantics as existing manual approval, but atomically."""
    import sqlite3
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        pay = cur.execute("SELECT status, tariff_name FROM payments WHERE tx_id=? AND user_id=?", (tx_id, user_id)).fetchone()
        if not pay or pay["status"] != "pending":
            return False, "payment_not_pending"
        tariff_name = pay["tariff_name"] or ""
        low = tariff_name.lower()
        if "o'qit" in low or "учител" in low or "teacher" in low:
            plan = "teachers"
        elif "haft" in low or "недель" in low or "weekly" in low or "7" in low:
            plan = "weekly"
        elif "oy" in low or "месяч" in low or "monthly" in low or "30" in low:
            plan = "monthly"
        else:
            plan = "daily"
        durations = {"daily": 86400, "weekly": 7*86400, "monthly": 30*86400, "teachers": 30*86400}
        now = _now()
        old = cur.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()
        old_until = int(old[0] or 0) if old else 0
        until = max(now, old_until) + durations[plan]
        cur.execute("UPDATE payments SET status='approved' WHERE tx_id=? AND status='pending'", (tx_id,))
        if cur.rowcount != 1:
            return False, "payment_race"
        # Localized display is handled by the existing status endpoint. Store canonical plan.
        cur.execute("UPDATE users SET status=?, plan_key=?, premium_until=? WHERE user_id=?",
                    (f"PRO ✨ ({plan})", plan, until, user_id))
        conn.commit()
        return True, plan
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim(tx_id):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute("SELECT status, attempts FROM p2p_receipts WHERE tx_id=?", (tx_id,)).fetchone()
        if not row or row["status"] not in ("queued", "waiting_capacity", "error"):
            conn.rollback()
            return False
        cur.execute("UPDATE p2p_receipts SET status='processing', attempts=COALESCE(attempts,0)+1, reason='', last_error='' WHERE tx_id=?", (tx_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _recover_queued():
    conn = _connect()
    try:
        rows = conn.execute("SELECT tx_id,user_id,telegram_file_id FROM p2p_receipts WHERE status IN ('queued','waiting_capacity') ORDER BY created_at LIMIT ?", (PAYMENT_OCR_QUEUE_MAX,)).fetchall()
    finally:
        conn.close()
    recovered = 0
    for row in rows:
        try:
            _queue.put_nowait((row["tx_id"], int(row["user_id"]), row["telegram_file_id"]))
            recovered += 1
        except queue.Full:
            break
    if recovered:
        logging.info("P2P OCR recovery: %s queued receipt(s) restored after restart", recovered)


def _process(item):
    tx_id, user_id, file_id = item
    if not _claim(tx_id):
        return
    pay = _payment_row(tx_id)
    if not pay or pay["status"] != "pending":
        _mark_by_tx(tx_id, status="cancelled", processed_at=_now(), reason="Payment is no longer pending")
        return

    allowed, reason = _budget_reserve()
    if not allowed:
        _mark_by_tx(tx_id, status="budget_blocked", reason=reason)
        _notify_admin(f"⚠️ P2P AI budget/limitga yetdi. TX: {tx_id}\nSabab: {reason}\nTo'lov avtomatik tasdiqlanmadi.")
        return

    try:
        tg_file = _bot.get_file(file_id)
        data = _bot.download_file(tg_file.file_path)
        if len(data) > PAYMENT_OCR_MAX_IMAGE_MB * 1024 * 1024:
            raise RuntimeError("Chek rasmi belgilangan hajm limitidan katta")
        mime = "image/jpeg"
        if str(tg_file.file_path).lower().endswith(".png"):
            mime = "image/png"

        result, key_idx, inp, out = _vision(data, mime)
        cost = _estimate_cost(inp, out)
        _budget_add_cost(cost)
        _mark_by_tx(tx_id, status="analyzed", amount=result.amount, currency=result.currency,
                     transaction_id=result.transaction_id, transaction_date=result.transaction_date,
                     transaction_time=result.transaction_time, recipient_card_last4=result.recipient_card_last4,
                     recipient_name=result.recipient_name, bank_or_app=result.bank_or_app,
                     confidence=result.confidence, reason=result.reason, ai_key_index=key_idx,
                     ai_input_tokens=inp, ai_output_tokens=out, estimated_cost_usd=cost, processed_at=_now())

        expected = _expected_amount(pay["tariff_price"])
        amount_ok = abs(_parse_amount(result.amount) - expected) < 0.01
        conf_ok = result.confidence >= 0.85
        receipt_ok = bool(result.is_receipt)
        txid_ok = bool(result.transaction_id.strip())
        if not receipt_ok or not amount_ok or not conf_ok or not txid_ok:
            _mark_by_tx(tx_id, status="review", reason=(result.reason or "AI tekshiruvi yetarli emas"))
            _notify_admin(
                f"⚠️ P2P TO'LOV — QO'CHIMCHA TEKSHIRUV KERAK\n\n"
                f"TX: {tx_id}\nUser: {user_id}\n"
                f"Kutilgan summa: {pay['tariff_price']}\nAI summa: {result.amount}\n"
                f"Confidence: {result.confidence:.2f}\nTransaction ID: {result.transaction_id or 'yo-q'}\n"
                f"Sabab: {result.reason or 'Qoidaga mos kelmadi'}"
            )
            return

        # Prevent reusing the same transaction identifier across different receipts.
        conn = _connect()
        try:
            duplicate = conn.execute("SELECT tx_id FROM p2p_receipts WHERE transaction_id=? AND tx_id<>? AND status IN ('analyzed','approved') LIMIT 1",
                                     (result.transaction_id.strip(), tx_id)).fetchone()
        finally:
            conn.close()
        if duplicate:
            _mark_by_tx(tx_id, status="rejected", reason="Transaction ID avval ishlatilgan")
            _notify_user(user_id, "❌ Bu to'lov cheki avval ishlatilgan. Tarif avtomatik faollashtirilmadi.")
            return

        ok, info = _approve(tx_id, user_id)
        if ok:
            _mark_by_tx(tx_id, status="approved", reason="AI rules passed", processed_at=_now())
            _notify_user(user_id, "🎉 To'lov tasdiqlandi! PRO status avtomatik faollashtirildi. 👑")
            logging.info("P2P payment auto-approved | tx=%s | user=%s | cost=$%.6f", tx_id, user_id, cost)
        else:
            _mark_by_tx(tx_id, status="review", reason=info)
    except Exception as exc:
        _mark_by_tx(tx_id, status="error", reason=str(exc), processed_at=_now())
        _notify_admin(f"⚠️ P2P AI xatosi\nTX: {tx_id}\nUser: {user_id}\n{str(exc)[:800]}")


def _mark_by_tx(tx_id, **fields):
    conn = _connect()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE p2p_receipts SET {sets} WHERE tx_id=?", list(fields.values()) + [tx_id])
        conn.commit()
    finally:
        conn.close()


def _worker():
    logging.info("P2P OCR worker started | model=%s | concurrency=%s | queue=%s | keys=%s",
                 PAYMENT_GEMINI_MODEL, PAYMENT_OCR_MAX_CONCURRENT, PAYMENT_OCR_QUEUE_MAX, len(PAYMENT_GEMINI_API_KEYS))
    while True:
        item = _queue.get()
        try:
            _process(item)
        except Exception:
            logging.exception("P2P OCR worker unexpected error")
        finally:
            _queue.task_done()


def start():
    global _started
    if _started:
        return
    if not _db_path:
        raise RuntimeError("p2p_payment_ai.configure() chaqirilmagan")
    init_db()
    with _start_lock:
        if _started:
            return
        for idx in range(PAYMENT_OCR_MAX_CONCURRENT):
            threading.Thread(target=_worker, name=f"p2p-ocr-{idx+1}", daemon=True).start()
        _started = True
        _recover_queued()


def get_status():
    return {
        "configured": bool(PAYMENT_GEMINI_API_KEYS),
        "model": PAYMENT_GEMINI_MODEL,
        "queue_size": _queue.qsize(),
        "queue_limit": PAYMENT_OCR_QUEUE_MAX,
        "budget": budget_status(),
    }
