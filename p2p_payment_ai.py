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

import telebot
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field

UZ_TZ = timezone(timedelta(hours=5))

# P2P OCR API kalitlari Generation AI kalitlaridan mustaqil ishlaydi.
# Eski usul saqlanadi: PAYMENT_GEMINI_API_KEYS=key1,key2,key3
# Raqamlangan Railway Variables ham qo'llab-quvvatlanadi:
# PAYMENT_GEMINI_API_KEY_001 ... PAYMENT_GEMINI_API_KEY_110
# Kalit qiymatlari logga chiqarilmaydi.
def _load_payment_gemini_api_keys():
    collected = []
    raw_keys = os.getenv("PAYMENT_GEMINI_API_KEYS", "")
    if raw_keys:
        collected.extend(k.strip() for k in raw_keys.split(",") if k.strip())

    prefix = "PAYMENT_GEMINI_API_KEY_"
    indexed = []
    for env_name, env_value in os.environ.items():
        if not env_name.startswith(prefix) or not env_value or not env_value.strip():
            continue
        suffix = env_name[len(prefix):]
        if suffix.isdigit():
            indexed.append((int(suffix), env_value.strip()))
    indexed.sort(key=lambda item: item[0])
    collected.extend(value for _, value in indexed)
    return list(dict.fromkeys(collected))


PAYMENT_GEMINI_API_KEYS = _load_payment_gemini_api_keys()
# Maximum keys attempted per configured model for one receipt. Defaults to all loaded keys.
# Set PAYMENT_OCR_KEY_ATTEMPTS lower if provider quotas/costs require a tighter cap.
PAYMENT_OCR_KEY_ATTEMPTS = max(1, min(
    len(PAYMENT_GEMINI_API_KEYS) or 1,
    int(os.getenv("PAYMENT_OCR_KEY_ATTEMPTS", str(len(PAYMENT_GEMINI_API_KEYS) or 1)))
))
PAYMENT_GEMINI_MODEL = os.getenv("PAYMENT_GEMINI_MODEL", "gemini-3.6-flash").strip()
_fallback_env = os.getenv("PAYMENT_GEMINI_FALLBACK_MODELS", "")
PAYMENT_GEMINI_FALLBACK_MODELS = [
    m.strip() for m in _fallback_env.split(",") if m.strip()
]
PAYMENT_OCR_MAX_CONCURRENT = max(1, int(os.getenv("PAYMENT_OCR_MAX_CONCURRENT", "2")))
PAYMENT_OCR_QUEUE_MAX = max(PAYMENT_OCR_MAX_CONCURRENT, int(os.getenv("PAYMENT_OCR_QUEUE_MAX", "100")))
PAYMENT_OCR_MAX_RETRIES = max(1, min(6, int(os.getenv("PAYMENT_OCR_MAX_RETRIES", "4"))))
PAYMENT_OCR_RETRY_BASE_SECONDS = max(5, int(os.getenv("PAYMENT_OCR_RETRY_BASE_SECONDS", "20")))
PAYMENT_OCR_RETRY_MAX_SECONDS = max(PAYMENT_OCR_RETRY_BASE_SECONDS, int(os.getenv("PAYMENT_OCR_RETRY_MAX_SECONDS", "600")))
PAYMENT_OCR_RETRY_SCAN_SECONDS = max(5, int(os.getenv("PAYMENT_OCR_RETRY_SCAN_SECONDS", "10")))
PAYMENT_OCR_PROCESSING_STALE_SECONDS = max(60, int(os.getenv("PAYMENT_OCR_PROCESSING_STALE_SECONDS", "900")))
P2P_CARD_NUMBER = os.getenv("P2P_CARD_NUMBER", "").strip()
PAYMENT_OCR_MAX_IMAGE_MB = max(1, int(os.getenv("PAYMENT_OCR_MAX_IMAGE_MB", "8")))
PAYMENT_OCR_DAILY_MAX_REQUESTS = max(1, int(os.getenv("PAYMENT_OCR_DAILY_MAX_REQUESTS", "5000")))
PAYMENT_OCR_MONTHLY_MAX_REQUESTS = max(1, int(os.getenv("PAYMENT_OCR_MONTHLY_MAX_REQUESTS", "50000")))
PAYMENT_OCR_MONTHLY_BUDGET_USD = max(0.0, float(os.getenv("PAYMENT_OCR_MONTHLY_BUDGET_USD", "10")))
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


class TemporaryAIError(RuntimeError):
    pass


class BudgetBlockedError(RuntimeError):
    pass


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
    suspicious_edits: bool = False
    suspicion_reason: str = ""
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


def _parse_receipt_date(value):
    """Parse common bank receipt date formats without guessing missing dates."""
    text = str(value or "").strip()
    if not text:
        return None
    patterns = (
        "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y",
        "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d",
        "%d.%m.%y", "%d/%m/%y", "%d-%m-%y",
    )
    for pattern in patterns:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    # If the model returned a longer date/time string, try visible date tokens.
    match = re.search(r"(?<!\d)(\d{1,4}[./-]\d{1,2}[./-]\d{2,4})(?!\d)", text)
    if match:
        token = match.group(1)
        for pattern in patterns:
            try:
                return datetime.strptime(token, pattern).date()
            except ValueError:
                pass
    return None


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
    """Persist a receipt before queueing it; rejected pending orders may submit a new image."""
    conn = _connect()
    try:
        row = conn.execute("SELECT status FROM p2p_receipts WHERE tx_id=?", (tx_id,)).fetchone()
        if row and row["status"] in ("analyzed", "approved", "review"):
            return False, "already_processed"
        if row and row["status"] == "rejected":
            payment = conn.execute(
                "SELECT status FROM payments WHERE tx_id=? AND user_id=?", (tx_id, int(user_id))
            ).fetchone()
            # A rejected receipt can be replaced only while its payment order is still pending.
            if not payment or payment["status"] != "pending":
                return False, "already_processed"
            conn.execute(
                "UPDATE p2p_receipts SET telegram_file_id=?, status='queued', reason='', last_error='', "
                "next_retry_at=0, processed_at=0, image_sha256='', attempts=0 WHERE tx_id=? AND user_id=?",
                (file_id, tx_id, int(user_id))
            )
        else:
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
    """Return the next key in a thread-safe round-robin rotation."""
    global _key_index
    if not PAYMENT_GEMINI_API_KEYS:
        return -1, None
    with _key_lock:
        idx = _key_index % len(PAYMENT_GEMINI_API_KEYS)
        _key_index = (idx + 1) % len(PAYMENT_GEMINI_API_KEYS)
    return idx, PAYMENT_GEMINI_API_KEYS[idx]


