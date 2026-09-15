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
import hashlib
import re

from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field

UZ_TZ = timezone(timedelta(hours=5))

PAYMENT_GEMINI_API_KEYS = [
    k.strip() for k in os.getenv("PAYMENT_GEMINI_API_KEYS", "").split(",") if k.strip()
]
PAYMENT_GEMINI_MODEL = os.getenv("PAYMENT_GEMINI_MODEL", "gemini-3.6-flash").strip()
PAYMENT_GEMINI_FALLBACK_MODELS = [
    m.strip() for m in os.getenv("PAYMENT_GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()
]
PAYMENT_OCR_MAX_CONCURRENT = max(1, int(os.getenv("PAYMENT_OCR_MAX_CONCURRENT", "2")))
PAYMENT_OCR_QUEUE_MAX = max(PAYMENT_OCR_MAX_CONCURRENT, int(os.getenv("PAYMENT_OCR_QUEUE_MAX", "100")))
PAYMENT_OCR_MAX_RETRIES = max(1, min(6, int(os.getenv("PAYMENT_OCR_MAX_RETRIES", "4"))))
PAYMENT_OCR_RETRY_BASE_SECONDS = max(5, int(os.getenv("PAYMENT_OCR_RETRY_BASE_SECONDS", "30")))
PAYMENT_OCR_RETRY_MAX_SECONDS = max(PAYMENT_OCR_RETRY_BASE_SECONDS, int(os.getenv("PAYMENT_OCR_RETRY_MAX_SECONDS", "300")))
PAYMENT_OCR_RETRY_SCAN_SECONDS = max(5, int(os.getenv("PAYMENT_OCR_RETRY_SCAN_SECONDS", "10")))
PAYMENT_OCR_PROCESSING_STALE_SECONDS = max(60, int(os.getenv("PAYMENT_OCR_PROCESSING_STALE_SECONDS", "900")))
P2P_CARD_NUMBER = os.getenv("P2P_CARD_NUMBER", "").strip()
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
        next_retry_at INTEGER DEFAULT 0,
        image_sha256 TEXT DEFAULT '',
        attempts INTEGER DEFAULT 0,
        last_error TEXT DEFAULT '',
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
    if "next_retry_at" not in cols:
        conn.execute("ALTER TABLE p2p_receipts ADD COLUMN next_retry_at INTEGER DEFAULT 0")
    if "image_sha256" not in cols:
        conn.execute("ALTER TABLE p2p_receipts ADD COLUMN image_sha256 TEXT DEFAULT ''")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_p2p_receipts_status ON p2p_receipts(status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_p2p_receipts_retry ON p2p_receipts(status, next_retry_at)")
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
        conn.execute("INSERT OR IGNORE INTO p2p_receipts (tx_id,user_id,telegram_file_id,status,created_at,next_retry_at) VALUES (?,?,?,?,?,0)",
                     (tx_id, user_id, file_id, "queued", _now()))
        conn.execute("UPDATE p2p_receipts SET telegram_file_id=?, status='queued', reason='', last_error='', next_retry_at=0 WHERE tx_id=? AND status IN ('waiting_capacity','error','retry_wait','budget_blocked')",
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


def _is_transient_error(exc):
    text = str(exc).upper()
    return any(token in text for token in (
        "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "502", "504",
        "DEADLINE_EXCEEDED", "TIMEOUT", "TIMED OUT", "TEMPORARY"
    ))


def _retry_delay(attempt):
    # Persistent exponential backoff: 30s, 60s, 120s, 240s... capped at 5m by default.
    return min(PAYMENT_OCR_RETRY_MAX_SECONDS, PAYMENT_OCR_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))


def _vision_once(file_bytes, mime_type, model, api_key):
    prompt = """
You analyze a payment receipt image. Extract only visible facts; never invent missing values.
Return JSON matching the requested schema.
Rules:
- amount: numeric payment amount, 0 if unknown.
- currency: visible currency, e.g. UZS.
- transaction_id: visible bank transaction/operation/reference ID; empty if absent.
- transaction_date/time: visible date/time as text.
- recipient_card_last4: ONLY the last 4 digits of the recipient card if visible; empty otherwise.
- recipient_name and bank_or_app: visible values only.
- confidence: 0..1 reflecting extraction confidence, not proof of real payment.
- is_receipt=false if this does not clearly look like a payment receipt.
- reason: concise explanation of missing/ambiguous data.
Do not claim that a receipt is authentic. OCR is not bank verification.
"""
    client = genai.Client(api_key=api_key)
    return client.models.generate_content(
        model=model,
        contents=[prompt, genai_types.Part.from_bytes(data=file_bytes, mime_type=mime_type)],
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ReceiptAIResult,
            temperature=0,
        ),
    )


def _vision(file_bytes, mime_type, attempt_number):
    if not PAYMENT_GEMINI_API_KEYS:
        raise RuntimeError("PAYMENT_GEMINI_API_KEYS sozlanmagan")

    models = [PAYMENT_GEMINI_MODEL] + PAYMENT_GEMINI_FALLBACK_MODELS
    last_error = None
    for model in models:
        # Rotate keys for every model/attempt. With multiple keys this avoids repeatedly
        # hitting the same provider key during a transient outage.
        key_idx, api_key = _next_key()
        if not api_key:
            continue
        acquired = _semaphore.acquire(timeout=120)
        if not acquired:
            last_error = RuntimeError("OCR concurrency limit")
            continue
        try:
            response = _vision_once(file_bytes, mime_type, model, api_key)
            result = _parse_response(response.text)
            inp, out = _extract_usage(response)
            return result, key_idx, inp, out, model
        except Exception as exc:
            last_error = exc
            logging.warning(
                "P2P Gemini Vision xatosi | model=%s | key=%s | receipt_attempt=%s | %s",
                model, key_idx, attempt_number, exc
            )
            if not _is_transient_error(exc):
                raise
        finally:
            _semaphore.release()
    raise RuntimeError(str(last_error or "Gemini Vision ishlamadi"))


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
        cur.execute("UPDATE users SET status=?, plan_key=?, premium_until=?, premium_source=? WHERE user_id=?",
                    (f"PRO ✨ ({plan})", plan, until, "paid", user_id))
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
        row = cur.execute("SELECT status, attempts, next_retry_at FROM p2p_receipts WHERE tx_id=?", (tx_id,)).fetchone()
        if not row or row["status"] not in ("queued", "waiting_capacity", "error", "retry_wait", "budget_blocked") or int(row["next_retry_at"] or 0) > _now():
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


def _recover_queued(force=False):
    conn = _connect()
    try:
        now = _now()
        # If the container died while a receipt was processing, leave it recoverable.
        # This is deliberately time-based so a live worker is not duplicated.
        stale_before = now - PAYMENT_OCR_PROCESSING_STALE_SECONDS
        conn.execute(
            "UPDATE p2p_receipts SET status='retry_wait', reason='Worker restart recovery', next_retry_at=? "
            "WHERE status='processing' AND created_at<=? AND (processed_at IS NULL OR processed_at=0)",
            (now, stale_before)
        )
        conn.commit()
        # Recover all durable states that are safe to retry. In particular,
        # older production receipts may have been left in `error` by the
        # previous P2P worker version; those must not be stranded forever.
        statuses = "('queued','waiting_capacity','retry_wait','budget_blocked','error')" if force else "('waiting_capacity','retry_wait','budget_blocked','error')"
        rows = conn.execute(
            f"SELECT receipt_id,tx_id,user_id,telegram_file_id,status FROM p2p_receipts "
            f"WHERE status IN {statuses} AND (next_retry_at IS NULL OR next_retry_at<=?) "
            f"ORDER BY created_at LIMIT ?",
            (now, PAYMENT_OCR_QUEUE_MAX)
        ).fetchall()
    finally:
        conn.close()

    recovered = 0
    for row in rows:
        # For periodic retries, atomically mark a row queued only when we are about
        # to put it into the in-memory queue. This prevents duplicate enqueueing.
        if not force:
            conn = _connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "UPDATE p2p_receipts SET status='queued', next_retry_at=0 WHERE receipt_id=? AND status=? AND (next_retry_at IS NULL OR next_retry_at<=?)",
                    (row["receipt_id"], row["status"], now)
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    continue
                try:
                    _queue.put_nowait((row["tx_id"], int(row["user_id"]), row["telegram_file_id"]))
                except queue.Full:
                    conn.rollback()
                    break
                conn.commit()
                recovered += 1
            finally:
                conn.close()
        else:
            conn = _connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "UPDATE p2p_receipts SET status='queued', next_retry_at=0 WHERE receipt_id=? AND status=? AND (next_retry_at IS NULL OR next_retry_at<=?)",
                    (row["receipt_id"], row["status"], now)
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    continue
                try:
                    _queue.put_nowait((row["tx_id"], int(row["user_id"]), row["telegram_file_id"]))
                except queue.Full:
                    conn.rollback()
                    break
                conn.commit()
                recovered += 1
            finally:
                conn.close()
    if recovered:
        logging.info("P2P OCR recovery: %s receipt(s) restored to worker queue", recovered)
    return recovered


def _retry_scheduler():
    while True:
        try:
            _recover_queued(force=False)
        except Exception:
            logging.exception("P2P OCR retry scheduler error")
        time.sleep(PAYMENT_OCR_RETRY_SCAN_SECONDS)


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
        # Do not lose a valid customer's receipt when the AI budget is exhausted.
        # Keep it persistent and retry automatically; never ask the customer to pay again.
        _mark_by_tx(tx_id, status="budget_blocked", reason=reason, next_retry_at=_now() + 3600)
        if _get_attempts(tx_id) == 1:
            _notify_user(user_id, "⏳ Chekingiz saqlandi. AI tekshiruv limiti vaqtincha to'ldi; tizim avtomatik qayta tekshiradi. Sizdan qayta to'lov talab qilinmaydi.")
        logging.warning("P2P OCR budget blocked | tx=%s | user=%s | reason=%s", tx_id, user_id, reason)
        return

    try:
        tg_file = _bot.get_file(file_id)
        data = _bot.download_file(tg_file.file_path)
        if len(data) > PAYMENT_OCR_MAX_IMAGE_MB * 1024 * 1024:
            raise ValueError("Chek rasmi belgilangan hajm limitidan katta")
        mime = "image/jpeg"
        if str(tg_file.file_path).lower().endswith(".png"):
            mime = "image/png"

        image_hash = hashlib.sha256(data).hexdigest()
        conn = _connect()
        try:
            duplicate_image = conn.execute(
                "SELECT tx_id FROM p2p_receipts WHERE image_sha256=? AND tx_id<>? AND status IN ('processing','analyzed','approved','review','rejected') LIMIT 1",
                (image_hash, tx_id)
            ).fetchone()
        finally:
            conn.close()
        if duplicate_image:
            _mark_by_tx(tx_id, status="rejected", reason="Aynan shu chek rasmi avval yuborilgan", image_sha256=image_hash, processed_at=_now())
            _notify_user(user_id, "❌ Bu chek rasmi avval yuborilgan. Tarif avtomatik faollashtirilmadi.")
            return
        _mark_by_tx(tx_id, image_sha256=image_hash)

        current_attempt = _get_attempts(tx_id)
        result, key_idx, inp, out, used_model = _vision(data, mime, current_attempt)
        cost = _estimate_cost(inp, out)
        _budget_add_cost(cost)
        _mark_by_tx(tx_id, status="analyzed", amount=result.amount, currency=result.currency,
                     transaction_id=result.transaction_id, transaction_date=result.transaction_date,
                     transaction_time=result.transaction_time, recipient_card_last4=result.recipient_card_last4,
                     recipient_name=result.recipient_name, bank_or_app=result.bank_or_app,
                     confidence=result.confidence, reason=result.reason, ai_key_index=key_idx,
                     ai_input_tokens=inp, ai_output_tokens=out, estimated_cost_usd=cost,
                     processed_at=_now(), next_retry_at=0)

        expected = _expected_amount(pay["tariff_price"])
        amount_ok = abs(_parse_amount(result.amount) - expected) < 0.01
        conf_ok = result.confidence >= 0.85
        receipt_ok = bool(result.is_receipt)
        txid_ok = bool(result.transaction_id.strip())
        expected_card = _last4(P2P_CARD_NUMBER)
        actual_card = _last4(result.recipient_card_last4)
        card_ok = bool(expected_card) and bool(actual_card) and expected_card == actual_card
        if not receipt_ok or not amount_ok or not conf_ok or not txid_ok or not card_ok:
            reasons = []
            if not receipt_ok: reasons.append("chek aniqlanmadi")
            if not amount_ok: reasons.append(f"summa mos emas: {result.amount} != {expected}")
            if not conf_ok: reasons.append(f"confidence past: {result.confidence:.2f}")
            if not txid_ok: reasons.append("transaction ID topilmadi")
            if not card_ok: reasons.append("qabul qiluvchi karta oxirgi 4 raqami mos emas yoki ko'rinmadi")
            reason_text = "; ".join(reasons) or result.reason or "AI tekshiruvi yetarli emas"
            _mark_by_tx(tx_id, status="review", reason=reason_text)
            _notify_admin(
                f"⚠️ P2P TO'LOV — QO'SHIMCHA TEKSHIRUV KERAK\n\n"
                f"TX: {tx_id}\nUser: {user_id}\n"
                f"Kutilgan summa: {pay['tariff_price']}\nAI summa: {result.amount}\n"
                f"Karta: {actual_card or 'yo-q'} / kutilgan oxiri: {expected_card or 'sozlanmagan'}\n"
                f"Confidence: {result.confidence:.2f}\nTransaction ID: {result.transaction_id or 'yo-q'}\n"
                f"Model: {used_model}\nSabab: {reason_text}"
            )
            return

        conn = _connect()
        try:
            duplicate = conn.execute(
                "SELECT tx_id FROM p2p_receipts WHERE transaction_id=? AND tx_id<>? AND status IN ('analyzed','approved','review') LIMIT 1",
                (result.transaction_id.strip(), tx_id)
            ).fetchone()
        finally:
            conn.close()
        if duplicate:
            _mark_by_tx(tx_id, status="rejected", reason="Transaction ID avval ishlatilgan")
            _notify_user(user_id, "❌ Bu to'lov cheki avval ishlatilgan. Tarif avtomatik faollashtirilmadi.")
            return

        ok, info = _approve(tx_id, user_id)
        if ok:
            _mark_by_tx(tx_id, status="approved", reason="AI rules passed", processed_at=_now(), next_retry_at=0)
            _notify_user(user_id, "🎉 To'lov tasdiqlandi! PRO status avtomatik faollashtirildi. 👑")
            logging.info("P2P payment auto-approved | tx=%s | user=%s | model=%s | cost=$%.6f", tx_id, user_id, used_model, cost)
        else:
            _mark_by_tx(tx_id, status="review", reason=info)
    except Exception as exc:
        attempts = _get_attempts(tx_id)
        transient = _is_transient_error(exc)
        if transient and attempts < PAYMENT_OCR_MAX_RETRIES:
            delay = _retry_delay(attempts)
            _mark_by_tx(tx_id, status="retry_wait", reason="Vaqtinchalik AI xatosi; avtomatik qayta uriniladi", last_error=str(exc), next_retry_at=_now() + delay)
            logging.warning("P2P OCR temporary failure | tx=%s | attempt=%s/%s | retry_in=%ss | %s", tx_id, attempts, PAYMENT_OCR_MAX_RETRIES, delay, exc)
            if attempts == 1:
                _notify_user(user_id, "⏳ Chekingiz saqlandi. AI xizmati vaqtincha band, tekshiruv avtomatik qayta uriniladi. Sizdan qayta to'lov talab qilinmaydi.")
        else:
            # Keep the receipt recoverable rather than losing a real customer's payment.
            # After the normal retry budget is exhausted, back off for 5 minutes and
            # continue automatically; no repeated admin spam for a temporary outage.
            delay = PAYMENT_OCR_RETRY_MAX_SECONDS
            _mark_by_tx(tx_id, status="retry_wait" if transient else "error", reason=str(exc)[:1000], last_error=str(exc)[:2000], next_retry_at=_now() + delay)
            if not transient or attempts == PAYMENT_OCR_MAX_RETRIES:
                _notify_admin(f"⚠️ P2P AI xatosi\nTX: {tx_id}\nUser: {user_id}\nAttempt: {attempts}\n{str(exc)[:800]}")


def _get_attempts(tx_id):
    conn = _connect()
    try:
        row = conn.execute("SELECT attempts FROM p2p_receipts WHERE tx_id=?", (tx_id,)).fetchone()
        return int(row["attempts"] or 0) if row else 0
    finally:
        conn.close()


def _last4(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-4:] if len(digits) >= 4 else ""


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
        _recover_queued(force=True)
        threading.Thread(target=_retry_scheduler, name="p2p-ocr-retry-scheduler", daemon=True).start()


def get_status():
    return {
        "configured": bool(PAYMENT_GEMINI_API_KEYS),
        "model": PAYMENT_GEMINI_MODEL,
        "fallback_models": PAYMENT_GEMINI_FALLBACK_MODELS,
        "queue_size": _queue.qsize(),
        "queue_limit": PAYMENT_OCR_QUEUE_MAX,
        "budget": budget_status(),
    }