def _next_key_rotation():
    """Reserve a rotated, unique key order for one receipt/model attempt."""
    if not PAYMENT_GEMINI_API_KEYS:
        return []
    start_idx, _ = _next_key()
    count = min(len(PAYMENT_GEMINI_API_KEYS), PAYMENT_OCR_KEY_ATTEMPTS)
    return [((start_idx + offset) % len(PAYMENT_GEMINI_API_KEYS),
             PAYMENT_GEMINI_API_KEYS[(start_idx + offset) % len(PAYMENT_GEMINI_API_KEYS)])
            for offset in range(count)]


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


def _needs_payment_fallback(result):
    """True when OCR result is too incomplete for automatic payment approval.
    Transaction ID is required for automatic approval as an anti-fraud control.
    If it is missing, another configured model gets a chance to extract it;
    if all models fail to find it, the receipt goes to admin review.
    """
    return (
        not bool(result.is_receipt)
        or float(_parse_amount(result.amount)) <= 0
        or not bool(_last4(result.recipient_card_last4))
        or not bool((result.transaction_id or "").strip())
        or result.confidence < 0.85
    )



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
Return JSON matching the requested schema. All free-text fields, especially suspicion_reason and reason, MUST be written in Uzbek (Latin script) only. Never include English phrases in these fields.
Rules:
- amount: numeric payment amount, 0 if unknown.
- currency: visible currency, e.g. UZS.
- transaction_id: visible bank transaction/operation/reference ID; empty if absent.
- transaction_date/time: visible date/time as text.
- recipient_card_last4: ONLY the last 4 digits of the recipient card if visible; empty otherwise.
- recipient_name and bank_or_app: visible values only.
- confidence: 0..1 reflecting extraction confidence, not proof of real payment.
- is_receipt=false if this does not clearly look like a payment receipt.
- suspicious_edits=true only when there are concrete visual signs that an important field was altered, pasted, covered, digitally replaced, or composited (especially amount, date/time, recipient card/name, transaction/operation ID, or payment status).
- Do NOT mark suspicious_edits=true just because the receipt is shown small, compressed, captured in a screenshot, or has a legitimate bank stamp/watermark such as a "payment completed" stamp. These features alone are not evidence of fraud.
- A stamp or watermark may overlap other printed areas naturally. Do not treat the stamp alone as proof of editing; however, if it actually hides or makes a key field unreadable, report that field as unknown and explain the specific obstruction.
- suspicion_reason: state the exact concrete visual clue(s); empty if none are visible.
- Inspect transaction/operation IDs carefully: do not confuse a watermark, bank stamp, background text, or overlapping stamp text with the ID. Mark suspicious_edits=true only if the ID itself shows concrete signs of replacement, such as a distinct font/sharpness/background or an unexplained patch, not merely because a stamp crosses nearby text.
- Inspect the whole receipt for inconsistent fonts, misaligned rows, uneven backgrounds, duplicated/blurred characters, unexplained patches, inconsistent shadows, clipped labels, or text covering other fields.
- If a value is obscured, overlapped, ambiguous, or looks edited, do not guess it; report it as unknown and mark suspicious_edits=true when alteration is plausible.
- reason: concise explanation of missing/ambiguous data.
Do not claim that a receipt is authentic. OCR is not bank verification. A visually plausible receipt can still be fake; never use visual appearance alone as proof of payment.
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

    models = []
    for model in [PAYMENT_GEMINI_MODEL] + PAYMENT_GEMINI_FALLBACK_MODELS:
        if model and model not in models:
            models.append(model)

    last_error = None
    best_incomplete = None
    attempted_calls = 0
    for model in models:
        # Rotate the first key between receipts; on a failure/incomplete OCR,
        # try the next configured key for this model, up to the configured cap.
        for key_idx, api_key in _next_key_rotation():
            # Reserve one budget unit for each real provider call, including key/model fallbacks.
            allowed, budget_reason = _budget_reserve()
            if not allowed:
                logging.warning(
                    "P2P API kalit aylanishi budjet limiti sabab to‘xtadi | urinishlar=%s | sabab=%s",
                    attempted_calls, budget_reason
                )
                raise BudgetBlockedError(budget_reason)

            acquired = _semaphore.acquire(timeout=120)
            if not acquired:
                last_error = TemporaryAIError("OCR concurrency limit")
                logging.warning(
                    "P2P Gemini navbatidan parallel ish joyi olinmadi | model=%s | kalit=%s",
                    model, key_idx
                )
                continue
            try:
                attempted_calls += 1
                response = _vision_once(file_bytes, mime_type, model, api_key)
                result = _parse_response(response.text)
                inp, out = _extract_usage(response)
                if _needs_payment_fallback(result):
                    if best_incomplete is None or result.confidence > best_incomplete[0].confidence:
                        best_incomplete = (result, key_idx, inp, out, model)
                    logging.warning(
                        "P2P Gemini natijasi to‘liq emas; keyingi kalit tekshiriladi | model=%s | kalit=%s | chek_urinish=%s | ishonchlilik=%.2f",
                        model, key_idx, attempt_number, result.confidence
                    )
                    continue
                logging.info(
                    "P2P Gemini kalitlari aylanishi muvaffaqiyatli | model=%s | kalit_indeksi=%s | API_urinishlari=%s | chek_urinish=%s",
                    model, key_idx, attempted_calls, attempt_number
                )
                return result, key_idx, inp, out, model
            except Exception as exc:
                last_error = exc
                logging.warning(
                    "P2P Gemini xatosi; keyingi kalit yoki model tekshiriladi | model=%s | kalit=%s | chek_urinish=%s | sabab=%s",
                    model, key_idx, attempt_number, exc
                )
                # A key may be invalid or rate-limited; continue the controlled
                # rotation. BudgetBlockedError is raised before the provider call.
            finally:
                _semaphore.release()

    if best_incomplete is not None:
        logging.warning(
            "P2P Gemini barcha sozlangan urinishlardan keyin eng yaxshi, ammo to‘liq bo‘lmagan OCR natijasini qaytarmoqda | API_urinishlari=%s",
            attempted_calls
        )
        return best_incomplete
    if last_error is None:
        raise RuntimeError("Gemini Vision uchun ishlaydigan API key/model topilmadi")
    if _is_transient_error(last_error):
        raise TemporaryAIError(str(last_error))
    raise last_error


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


def _get_user_lang(user_id):
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT language FROM users WHERE user_id=?", (user_id,)).fetchone()
        finally:
            conn.close()
        lang = row[0] if row else "uz"
        return lang if lang in ("uz", "ru", "en") else "uz"
    except Exception:
        return "uz"


P2P_MESSAGES = {
    "uz": {
        "received": "⏳ Chekingiz qabul qilindi.\nAI tekshiruvi vaqtincha bandligi sabab biroz choʻzilmoqda. Tekshiruv avtomatik davom etadi. Premium toʻlovingiz tasdiqlangandan soʻng avtomatik faollashadi.",
        "queue": "⏳ Chekingiz qabul qilindi va navbatga saqlandi. Navbat vaqtincha band; tizim uni avtomatik qayta ishlaydi va natijani xabar qiladi. Iltimos, ayni chekni qayta yubormang.",
        "budget": "⏳ Chekingiz saqlandi. AI tekshiruv limiti vaqtincha toʻldi; tizim avtomatik qayta tekshiradi. Sizdan qayta toʻlov talab qilinmaydi.",
        "approved": "🎉 Toʻlov tasdiqlandi! PRO status avtomatik faollashtirildi. 👑",
        "duplicate": "❌ Bu chek rasmi avval yuborilgan. Tarif avtomatik faollashtirilmadi.",
        "duplicate_tx": "❌ Bu toʻlov cheki avval ishlatilgan. Tarif avtomatik faollashtirilmadi.",
        "card_mismatch": "❌ Toʻlov tasdiqlanmadi: chekdagi qabul qiluvchi karta raqami biz ko‘rsatgan kartaga mos emas. Premium faollashtirilmadi. Iltimos, toʻgʻri kartaga toʻlov qilib, yangi chek yuboring.",
        "card_unreadable": "⚠️ Chekdagi qabul qiluvchi karta raqamini aniq oʻqib boʻlmadi. Premium faollashtirilmadi. Iltimos, karta raqami aniq koʻrinadigan chek rasmini qayta yuboring.",
        "retry_receipt_title": "❌ Chek tekshiruvdan oʻtmadi.",
        "retry_receipt_intro": "Quyidagi muammolar aniqlandi:",
        "retry_receipt_amount": "• Chekdagi summa tanlangan tarif narxiga mos emas.",
        "retry_receipt_date": "• Chek sanasi bugungi sana emas yoki sana aniqlanmadi.",
        "retry_receipt_card": "• Qabul qiluvchi karta raqami mos emas yoki aniq koʻrinmayapti.",
        "retry_receipt_tx": "• Tranzaksiya raqami aniqlanmadi.",
        "retry_receipt_not_receipt": "• Rasm toʻlov cheki ekanligi aniqlanmadi.",
        "retry_receipt_confidence": "• Chekdagi maʼlumotlar aniq oʻqilmadi.",
        "retry_receipt_next": "Premium faollashtirilmadi. Iltimos, maʼlumotlari toʻliq va aniq koʻrinadigan toʻgʻri chek rasmini yuboring. Toʻlovni qaytadan amalga oshirmang; avvalgi buyurtmangiz boʻyicha yangi chek yuboring.",
        "review_title": "⚠️ Chek avtomatik tekshiruvdan oʻtmadi.",
        "review_intro": "Premium avtomatik faollashtirilmadi. Aniqlangan holatlar:",
        "review_date_missing": "• Chek sanasi aniqlanmadi yoki oʻqib boʻlmadi.",
        "review_date_old": "• Chek sanasi bugungi sana bilan mos emas.",
        "review_tx_missing": "• Tranzaksiya raqami (Transaction ID) topilmadi.",
        "review_amount": "• Chekdagi summa buyurtmadagi summaga mos kelmadi.",
        "review_card": "• Qabul qiluvchi karta raqami mos kelmadi yoki aniqlanmadi.",
        "review_not_receipt": "• Yuborilgan rasm toʻlov cheki ekanligi aniqlanmadi.",
        "review_confidence": "• Chek maʼlumotlarini aniqlash ishonchliligi yetarli emas.",
        "review_suspicious": "• Chekdagi ayrim maʼlumotlar tahrirlangan boʻlishi mumkin.",
        "review_next": "Iltimos, chek maʼlumotlarini tekshiring. Agar toʻlov toʻgʻri boʻlsa, administrator qoʻshimcha tekshiradi.",
        "technical_error": "⚠️ Chekni avtomatik tekshirishda texnik xatolik yuz berdi. Premium faollashtirilmadi; administrator qoʻshimcha tekshiradi. Iltimos, hozircha chekni qayta yubormang.",
    },
    "ru": {
        "received": "⏳ Чек получен.\nИз-за временной нагрузки AI-проверка может занять немного больше времени. Проверка продолжится автоматически. После подтверждения платежа Premium будет активирован автоматически.",
        "queue": "⏳ Чек принят и сохранён в очереди. Очередь временно перегружена; система автоматически обработает чек и сообщит результат. Пожалуйста, не отправляйте тот же чек повторно.",
        "budget": "⏳ Чек сохранён. Лимит AI-проверок временно исчерпан; система автоматически повторит проверку. Повторно оплачивать не нужно.",
        "approved": "🎉 Платёж подтверждён! Статус PRO активирован автоматически. 👑",
        "duplicate": "❌ Этот чек уже был отправлен ранее. Тариф автоматически не активирован.",
        "duplicate_tx": "❌ Этот платёжный чек уже использовался. Тариф автоматически не активирован.",
        "card_mismatch": "❌ Платёж не подтверждён: номер карты получателя в чеке не совпадает с указанной нами картой. Premium не активирован. Пожалуйста, оплатите на правильную карту и отправьте новый чек.",
        "card_unreadable": "⚠️ Не удалось чётко прочитать номер карты получателя на чеке. Premium не активирован. Пожалуйста, отправьте чек, на котором номер карты хорошо виден.",
        "retry_receipt_title": "❌ Чек не прошёл проверку.",
        "retry_receipt_intro": "Обнаружены следующие проблемы:",
        "retry_receipt_amount": "• Сумма в чеке не совпадает с ценой выбранного тарифа.",
        "retry_receipt_date": "• Дата чека не сегодняшняя или её не удалось определить.",
        "retry_receipt_card": "• Номер карты получателя не совпадает или плохо виден.",
        "retry_receipt_tx": "• Не удалось определить номер транзакции.",
        "retry_receipt_not_receipt": "• Не удалось определить, что изображение является платёжным чеком.",
        "retry_receipt_confidence": "• Данные чека не удалось прочитать достаточно чётко.",
        "retry_receipt_next": "Premium не активирован. Пожалуйста, отправьте новый чёткий чек, на котором все данные хорошо видны. Не оплачивайте повторно; отправьте новый чек по текущему заказу.",
        "review_title": "⚠️ Чек не прошёл автоматическую проверку.",
        "review_intro": "Premium не активирован автоматически. Обнаружены следующие проблемы:",
        "review_date_missing": "• Не удалось определить или прочитать дату чека.",
        "review_date_old": "• Дата чека не совпадает с сегодняшней датой.",
        "review_tx_missing": "• Не найден номер транзакции (Transaction ID).",
        "review_amount": "• Сумма в чеке не совпадает с суммой заказа.",
        "review_card": "• Номер карты получателя не совпадает или не удалось его определить.",
        "review_not_receipt": "• Не удалось подтвердить, что изображение является платёжным чеком.",
        "review_confidence": "• Недостаточная уверенность в распознавании данных чека.",
        "review_suspicious": "• Возможно, некоторые данные чека были изменены.",
        "review_next": "Пожалуйста, проверьте данные чека. Если платёж выполнен правильно, администратор проверит его дополнительно.",
        "technical_error": "⚠️ При автоматической проверке чека произошла техническая ошибка. Premium не активирован; администратор проверит чек дополнительно. Пожалуйста, пока не отправляйте чек повторно.",
    },
    "en": {
        "received": "⏳ Your receipt has been received.\nDue to temporary AI load, verification may take a little longer. The verification will continue automatically. Once your payment is confirmed, Premium will be activated automatically.",
        "queue": "⏳ Your receipt has been received and saved in the queue. The queue is temporarily busy; the system will process it automatically and notify you of the result. Please do not resend the same receipt.",
        "budget": "⏳ Your receipt has been saved. The AI verification limit is temporarily reached; the system will retry automatically. You do not need to pay again.",
        "approved": "🎉 Payment confirmed! Your PRO status has been activated automatically. 👑",
        "duplicate": "❌ This receipt image was already submitted. The plan was not activated automatically.",
        "duplicate_tx": "❌ This payment receipt has already been used. The plan was not activated automatically.",
        "card_mismatch": "❌ Payment not confirmed: the recipient card number on the receipt does not match the card we provided. Premium was not activated. Please pay to the correct card and submit a new receipt.",
        "card_unreadable": "⚠️ The recipient card number on the receipt could not be read clearly. Premium was not activated. Please resend a receipt where the card number is clearly visible.",
        "retry_receipt_title": "❌ The receipt did not pass verification.",
        "retry_receipt_intro": "The following issues were detected:",
        "retry_receipt_amount": "• The receipt amount does not match the selected plan price.",
        "retry_receipt_date": "• The receipt date is not today or could not be identified.",
        "retry_receipt_card": "• The recipient card number does not match or is not clearly visible.",
        "retry_receipt_tx": "• The transaction number could not be identified.",
        "retry_receipt_not_receipt": "• The image could not be identified as a payment receipt.",
        "retry_receipt_confidence": "• The receipt details could not be read clearly enough.",
        "retry_receipt_next": "Premium was not activated. Please send a new, clear receipt with all details visible. Do not pay again; submit a new receipt for the current order.",
        "review_title": "⚠️ The receipt did not pass automatic verification.",
        "review_intro": "Premium was not activated automatically. The following issues were detected:",
        "review_date_missing": "• The receipt date could not be identified or read.",
        "review_date_old": "• The receipt date does not match today's date.",
        "review_tx_missing": "• The transaction number (Transaction ID) was not found.",
        "review_amount": "• The receipt amount does not match the order amount.",
        "review_card": "• The recipient card number does not match or could not be identified.",
        "review_not_receipt": "• The image could not be confirmed as a payment receipt.",
        "review_confidence": "• The receipt details could not be identified with sufficient confidence.",
        "review_suspicious": "• Some receipt details may have been altered.",
        "review_next": "Please check the receipt details. If you made the payment correctly, an administrator will review it further.",
        "technical_error": "⚠️ A technical error occurred while verifying the receipt. Premium was not activated; an administrator will review it. Please do not resend the receipt for now.",
    },
}


def _p2p_message(user_id, key):
    return P2P_MESSAGES[_get_user_lang(user_id)][key]


def _p2p_review_message(user_id, issue_keys):
    messages = P2P_MESSAGES[_get_user_lang(user_id)]
    lines = [messages["review_title"], "", messages["review_intro"]]
    lines.extend(messages[key] for key in issue_keys if key in messages)
    lines.extend(["", messages["review_next"]])
    return "\n".join(lines)


def _p2p_retry_receipt_message(user_id, issue_keys):
    """Explain definite receipt errors and ask for a replacement, without admin review."""
    messages = P2P_MESSAGES[_get_user_lang(user_id)]
    lines = [messages["retry_receipt_title"], "", messages["retry_receipt_intro"]]
    lines.extend(messages[key] for key in issue_keys if key in messages)
    lines.extend(["", messages["retry_receipt_next"]])
    return "\n".join(lines)


def _notify_user(user_id, text):
    try:
        if _bot:
            _bot.send_message(user_id, text)
    except Exception as exc:
        logging.warning("P2P: foydalanuvchiga xabar yuborilmadi | sabab=%s", exc)


def _notify_admin(text):
    try:
        if _bot and _admin_id:
            _bot.send_message(_admin_id, text)
    except Exception as exc:
        logging.warning("P2P: administratorga xabar yuborilmadi | sabab=%s", exc)


def _notify_admin_review(text, file_id, tx_id, user_id):
    """Send the original receipt image with admin decision buttons."""
    if not _bot or not _admin_id:
        return
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"p2p_approve|{tx_id}|{int(user_id)}"),
        telebot.types.InlineKeyboardButton("❌ Rad etish", callback_data=f"p2p_reject|{tx_id}|{int(user_id)}"),
    )
    try:
        _bot.send_photo(_admin_id, file_id, caption=text[:1000], reply_markup=markup)
    except Exception as exc:
        logging.warning("P2P: chek rasmini administratorga yuborishda xato | buyurtma=%s | sabab=%s", tx_id, exc)
        try:
            _bot.send_message(_admin_id, text[:4000], reply_markup=markup)
        except Exception:
            logging.exception("P2P: administratorga zaxira xabar yuborishda xato | buyurtma=%s", tx_id)


def handle_admin_decision(call):
    """Handle admin approval/rejection for a receipt requiring manual review."""
    try:
        action, tx_id, raw_user_id = (call.data or "").split("|", 2)
        user_id = int(raw_user_id)
    except (ValueError, AttributeError):
        _bot.answer_callback_query(call.id, "Noto'g'ri to'lov so'rovi.", show_alert=True)
        return

    if not _admin_id or int(call.from_user.id) != int(_admin_id):
        _bot.answer_callback_query(call.id, "Bu amal faqat administrator uchun.", show_alert=True)
        return

    conn = _connect()
    try:
        pay = conn.execute("SELECT status FROM payments WHERE tx_id=? AND user_id=?", (tx_id, user_id)).fetchone()
        receipt = conn.execute("SELECT status FROM p2p_receipts WHERE tx_id=? AND user_id=?", (tx_id, user_id)).fetchone()
        if not pay or pay["status"] != "pending" or not receipt or receipt["status"] != "review":
            _bot.answer_callback_query(call.id, "Bu to'lov allaqachon ko'rib chiqilgan yoki ko'rib chiqishga tayyor emas.", show_alert=True)
            return
    finally:
        conn.close()

    lang = _get_user_lang(user_id)
    if action == "p2p_approve":
        ok, _plan = _approve(tx_id, user_id)
        if not ok:
            _bot.answer_callback_query(call.id, "To'lov tasdiqlanmadi: holat o'zgargan.", show_alert=True)
            return
        _mark_by_tx(tx_id, status="approved", reason="Admin tomonidan tasdiqlandi", processed_at=_now(), next_retry_at=0)
        messages = {
            "uz": "🎉 Admin to'lovingizni tasdiqladi. Premium/PRO faollashtirildi! 👑",
            "ru": "🎉 Администратор подтвердил платёж. Premium/PRO активирован! 👑",
            "en": "🎉 The administrator approved your payment. Premium/PRO is now active! 👑",
        }
        _notify_user(user_id, messages.get(lang, messages["uz"]))
        result_text, answer = "🟢 ADMIN TOMONIDAN TASDIQLANDI", "To'lov tasdiqlandi."
    elif action == "p2p_reject":
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("UPDATE payments SET status='rejected' WHERE tx_id=? AND user_id=? AND status='pending'", (tx_id, user_id))
            if cur.rowcount != 1:
                conn.rollback()
                _bot.answer_callback_query(call.id, "To'lov holati o'zgargan.", show_alert=True)
                return
            conn.execute("UPDATE p2p_receipts SET status='rejected', reason='Admin tomonidan rad etildi', processed_at=?, next_retry_at=0 WHERE tx_id=?", (_now(), tx_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        messages = {
            "uz": "❌ Admin to'lov chekingizni rad etdi. Sababini aniqlash uchun administrator bilan bog'laning.",
            "ru": "❌ Администратор отклонил ваш чек. Свяжитесь с администратором, чтобы уточнить причину.",
            "en": "❌ The administrator rejected your receipt. Contact the administrator for details.",
        }
        _notify_user(user_id, messages.get(lang, messages["uz"]))
        result_text, answer = "🔴 ADMIN TOMONIDAN RAD ETILDI", "To'lov rad etildi."
    else:
        _bot.answer_callback_query(call.id, "Noma'lum amal.", show_alert=True)
        return

    _bot.answer_callback_query(call.id, answer)
    try:
        if getattr(call.message, "photo", None):
            _bot.edit_message_caption((getattr(call.message, "caption", "") or "") + "\n\n" + result_text,
                                      call.message.chat.id, call.message.message_id)
        else:
            _bot.edit_message_text((getattr(call.message, "text", "") or "") + "\n\n" + result_text,
                                   call.message.chat.id, call.message.message_id)
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception as exc:
        logging.warning("P2P: Telegramda administrator qarorini belgilab bo‘lmadi | buyurtma=%s | sabab=%s", tx_id, exc)


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
        logging.info("P2P OCR: %s ta chek qayta ishlash navbatiga qaytarildi", recovered)
    return recovered


def _retry_scheduler():
    while True:
        try:
            _recover_queued(force=False)
        except Exception:
            logging.exception("P2P OCR: qayta urinish jadvalida xatolik")
        time.sleep(PAYMENT_OCR_RETRY_SCAN_SECONDS)


def _user_identity(user_id):
    """Return Telegram display name and username for admin diagnostics.
    Telegram lookup is best-effort; payment processing must never fail because
    profile information could not be fetched.
    """
    name = "Mavjud emas"
    username = "Mavjud emas"
    try:
        if _bot:
            chat = _bot.get_chat(user_id)
            first = (getattr(chat, "first_name", "") or "").strip()
            last = (getattr(chat, "last_name", "") or "").strip()
            name = " ".join(x for x in (first, last) if x) or name
            raw_username = (getattr(chat, "username", "") or "").strip()
            if raw_username:
                username = f"@{raw_username}"
    except Exception as exc:
        logging.debug("P2P: Telegram foydalanuvchi profilini olishda xatolik | foydalanuvchi=%s | sabab=%s", user_id, exc)
    return name, username


def _friendly_ai_error(exc):
    """Translate common provider errors into a concise Uzbek admin explanation."""
    text = str(exc or "")
    upper = text.upper()
    if "404" in upper or "NOT_FOUND" in upper:
        return "Sozlangan AI modeli ushbu API hisobida mavjud emas."
    if "429" in upper or "RESOURCE_EXHAUSTED" in upper:
        return "AI so‘rovlar limiti vaqtincha oshib ketdi."
    if "503" in upper or "UNAVAILABLE" in upper:
        return "AI modeli vaqtincha band yoki xizmatda uzilish yuz berdi."
    if "401" in upper or "403" in upper or "API KEY" in upper or "PERMISSION" in upper:
        return "P2P AI API kaliti yoki unga berilgan ruxsat bilan muammo bor."
    if "TIMEOUT" in upper or "DEADLINE_EXCEEDED" in upper:
        return "AI tekshiruvi vaqt chegarasidan oshdi."
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return "AI javobi kutilgan formatda qaytmadi."
    return "AI tekshiruvida kutilmagan texnik xatolik yuz berdi."


def _process(item):
    tx_id, user_id, file_id = item
    if not _claim(tx_id):
        return
    pay = _payment_row(tx_id)
    if not pay or pay["status"] != "pending":
        _mark_by_tx(tx_id, status="cancelled", processed_at=_now(), reason="Payment is no longer pending")
        return

    # Budget is reserved inside _vision() for each real provider call.
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
            _notify_user(user_id, _p2p_message(user_id, "duplicate"))
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

        # Bir tranzaksiya ID qayta ishlatilmasin: bu tekshiruv admin-review qaroridan
        # oldin bajariladi, shunda avval ko‘rilgan chek qayta yuborilsa admin navbatiga tushmaydi.
        transaction_id = (result.transaction_id or "").strip()
        if transaction_id:
            conn = _connect()
            try:
                duplicate = conn.execute(
                    "SELECT tx_id FROM p2p_receipts "
                    "WHERE transaction_id=? AND tx_id<>? "
                    "AND status IN ('analyzed','approved','review') LIMIT 1",
                    (transaction_id, tx_id)
                ).fetchone()
            finally:
                conn.close()
            if duplicate:
                _mark_by_tx(
                    tx_id, status="rejected",
                    reason="Transaction ID avval ishlatilgan",
                    processed_at=_now(), next_retry_at=0
                )
                logging.warning(
                    "P2P TO‘LOV QARORI | qaror=TAKRORIY_CHEK_RAD_ETILDI | "
                    "buyurtma=%s | transaction_id=%s | avvalgi_buyurtma=%s",
                    tx_id, transaction_id, duplicate["tx_id"]
                )
                _notify_user(user_id, _p2p_message(user_id, "duplicate_tx"))
                return

        expected = _expected_amount(pay["tariff_price"])
        amount_ok = abs(_parse_amount(result.amount) - expected) < 0.01
        conf_ok = result.confidence >= 0.85
        receipt_ok = bool(result.is_receipt)
        txid_ok = bool((result.transaction_id or "").strip())
        # Any model-detected visual tampering signal blocks automatic approval.
        # Admin must compare the original image and transaction details manually.
        suspicious_edits = bool(result.suspicious_edits)
        expected_card = _last4(P2P_CARD_NUMBER)
        actual_card = _last4(result.recipient_card_last4)
        card_ok = bool(expected_card) and bool(actual_card) and expected_card == actual_card

        # Clear card failures are actionable user errors, not admin-review cases.
        # If the expected card is not configured, retain admin review instead of blaming the user.
        if expected_card and receipt_ok and not actual_card:
            reason_text = "Chekdagi qabul qiluvchi karta raqami aniq o‘qilmadi"
            _mark_by_tx(tx_id, status="rejected", reason=reason_text, processed_at=_now(), next_retry_at=0)
            logging.warning(
                "P2P TO‘LOV QARORI | qaror=KARTA_O‘QILMADI | buyurtma=%s | foydalanuvchi=%s",
                tx_id, user_id
            )
            _notify_user(user_id, _p2p_message(user_id, "card_unreadable"))
            return
        if expected_card and receipt_ok and conf_ok and actual_card and expected_card != actual_card:
            reason_text = f"Qabul qiluvchi karta mos emas: {actual_card} != {expected_card}"
            _mark_by_tx(tx_id, status="rejected", reason=reason_text, processed_at=_now(), next_retry_at=0)
            logging.warning(
                "P2P TO‘LOV QARORI | qaror=KARTA_MOS_EMAS | buyurtma=%s | foydalanuvchi=%s | karta_oxiri=%s",
                tx_id, user_id, actual_card
            )
            _notify_user(user_id, _p2p_message(user_id, "card_mismatch"))
            return

        receipt_date = _parse_receipt_date(result.transaction_date)
        today_uz = datetime.now(UZ_TZ).date()
        date_ok = receipt_date is not None and receipt_date == today_uz

        # Har bir chek bo‘yicha OCR natijasi va qaror mezonlarini logda ko‘rsatish.
        logging.info(
             "P2P CHEK TAHLILI | buyurtma=%s | aniqlash_ishonchliligi=%.2f | tahrir_alomati=%s | "
            "tranzaksiya_raqami=%s | chek_sanasi=%s | summa_mos=%s | karta_mos=%s | sana_mos=%s",
            tx_id, result.confidence, suspicious_edits,
            (result.transaction_id or "aniqlanmadi"),
            (result.transaction_date or "aniqlanmadi"),
            amount_ok, card_ok, date_ok
        )

        if not expected_card and not suspicious_edits:
            # Qabul qiluvchi karta konfiguratsiyasi yo‘q bo‘lsa, foydalanuvchini ayblamasdan
            # sozlama muammosini administratorga chiqaramiz.
            reason_text = "P2P_CARD_NUMBER sozlanmagan; qabul qiluvchi kartani tekshirib bo‘lmaydi"
            _mark_by_tx(tx_id, status="review", reason=reason_text)
            logging.error(
                "P2P TO‘LOV QARORI | qaror=ADMIN_TEKSHIRUVI | buyurtma=%s | "
                "sabab=qabul_qiluvchi_karta_sozlanmagan",
                tx_id
            )
            _notify_user(user_id, _p2p_message(user_id, "technical_error"))
            display_name, username = _user_identity(user_id)
            _notify_admin_review(
                f"⚠️ P2P TO‘LOV — SOZLAMA MUAMMOSI\\n\\n"
                f"🧾 Buyurtma: {tx_id}\\n"
                f"👤 Foydalanuvchi: {display_name}\\n"
                f"🔗 Foydalanuvchi nomi: {username}\\n"
                f"🆔 Telegram ID: {user_id}\\n\\n"
                f"🔎 Muammo: {reason_text}. Railway sozlamasidagi P2P_CARD_NUMBER qiymatini tekshiring.\\n"
                f"ℹ️ To‘lov avtomatik tasdiqlanmadi.",
                file_id, tx_id, user_id
            )
            return

        if suspicious_edits:
            # Tahrirlash yoki soxtalashtirish alomati bo‘lsa, foydalanuvchiga avtomatik ayb qo‘ymaymiz:
            # asl chek administrator tomonidan qo‘lda tekshiriladi.
            reason_text = "Chekda tahrirlash yoki muhim ma’lumotni almashtirish alomatlari bo‘lishi mumkin"
            logging.warning(
                "P2P TO‘LOV QARORI | qaror=ADMIN_TEKSHIRUVI | buyurtma=%s | "
                "ishonchlilik=%.2f | tahrir_alomati=%s | tranzaksiya_raqami=%s | chek_sanasi=%s | sabab=%s",
                tx_id, result.confidence, suspicious_edits,
                result.transaction_id or "aniqlanmadi",
                result.transaction_date or "aniqlanmadi", reason_text
            )
            _mark_by_tx(tx_id, status="review", reason=reason_text)
            display_name, username = _user_identity(user_id)
            _notify_user(user_id, _p2p_review_message(user_id, ["review_suspicious"]))
            _notify_admin_review(
                f"⚠️ P2P TO‘LOV — QO‘SHIMCHA TEKSHIRUV KERAK\n\n"
                f"🧾 Buyurtma: {tx_id}\n"
                f"👤 Foydalanuvchi: {display_name}\n"
                f"🔗 Foydalanuvchi nomi: {username}\n"
                f"🆔 Telegram ID: {user_id}\n\n"
                f"💰 Kutilgan summa: {pay['tariff_price']}\n"
                f"🤖 Aniqlangan summa: {result.amount}\n"
                f"💳 Karta oxirgi raqamlari: {actual_card or 'aniqlanmadi'} / kutilgan: {expected_card or 'sozlanmagan'}\n"
                f"📅 Chek sanasi: {receipt_date.strftime('%d.%m.%Y') if receipt_date else (result.transaction_date or 'aniqlanmadi')} / bugun: {today_uz.strftime('%d.%m.%Y')}\n"
                f"📊 Aniqlash ishonchliligi: {result.confidence:.2f}\n"
                f"🔢 Tranzaksiya raqami: {result.transaction_id or 'aniqlanmadi'}\n"
                f"🛡️ Tahrir alomati: ANIQLANDI — asl rasmni qo‘lda tekshiring\n"
                f"🤖 Ishlatilgan model: {used_model}\n\n"
                f"🔎 Sabab: {reason_text}\n\n"
                f"ℹ️ Premium avtomatik faollashtirilmadi.",
                file_id, tx_id, user_id
            )
            return

        # Soxtalashtirish alomati bo‘lmagan aniq yoki o‘qib bo‘lmaydigan xatolar
        # administrator navbatini band qilmaydi: buyurtma rad etiladi va yangi chek so‘raladi.
        if not receipt_ok or not amount_ok or not conf_ok or not txid_ok or not card_ok or not date_ok:
            reasons = []
            user_issue_keys = []
            if not receipt_ok:
                reasons.append("rasm to‘lov cheki emas yoki chek aniqlanmadi")
                user_issue_keys.append("retry_receipt_not_receipt")
            if not amount_ok:
                reasons.append(f"summa mos emas: {result.amount} != {expected}")
                user_issue_keys.append("retry_receipt_amount")
            if not conf_ok:
                reasons.append(f"chek ma’lumotlarini aniqlash ishonchliligi past: {result.confidence:.2f}")
                user_issue_keys.append("retry_receipt_confidence")
            if not txid_ok:
                reasons.append("tranzaksiya raqami aniqlanmadi")
                user_issue_keys.append("retry_receipt_tx")
            if not card_ok:
                reasons.append("qabul qiluvchi karta raqami mos emas yoki aniqlanmadi")
                user_issue_keys.append("retry_receipt_card")
            if receipt_date is None or receipt_date != today_uz:
                if receipt_date is None:
                    reasons.append("chek sanasi aniqlanmadi yoki o‘qib bo‘lmadi")
                else:
                    reasons.append(f"chek sanasi mos emas: {receipt_date.strftime('%d.%m.%Y')} / bugun: {today_uz.strftime('%d.%m.%Y')}")
                user_issue_keys.append("retry_receipt_date")
            reason_text = "; ".join(reasons) or "Chek ma’lumotlari tekshiruv talablariga mos emas"
            _mark_by_tx(tx_id, status="rejected", reason=reason_text, processed_at=_now(), next_retry_at=0)
            logging.warning(
                "P2P TO‘LOV QARORI | qaror=YANGI_CHEK_SO‘RALDI | buyurtma=%s | foydalanuvchi=%s | "
                "ishonchlilik=%.2f | summa_mos=%s | karta_mos=%s | sana_mos=%s | tranzaksiya_raqami=%s | sabab=%s",
                tx_id, user_id, result.confidence, amount_ok, card_ok, date_ok,
                result.transaction_id or "aniqlanmadi", reason_text
            )
            _notify_user(user_id, _p2p_retry_receipt_message(user_id, user_issue_keys))
            return

        ok, info = _approve(tx_id, user_id)
        if ok:
            _mark_by_tx(tx_id, status="approved", reason="AI rules passed", processed_at=_now(), next_retry_at=0)
            _notify_user(user_id, _p2p_message(user_id, "approved"))
            logging.info(
                 "P2P TO‘LOV QARORI | qaror=AVTOMATIK_TASDIQLANDI | buyurtma=%s | "
                "foydalanuvchi=%s | aniqlash_ishonchliligi=%.2f | tahrir_alomati=%s | "
                "tranzaksiya_raqami=%s | chek_sanasi=%s | sabab=barcha tekshiruvlar muvaffaqiyatli | model=%s | xarajat=$%.6f",
                tx_id, user_id, result.confidence, bool(result.suspicious_edits),
                (result.transaction_id or "aniqlanmadi"),
                (result.transaction_date or "aniqlanmadi"), used_model, cost
            )
        else:
            _mark_by_tx(tx_id, status="review", reason=info)
    except BudgetBlockedError as exc:
        attempts = _get_attempts(tx_id)
        _mark_by_tx(tx_id, status="budget_blocked", reason=str(exc), last_error=str(exc), next_retry_at=_now() + 3600)
        if attempts <= 1:
            _notify_user(user_id, _p2p_message(user_id, "budget"))
        logging.warning("P2P OCR: xarajat limiti sabab tekshiruv to‘xtatildi | buyurtma=%s | foydalanuvchi=%s | sabab=%s", tx_id, user_id, exc)
    except Exception as exc:
        attempts = _get_attempts(tx_id)
        transient = _is_transient_error(exc) or isinstance(exc, TemporaryAIError)
        if transient:
            delay = _retry_delay(min(attempts, PAYMENT_OCR_MAX_RETRIES))
            _mark_by_tx(tx_id, status="retry_wait", reason="Vaqtinchalik AI xatosi; avtomatik qayta uriniladi", last_error=str(exc), next_retry_at=_now() + delay)
            logging.warning("P2P OCR: vaqtinchalik xatolik | buyurtma=%s | urinish=%s | qayta_urinish=%s soniyadan keyin | sabab=%s", tx_id, attempts, delay, exc)
            if attempts == 1:
                _notify_user(user_id, _p2p_message(user_id, "received"))
            # Do not spam the admin for every transient 503. The receipt remains durable.
        else:
            delay = PAYMENT_OCR_RETRY_MAX_SECONDS
            _mark_by_tx(tx_id, status="error", reason=str(exc)[:1000], last_error=str(exc)[:2000], next_retry_at=_now() + delay)
            display_name, username = _user_identity(user_id)
            friendly = _friendly_ai_error(exc)
            _notify_user(user_id, _p2p_message(user_id, "technical_error"))
            _notify_admin(
                f"⚠️ P2P TO‘LOV — AI TEKSHIRUVIDA XATOLIK\n\n"
                f"🧾 TX: {tx_id}\n"
                f"👤 Foydalanuvchi: {display_name}\n"
                f"🔗 Username: {username}\n"
                f"🆔 Telegram ID: {user_id}\n"
                f"🔢 Urinish: {attempts}\n\n"
                f"🔴 Muammo: {friendly}\n\n"
                f"💳 Premium avtomatik faollashtirilmadi.\n"
                f"🔄 Tizim holati: to‘lov saqlandi va belgilangan tartibda qayta tekshiriladi.\n\n"
                f"🛠 Texnik xato: {str(exc)[:800]}"
            )


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
    logging.info("P2P OCR ishchisi ishga tushdi | asosiy_model=%s | zaxira_modellar=%s | parallel_ish=%s | navbat_hajmi=%s | kalitlar_soni=%s",
                 PAYMENT_GEMINI_MODEL, ",".join(PAYMENT_GEMINI_FALLBACK_MODELS) or "sozlanmagan",
                 PAYMENT_OCR_MAX_CONCURRENT, PAYMENT_OCR_QUEUE_MAX, len(PAYMENT_GEMINI_API_KEYS))
    while True:
        item = _queue.get()
        try:
            _process(item)
        except Exception:
            logging.exception("P2P OCR ishchi jarayonida kutilmagan xatolik yuz berdi")
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
