# -*- coding: utf-8 -*-
import docx
import asyncio
from contextlib import asynccontextmanager
import re
from io import BytesIO
from docx import Document
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from google import genai
from google.genai import types as genai_types
import json
import logging
import os
import hashlib
import hmac
from urllib.parse import parse_qsl, urlparse, parse_qs
from pydantic import BaseModel, Field
from pypdf import PdfReader

# PyMuPDF tez PDF matn ajratish uchun ixtiyoriy ishlatiladi.
# Kutubxona o'rnatilmagan muhitda mavjud pypdf usuli saqlanadi.
try:
    import pymupdf
except ImportError:
    pymupdf = None
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
import sqlite3
import telebot
import threading
import time
from typing import List, Optional
import uvicorn
import uuid
import random
import secrets
from copy import deepcopy
from pathlib import Path
from deep_translator import GoogleTranslator
from datetime import datetime, timezone, timedelta

# P2P Payment AI is isolated from Generation AI. It uses PAYMENT_GEMINI_API_KEYS only.
import p2p_payment_ai

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)
templates = Jinja2Templates(directory="templates")

# Jarayon to'xtatilganda fon ishchilarini tartibli yakunlash uchun lifecycle holati.
_APP_STOP_EVENT = threading.Event()
_APP_WORKER_THREADS = []

raw_admin_id = os.getenv("ADMIN_ID", "").strip()
try:
    ADMIN_ID = int(raw_admin_id) if raw_admin_id else None
except (TypeError, ValueError):
    logging.error("ADMIN_ID noto'g'ri formatda; asosiy admin ID o'chirib qo'yildi.")
    ADMIN_ID = None

# Qo'shimcha administratorlar vergul bilan ajratilgan Telegram ID ro'yxati.
# ADMIN_ID orqaga moslik uchun asosiy administrator sifatida saqlanadi.
ADMIN_TELEGRAM_IDS = set()
for _raw_id in os.getenv("ADMIN_TELEGRAM_IDS", "").split(","):
    _raw_id = _raw_id.strip()
    if not _raw_id:
        continue
    try:
        _parsed_id = int(_raw_id)
        if _parsed_id > 0:
            ADMIN_TELEGRAM_IDS.add(_parsed_id)
        else:
            logging.warning("ADMIN_TELEGRAM_IDS ichida musbat bo'lmagan ID e'tiborsiz qoldirildi.")
    except ValueError:
        logging.warning("ADMIN_TELEGRAM_IDS ichida noto'g'ri ID e'tiborsiz qoldirildi.")

ADMIN_IDS = set(ADMIN_TELEGRAM_IDS)
if ADMIN_ID and ADMIN_ID > 0:
    ADMIN_IDS.add(ADMIN_ID)


def is_admin_id(user_id):
    """Telegram ID asosida administrator huquqini markaziy tekshirish."""
    try:
        return int(user_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


# --- ADMIN ERROR ALERTS ---
# ERROR/CRITICAL log yozuvlarini administratorga Telegram orqali yuboradi.
# Xabarlar ko'payib ketmasligi uchun 60 soniyada ko'pi bilan bitta alert yuboriladi.
class AdminTelegramErrorHandler(logging.Handler):
    def __init__(self, cooldown_seconds=60):
        super().__init__(level=logging.ERROR)
        self.cooldown_seconds = cooldown_seconds
        self._alert_lock = threading.Lock()
        self._last_alert_at = 0.0
        self._suppressed_count = 0
        self._sending_alert = False

    def emit(self, record):
        if not ADMIN_ID or getattr(self, "_sending_alert", False):
            return

        now = time.monotonic()
        with self._alert_lock:
            if now - self._last_alert_at < self.cooldown_seconds:
                self._suppressed_count += 1
                return
            suppressed = self._suppressed_count
            self._suppressed_count = 0
            self._last_alert_at = now

        try:
            details = self.format(record)
            # Telegram xabar uzunligi va maxfiy ma'lumotlar xavfini kamaytirish uchun qisqartiramiz.
            details = details[:2500]
            message = (
                "🚨 <b>Quiz AI — Server xatoligi</b>\n"
                f"🕒 {datetime.now(timezone(timedelta(hours=5))).strftime('%d-%m-%Y %H:%M:%S')} (O‘zbekiston)\n"
                f"📍 Modul: <code>{record.name[:120]}</code>\n\n"
                f"<pre>{details.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')[:2500]}</pre>"
            )
            if suppressed:
                message += f"\n\nℹ️ Oxirgi xabardan beri yana {suppressed} ta xatolik logi birlashtirildi."
            self._sending_alert = True
            bot.send_message(ADMIN_ID, message, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            # Telegram ogohlantirishining o'zi muvaffaqiyatsiz bo'lsa, cheksiz log aylanishini boshlamaymiz.
            pass
        finally:
            self._sending_alert = False


_admin_error_handler = AdminTelegramErrorHandler()
_admin_error_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logging.getLogger().addHandler(_admin_error_handler)

# --- ADMIN SUPPORT ---
support_waiting_users = set()
support_reply_targets = {}

# Ommaviy xabar yuborish holati (faqat administrator uchun).
broadcast_waiting_content = set()
broadcast_pending_messages = {}
broadcast_active = False
broadcast_lock = threading.Lock()

# Test yaratish API kalitlari:
# 1) Eski usul saqlanadi: GOOGLE_API_KEYS=key1,key2,key3
# 2) Railway Variables'da alohida o'zgaruvchilar: GOOGLE_API_KEY_001 ... GOOGLE_API_KEY_110
# 3) Muqobil nomlash ham qo'llab-quvvatlanadi: GEMINI_API_KEY_001 ... GEMINI_API_KEY_110
# Kalit qiymatlari hech qachon logga chiqarilmaydi.
def _load_generation_api_keys():
    collected = []
    raw_keys = os.getenv("GOOGLE_API_KEYS", "")
    if raw_keys:
        collected.extend(k.strip() for k in raw_keys.split(",") if k.strip())

    for prefix in ("GOOGLE_API_KEY_", "GEMINI_API_KEY_"):
        indexed = []
        for env_name, env_value in os.environ.items():
            if not env_name.startswith(prefix) or not env_value or not env_value.strip():
                continue
            suffix = env_name[len(prefix):]
            if suffix.isdigit():
                indexed.append((int(suffix), env_value.strip()))
        indexed.sort(key=lambda item: item[0])
        collected.extend(value for _, value in indexed)

    # Bir xil kalit ikki marta kiritilgan bo'lsa, takrorini olib tashlaymiz.
    return list(dict.fromkeys(collected))


GOOGLE_API_KEYS = _load_generation_api_keys()
current_key_index = 0
key_lock = threading.Lock()

def _next_generation_key_start(total_keys=None):
    """Atomically reserve a round-robin starting point for one quiz request."""
    global current_key_index
    count = len(GOOGLE_API_KEYS) if total_keys is None else int(total_keys)
    if count <= 0:
        return 0
    with key_lock:
        start = current_key_index % count
        current_key_index = (start + 1) % count
    return start

logging.info(
    "AI test kalitlari sozlamasi | topilgan_kalitlar_soni=%s | manba=GOOGLE_API_KEYS va raqamlangan Railway Variables",
    len(GOOGLE_API_KEYS),
)

# --- PROFESSIONAL AI MANAGER: KEY ROTATION != CONCURRENCY CONTROL ---
# API key rotation must never determine the number of simultaneous Gemini requests.
# This keeps the architecture stable when moving from multiple free keys to one
# paid key, or later to multiple independent projects/keys.
#
# Recommended Railway variables for production can be tuned without code changes:
#   GEMINI_MODEL=gemini-3.6-flash
#   GEMINI_FALLBACK_MODEL=gemini-2.5-flash
#   AI_MAX_CONCURRENT=10
#   AI_MAX_QUEUE=500
#   AI_RETRY_PER_KEY=2
#   AI_REQUEST_TIMEOUT=600
#   AI_TOTAL_TIMEOUT=1800
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash"
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
AI_MAX_CONCURRENT = max(1, int(os.getenv("AI_MAX_CONCURRENT", "10")))
AI_MAX_QUEUE = max(AI_MAX_CONCURRENT, int(os.getenv("AI_MAX_QUEUE", "500")))
AI_REQUEST_TIMEOUT = max(60, int(os.getenv("AI_REQUEST_TIMEOUT", "600")))
AI_TOTAL_TIMEOUT = max(AI_REQUEST_TIMEOUT, int(os.getenv("AI_TOTAL_TIMEOUT", "1800")))
AI_RETRY_PER_KEY = max(1, min(3, int(os.getenv("AI_RETRY_PER_KEY", "2"))))

# Katta testlar uchun chiqish tokenlari limitini Railway Variables orqali boshqarish.
GEMINI_MAX_OUTPUT_TOKENS = max(1024, int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "65536")))

# Explanation generation is configurable. When disabled, the schema still stays
# compatible, but Gemini returns an empty explanation string instead of spending
# output tokens on explanations. Default remains ON for the educational value.
GEMINI_INCLUDE_EXPLANATIONS = os.getenv("GEMINI_INCLUDE_EXPLANATIONS", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
# Promptni ixchamlashtirish Railway Variables orqali boshqariladi.
# AI_PROMPT_OPTIMIZATION=false bo'lsa, avvalgi batafsil prompt ishlatiladi.
AI_PROMPT_OPTIMIZATION = os.getenv("AI_PROMPT_OPTIMIZATION", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
logging.info(
    "AI prompt optimallashtirish sozlamasi | holat=%s",
    "YOQILGAN" if AI_PROMPT_OPTIMIZATION else "O'CHIRILGAN"
)
# Savol variantlari uzunligini muvozanatlash va izohlarni ixcham yozish Railway Variables orqali boshqariladi.
GEMINI_BALANCE_OPTIONS = os.getenv("GEMINI_BALANCE_OPTIONS", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
GEMINI_CONCISE_EXPLANATIONS = os.getenv("GEMINI_CONCISE_EXPLANATIONS", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
logging.info(
    "AI javob uslubi | variantlar_muvozanati=%s | ixcham_izohlar=%s",
    "YOQILGAN" if GEMINI_BALANCE_OPTIONS else "O'CHIRILGAN",
    "YOQILGAN" if GEMINI_CONCISE_EXPLANATIONS else "O'CHIRILGAN"
)

# Input protection: pasted text gets a visible hard limit; extracted files are
# protected separately below. This prevents unexpectedly large requests entering
# the AI queue. Large books should be split/batched rather than silently truncated.
MAX_TEXT_INPUT_CHARS = max(1, int(os.getenv("MAX_TEXT_INPUT_CHARS", "80000")))

# Queue slot so'rovni boshqaradi, Gemini semaphore esa haqiqiy parallel
# AI so'rovlar sonini cheklaydi. Queue va concurrency mustaqil sozlanadi.
ai_queue_slots = threading.BoundedSemaphore(AI_MAX_QUEUE)
gemini_semaphore = threading.BoundedSemaphore(AI_MAX_CONCURRENT)

# AI navbati va API kalitlari bo'yicha joriy jarayon statistikasi.
_ai_monitor_lock = threading.Lock()
_ai_monitor_state = {
    "waiting_for_queue": 0,
    "accepted_requests": 0,
    "key_attempts": {},
    "key_success": {},
    "key_failures": {},
    "last_errors": [],
}
logging.info(
    "AI boshqaruvchisi ishga tushdi | asosiy_model=%s | zaxira_model=%s | bir_vaqtdagi_so'rovlar=%s | navbat_limit=%s | izohlar_yaratiladi=%s | bitta_so'rov_vaqti_limit=%ss | umumiy_vaqt_limit=%ss",
    GEMINI_MODEL, GEMINI_FALLBACK_MODEL, AI_MAX_CONCURRENT, AI_MAX_QUEUE,
    GEMINI_INCLUDE_EXPLANATIONS, AI_REQUEST_TIMEOUT, AI_TOTAL_TIMEOUT
)
logging.info(
    "IZOH SOZLAMASI | Gemini izoh yaratishi: %s",
    "YOQILGAN" if GEMINI_INCLUDE_EXPLANATIONS else "O'CHIRILGAN"
)

# --- PROFESSIONAL FILE PROTECTION LAYER ---
# Katta kitob yoki juda og'ir fayllar server, parser va AI Queue ga
# ortiqcha yuk bermasligi uchun yuklashdan oldin tekshiriladi.
MAX_UPLOAD_FILE_MB = max(1, int(os.getenv("MAX_UPLOAD_FILE_MB", "10")))
MAX_UPLOAD_FILE_BYTES = MAX_UPLOAD_FILE_MB * 1024 * 1024
MAX_PDF_PAGES = max(1, int(os.getenv("MAX_PDF_PAGES", "150")))
MAX_EXTRACTED_TEXT_CHARS = max(1, int(os.getenv("MAX_EXTRACTED_TEXT_CHARS", "80000")))
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".docx"}

FILE_PROTECTION_MESSAGES = {
    "uz": {
        "unsupported": "Faqat PDF yoki DOCX fayl yuklash mumkin.",
        "too_large": f"Fayl hajmi {MAX_UPLOAD_FILE_MB} MB limitdan oshdi. Iltimos, faylni kichikroq qismlarga bo'lib yuboring.",
        "too_many_pages": f"PDF sahifalari soni {MAX_PDF_PAGES} ta limitdan oshdi. Iltimos, PDF faylni qismlarga bo'ling.",
        "too_much_text": "Fayldagi matn hajmi juda katta. Iltimos, faylni kichikroq qismlarga bo'lib yuboring.",
        "text_too_long": f"Kiritilgan matn {MAX_TEXT_INPUT_CHARS:,} ta belgidan oshmasligi kerak. Iltimos, matnni qismlarga bo'ling.",
        "unreadable": "Faylni o'qib bo'lmadi. Matnli PDF yoki to'g'ri DOCX fayl yuboring.",
    },
    "ru": {
        "unsupported": "Можно загрузить только файл PDF или DOCX.",
        "too_large": f"Размер файла превышает лимит {MAX_UPLOAD_FILE_MB} МБ. Пожалуйста, разделите файл на меньшие части.",
        "too_many_pages": f"Количество страниц PDF превышает лимит {MAX_PDF_PAGES}. Пожалуйста, разделите PDF на части.",
        "too_much_text": "Объём текста в файле слишком большой. Пожалуйста, разделите файл на меньшие части.",
        "unreadable": "Не удалось прочитать файл. Отправьте текстовый PDF или корректный DOCX-файл.",
    },
    "en": {
        "unsupported": "Only PDF or DOCX files can be uploaded.",
        "too_large": f"The file exceeds the {MAX_UPLOAD_FILE_MB} MB limit. Please split it into smaller parts.",
        "too_many_pages": f"The PDF exceeds the {MAX_PDF_PAGES}-page limit. Please split the PDF into smaller parts.",
        "too_much_text": "The amount of text in the file is too large. Please split the file into smaller parts.",
        "unreadable": "The file could not be read. Please upload a text-based PDF or a valid DOCX file.",
    },
}

def file_protection_message(lang: str, key: str) -> str:
    return FILE_PROTECTION_MESSAGES.get(lang, FILE_PROTECTION_MESSAGES["uz"]).get(key, FILE_PROTECTION_MESSAGES["uz"]["unreadable"])

DOWNLOADS_DIR = "downloads"
DB_PATH = (
    "/data/quiz_pilot_v2.db" if os.path.exists("/data") else "quiz_pilot_v2.db"
)

# --- TARIFLAR: Railway Variables yagona manba ---
def _env_positive_int(name, default, minimum=1):
    try:
        value = int(os.getenv(name, str(default)).strip())
        return value if value >= minimum else default
    except (TypeError, ValueError):
        logging.warning("Railway sozlamasi noto'g'ri | o'zgaruvchi=%s | standart qiymat ishlatiladi", name)
        return default

def _format_price(amount):
    return f"{amount:,}".replace(",", " ") + " so'm"

_daily_price = _env_positive_int("TARIFF_DAILY_PRICE", 10000)
_weekly_price = _env_positive_int("TARIFF_WEEKLY_PRICE", 35000)
_monthly_price = _env_positive_int("TARIFF_MONTHLY_PRICE", 65000)
_teachers_price = _env_positive_int("TARIFF_TEACHERS_PRICE", 95000)
_daily_hours = _env_positive_int("TARIFF_DAILY_HOURS", 24)
_weekly_days = _env_positive_int("TARIFF_WEEKLY_DAYS", 7)
_monthly_days = _env_positive_int("TARIFF_MONTHLY_DAYS", 30)
_teachers_days = _env_positive_int("TARIFF_TEACHERS_DAYS", 30)
TARIFFS = {
    "daily": {"price": _format_price(_daily_price), "price_amount": _daily_price, "duration": _daily_hours * 3600,
              "duration_value": _daily_hours, "duration_unit": "hours",
              "names": {"uz": f"Kunlik Cheksiz ({_daily_hours} soat)", "ru": f"Суточный Безлимит ({_daily_hours} ч)", "en": f"Daily Unlimited ({_daily_hours}h)"}},
    "weekly": {"price": _format_price(_weekly_price), "price_amount": _weekly_price, "duration": _weekly_days * 86400,
               "duration_value": _weekly_days, "duration_unit": "days",
               "names": {"uz": f"Haftalik Cheksiz ({_weekly_days} kun)", "ru": f"Недельный Безлимит ({_weekly_days} дн)", "en": f"Weekly Unlimited ({_weekly_days}d)"}},
    "monthly": {"price": _format_price(_monthly_price), "price_amount": _monthly_price, "duration": _monthly_days * 86400,
                "duration_value": _monthly_days, "duration_unit": "days",
                "names": {"uz": f"Oylik Cheksiz ({_monthly_days} kun)", "ru": f"Месячный Безлимит ({_monthly_days} дн)", "en": f"Monthly Unlimited ({_monthly_days}d)"}},
    "teachers": {"price": _format_price(_teachers_price), "price_amount": _teachers_price, "duration": _teachers_days * 86400,
                 "duration_value": _teachers_days, "duration_unit": "days",
                 "names": {"uz": f"O'qituvchilar Uchun ({_teachers_days} kun)", "ru": f"Для Учителей ({_teachers_days} дн)", "en": f"For Teachers ({_teachers_days}d)"}},
}
logging.info("Tariflar Railway'dan yuklandi | kunlik=%s/%s soat | haftalik=%s/%s kun | oylik=%s/%s kun | o'qituvchilar=%s/%s kun",
             TARIFFS['daily']['price'], _daily_hours, TARIFFS['weekly']['price'], _weekly_days,
             TARIFFS['monthly']['price'], _monthly_days, TARIFFS['teachers']['price'], _teachers_days)
# Bepul limitlar: musbat son = limit, -1 = cheksiz. 0 va boshqa manfiy qiymatlar yaroqsiz.
def _env_free_limit(name, default=3):
    raw = os.getenv(name, str(default))
    try:
        value = int(str(raw).strip())
        if value == -1 or value >= 1:
            return value
    except (TypeError, ValueError):
        pass
    logging.warning("Bepul limit sozlamasi noto'g'ri; standart qiymat ishlatiladi | o'zgaruvchi=%s", name)
    return default

FREE_QUIZ_LIMIT = _env_free_limit("FREE_QUIZ_LIMIT", 3)
FREE_PUBLIC_LIMIT = _env_free_limit("FREE_PUBLIC_LIMIT", 3)
FREE_FLASHCARD_LIMIT = _env_free_limit("FREE_FLASHCARD_LIMIT", 3)

def format_free_limit(limit, lang="uz"):
    if limit == -1:
        return {"uz": "Cheksiz", "ru": "Безлимитно", "en": "Unlimited"}.get(lang, "Cheksiz")
    return str(limit)

def free_limit_remaining(limit, used):
    return None if limit == -1 else max(0, limit - int(used or 0))

def free_limit_reached(limit, used):
    return limit != -1 and int(used or 0) >= limit

def tariff_lines(lang="uz"):
    labels = {
        "uz": ("Kunlik", "Haftalik", "Oylik", "O'qituvchilar"),
        "ru": ("Суточный", "Недельный", "Месячный", "Для учителей"),
        "en": ("Daily", "Weekly", "Monthly", "Teachers"),
    }.get(lang, ("Kunlik", "Haftalik", "Oylik", "O'qituvchilar"))
    return "\n".join(
        f"💎 {label} — {TARIFFS[key]['price']}"
        for label, key in zip(labels, ("daily", "weekly", "monthly", "teachers"))
    )

logging.info(
    "Bepul limitlar sozlamasi | AI_test=%s | ommaviy_test=%s | flash_kartochka=%s",
    FREE_QUIZ_LIMIT, FREE_PUBLIC_LIMIT, FREE_FLASHCARD_LIMIT
)
P2P_CARD_NUMBER = re.sub(r"\D", "", os.getenv("P2P_CARD_NUMBER", "").strip())
PAYMENT_CARDHOLDER = os.getenv("PAYMENT_CARDHOLDER", "").strip()

def is_active_paid_status(status: str, premium_until: int) -> bool:
    return bool(status and "PRO" in status and premium_until and int(time.time()) <= premium_until)

def get_plan_key(status: str, plan_key: str = "") -> str:
    if plan_key in TARIFFS:
        return plan_key
    s = (status or "").lower()
    if "teacher" in s or "o'qit" in s or "учител" in s:
        return "teachers"
    if "weekly" in s or "haftalik" in s or "недель" in s:
        return "weekly"
    if "monthly" in s or "oylik" in s or "месяч" in s:
        return "monthly"
    if "daily" in s or "kunlik" in s or "суточ" in s:
        return "daily"
    return ""

def localized_tariff_name(plan_key: str, lang: str) -> str:
    lang = lang if lang in ("uz", "ru", "en") else "uz"
    return TARIFFS.get(plan_key, {}).get("names", {}).get(lang, plan_key)

# --- MULTILINGUAL (3 TILDAGI BILDIRISHNOMALAR) ---
MESSAGES = {
    "uz": {
        "welcome": (
            "👋 *Salom, {name}!*\n"
            "🎓 *Quiz AI* — AI yordamida savollarni tez va qulay testga aylantiruvchi ilova.\n\n"
            "✨ *Ilova imkoniyatlari:*\n"
            "🤖 AI yordamida matn, PDF yoki DOCX dan test yaratish.\n"
            "📚 Testlarni Kutubxona bo'limida saqlash va ishlash.\n"
            "🌐 Ommaviy testlardan foydalanish.\n"
            "🗂 Flash Kartochkalar orqali takrorlash.\n"
            "👥 Guruhlarga qo'shilish va guruh testlarida qatnashish.\n"
            "👨‍🏫 O'qituvchilar uchun Professional vositalar.\n"
            "📊 Natijalarni qulay ko'rish va tahlil qilish.\n"
            "🔊 Ovoz va vibratsiya sozlamalari.\n"
            "🌍 O'zbek, Русский va English tillari.\n"
            "🆓 *Bepul (har 30 kunda):* AI test — {quiz_limit}, ommaviy test — {public_limit}, Flash Kartochka — {flashcard_limit}.\n"
            "👑 *Premium:* limitlarsiz foydalanish imkoniyati.\n\n"
            "📌 *Eslatma:* Premium bo'limida «Tariflarni faollashtirish» tugmasi bosilganda yangi oyna ochiladi. Shu oynadagi «Chekni yuborish» tugmasini bosing — bu sizni botga qaytaradi. Soʻng toʻlov chekini rasm yoki skrinshot shaklida yuboring.\n\n"
            "💬 *Bizning rasmiy guruhimiz:* [Quiz AI Rasmiy Chat](https://t.me/Quiz_AI_Chat)\n\n"
            "📌 *Ilovada test yaratish va Premium olish usullari guruhimizga video shaklida joylashtirilgan, tanishib chiqing.*\n\n"
        ),
        "open_app": "Ilovani ochish 📱",
        "payment_prompt": (
            "🧾 Siz {tariff_name} ({tariff_price}) tarifini tanladingiz.\n\n"
            "Iltimos, plastik kartaga to'lov qilganingiz haqidagi To'lov Chekini "
            "(Rasm/Skrinshot ko'rinishida) shu yerga yuboring.\n"
            "Sizning buyurtma raqamingiz: {tx_id}"
        ),
        "receipt_received": "✅ Rahmat! To'lov chekingiz administratorga yuborildi. Tez orada tekshirilib, tarifingiz faollashtiriladi.",
        "receipt_error": "⚠️ To'lov chekingiz qabul qilindi, biroq adminga bildirishnoma yuborishda muammo bo'ldi. Admin paneldan tekshiriladi.",
        "payment_approved": "🎉 Tabriklaymiz! Sizning {tariff_name} tarifi uchun qilgan to'lovingiz tasdiqlandi. Ilovada PRO status faollashdi! 👑",
        "payment_rejected": "❌ Siz yuborgan to'lov cheki qabul qilinmadi yoki rad etildi. Agar xatolik bo'lgan deb o'ylasangiz, administratorga murojaat qiling.",
        "quiz_limit_reached": "🔒 Bepul limit tugadi. Test yaratishni davom ettirish uchun Premium tarifga o'ting. 👑",
        "public_limit_reached": "🔒 Bepul limit tugadi. Ommaviy testlarni davom ettirish uchun Premium tarifga o'ting. 👑",
        "flashcard_limit_reached": "🔒 Bepul limit tugadi. Flash Kartochka yaratishni davom ettirish uchun Premium tarifga o'ting. 👑",
        "support_prompt": "💬 Admin bilan bog'lanish. Savolingiz yoki muammoingizni shu yerga yozing. Xabaringiz administratorga yuboriladi.",
        "support_sent": "✅ Murojaatingiz administratorga yuborildi. Javob kelishini kuting.",
        "support_continue_btn": "💬 Admin bilan bog'lanish",
        "support_admin_title": "📩 Yangi murojaat",
        "support_reply_btn": "✉️ Javob berish",
        "support_reply_prompt": "✍️ Javobingizni yozing. U foydalanuvchiga yuboriladi.",
        "support_reply_sent": "✅ Javob foydalanuvchiga yuborildi.",
        "support_admin_reply_title": "Admin javobi",
        "support_config_error": "⚠️ Admin bilan bog'lanish hozircha sozlanmagan. Iltimos, keyinroq urinib ko'ring.",
        "quiz_ready": "📝 {title} darsligi bo'yicha jami {count} ta test savoli muvaffaqiyatli tayyorlandi!",
        "free_quiz_limit_notice": "🔒 *Bepul AI test limiti tugadi!*\n\nSizga ajratilgan {limit} ta bepul AI testdan foydalanib bo'ldingiz. Yangi testlar yaratishda davom etish uchun 👑 *Premium tarif*ni tavsiya qilamiz.\n\n{tariff_lines}\n\n🚀 Premium bo'limidan o'zingizga mos tarifni tanlashingiz mumkin.",
        "free_public_limit_notice": "🔒 *Bepul ommaviy test limiti tugadi!*\n\nSiz 30 kunlik bepul {limit} ta ommaviy test limitidan foydalanib bo'ldingiz. Davom etish uchun 👑 *Premium tarif*ni tavsiya qilamiz.\n\n🚀 Premium bo'limidan tarifni tanlang.",
        "free_flashcard_limit_notice": "🔒 *Bepul Flash Kartochka limiti tugadi!*\n\nSiz 30 kunlik bepul {limit} ta Flash Kartochka limitidan foydalanib bo'ldingiz. Davom etish uchun 👑 *Premium tarif*ni tavsiya qilamiz.\n\n🚀 Premium bo'limidan tarifni tanlang.",
        "paid_limit_notice": "⏳ *Premium limitingiz tugadi!*\n\nSizning {tariff_name} tarifingiz muddati yakunlandi. 👑 Premium imkoniyatlardan yana foydalanish uchun tarifni yangilashni tavsiya qilamiz.\n\n{tariff_lines}\n\n🚀 Premium bo'limidan yangi tarifni tanlang.",
        "free_limits_restored_notice": "🎉 *Bepul limitlaringiz qaytdi!*\n\nSizning 30 kunlik bepul limit davringiz yangilandi. Endi bepul limitlaringiz: AI test — {quiz_limit}, ommaviy test — {public_limit}, Flash Kartochka — {flashcard_limit}.\n\n🚀 Quiz Pilot Bot’dan foydalanishda davom eting!",
    },
    "ru": {
        "welcome": (
            "👋 *Привет, {name}!*\n"
            "🎓 *Quiz AI* — Приложение, которое с помощью ИИ быстро и удобно превращает вопросы в тесты.\n\n"
            "✨ *Возможности приложения:*\n"
            "🤖 Создание тестов с помощью ИИ из текста, PDF или DOCX.\n"
            "📚 Сохранение и прохождение тестов в разделе «Библиотека».\n"
            "🌐 Публичные тесты.\n"
            "🗂 Флеш-карточки для повторения материала.\n"
            "👥 Вступление в группы и участие в групповых тестах.\n"
            "👨‍🏫 Профессиональные инструменты для учителей.\n"
            "📊 Удобный просмотр и анализ результатов.\n"
            "🔊 Настройки звука и вибрации.\n"
            "🌍 Узбекский, русский и английский языки.\n"
            "🆓 *Бесплатно (каждые 30 дней):* AI-тесты — {quiz_limit}, публичные тесты — {public_limit}, флеш-карточки — {flashcard_limit}.\n"
            "👑 *Premium:* использование без лимитов.\n\n"
            "📌 *Примечание:* В разделе Премиум при нажатии на кнопку «Активировать тарифы» откроется новое окно. Нажмите в этом окне кнопку «Отправить чек» — это вернёт вас в бот. Затем отправьте чек об оплате в виде фото или скриншота.\n\n"
            "💬 *Наша официальная группа:* [Quiz AI Официальный Чат](https://t.me/Quiz_AI_Chat)\n\n"
            "📌 *Способы создания тестов и получения Премиума в приложении опубликованы в нашей группе в виде видео, пожалуйста, ознакомьтесь.*\n\n"
        ),
        "open_app": "Открыть приложение 📱",
        "payment_prompt": (
            "🧾 Вы выбрали тариф {tariff_name} ({tariff_price}).\n\n"
            "Пожалуйста, отправьте чек об оплате (в виде фото/скриншота) сюда.\n"
            "Ваш номер заказа: {tx_id}"
        ),
        "receipt_received": "✅ Спасибо! Ваш чек отправлен администратору. В ближайшее время он будет проверен, и ваш тариф активируется.",
        "receipt_error": "⚠️ Ваш чек принят, но возникла проблема с отправкой уведомления администратору. Он будет проверен через админ-панель.",
        "payment_approved": "🎉 Поздравляем! Ваш платеж по тарифу {tariff_name} подтвержден. В приложении активирован PRO статус! 👑",
        "payment_rejected": "❌ Ваш чек об оплате был отклонен. Если вы считаете, что произошла ошибка, свяжитесь с администратором.",
        "quiz_limit_reached": "🔒 Бесплатный лимит исчерпан. Чтобы продолжить создавать тесты, перейдите на Premium тариф. 👑",
        "public_limit_reached": "🔒 Бесплатный лимит исчерпан. Чтобы продолжить проходить публичные тесты, перейдите на Premium тариф. 👑",
        "flashcard_limit_reached": "🔒 Бесплатный лимит исчерпан. Чтобы продолжить создавать флеш-карточки, перейдите на Premium тариф. 👑",
        "support_prompt": "💬 Связаться с администратором. Напишите ваш вопрос или проблему здесь. Сообщение будет отправлено администратору.",
        "support_sent": "✅ Ваше обращение отправлено администратору. Ожидайте ответа.",
        "support_continue_btn": "💬 Связаться с администратором",
        "support_admin_title": "📩 Новое обращение",
        "support_reply_btn": "✉️ Ответить",
        "support_reply_prompt": "✍️ Напишите ответ. Он будет отправлен пользователю.",
        "support_reply_sent": "✅ Ответ отправлен пользователю.",
        "support_admin_reply_title": "Ответ администратора",
        "support_config_error": "⚠️ Связь с администратором пока не настроена. Пожалуйста, попробуйте позже.",
        "quiz_ready": "📝 Успешно подготовлено {count} тестовых вопросов по материалу {title}!",
        "free_quiz_limit_notice": "🔒 *Бесплатный лимит AI-тестов исчерпан!*\n\nВы использовали все {limit} бесплатных AI-тестов. Для продолжения рекомендуем 👑 *Premium тариф*.\n\n{tariff_lines}\n\n🚀 Выберите подходящий тариф в разделе Premium.",
        "free_public_limit_notice": "🔒 *Бесплатный лимит публичных тестов исчерпан!*\n\nВы использовали {limit} бесплатных публичных тестов за 30 дней. Для продолжения рекомендуем 👑 *Premium тариф*.\n\n🚀 Выберите тариф в разделе Premium.",
        "free_flashcard_limit_notice": "🔒 *Бесплатный лимит флеш-карточек исчерпан!*\n\nВы использовали {limit} бесплатные флеш-карточки за 30 дней. Для продолжения рекомендуем 👑 *Premium тариф*.\n\n🚀 Выберите тариф в разделе Premium.",
        "paid_limit_notice": "⏳ *Срок Premium тарифа истёк!*\n\nВаш тариф {tariff_name} завершён. 👑 Рекомендуем продлить Premium, чтобы снова пользоваться всеми возможностями без ограничений.\n\n{tariff_lines}\n\n🚀 Выберите новый тариф в разделе Premium.",
        "free_limits_restored_notice": "🎉 *Ваши бесплатные лимиты восстановлены!*\n\nВаш 30-дневный бесплатный период обновлён. Ваши бесплатные лимиты: AI-тесты — {quiz_limit}, публичные тесты — {public_limit}, флеш-карточки — {flashcard_limit}.\n\n🚀 Продолжайте пользоваться Quiz Pilot Bot!",
    },
    "en": {
        "welcome": (
            "👋 *Hello, {name}!*\n"
            "🎓 *Quiz AI* — An application that uses AI to quickly and conveniently turn questions into tests.\n\n"
            "✨ *App features:*\n"
            "🤖 Create quizzes with AI from text, PDF or DOCX.\n"
            "📚 Save and take quizzes in the Library.\n"
            "🌐 Public quizzes.\n"
            "🗂 Flashcards for revision.\n"
            "👥 Join groups and participate in group quizzes.\n"
            "👨‍🏫 Professional tools for teachers.\n"
            "📊 Easy results viewing and analysis.\n"
            "🔊 Sound and vibration settings.\n"
            "🌍 Uzbek, Russian and English languages.\n"
            "🆓 *Free (every 30 days):* AI quizzes — {quiz_limit}, public quizzes — {public_limit}, flashcards — {flashcard_limit}.\n"
            "👑 *Premium:* unlimited usage.\n\n"
            "📌 *Note:* In the Premium section, when you click on the «Activate tariffs» button, a new window opens. Click the «Send receipt» button in this window — this will return you to the bot. Then send the payment receipt in the form of a photo or screenshot.\n\n"
            "💬 *Our official group:* [Quiz AI Official Chat](https://t.me/Quiz_AI_Chat)\n\n"
            "📌 *Methods for creating tests and getting Premium in the app have been posted in our group as a video, please check them out.*\n\n"
        ),
        "open_app": "Open App 📱",
        "payment_prompt": (
            "🧾 You have selected the {tariff_name} ({tariff_price}) plan.\n\n"
            "Please send your payment receipt (as a Photo/Screenshot) here.\n"
            "Your Order ID is: {tx_id}"
        ),
        "receipt_received": "✅ Thank you! Your payment receipt has been sent to the administrator. It will be verified shortly, and your plan will be activated.",
        "receipt_error": "⚠️ Your receipt was received, but there was an issue notifying the admin. It will be reviewed via the admin panel.",
        "payment_approved": "🎉 Congratulations! Your payment for the {tariff_name} plan has been confirmed. PRO status is now active! 👑",
        "payment_rejected": "❌ Your payment receipt was rejected. If you believe this is an error, please contact support.",
        "quiz_limit_reached": "🔒 Your free limit has been reached. Upgrade to Premium to continue creating quizzes. 👑",
        "public_limit_reached": "🔒 Your free limit has been reached. Upgrade to Premium to continue taking public quizzes. 👑",
        "flashcard_limit_reached": "🔒 Your free limit has been reached. Upgrade to Premium to continue creating flashcards. 👑",
        "support_prompt": "💬 Contact Admin. Write your question or problem here. Your message will be sent to the administrator.",
        "support_sent": "✅ Your message has been sent to the administrator. Please wait for a reply.",
        "support_continue_btn": "💬 Contact Admin",
        "support_admin_title": "📩 New support request",
        "support_reply_btn": "✉️ Reply",
        "support_reply_prompt": "✍️ Write your reply. It will be sent to the user.",
        "support_reply_sent": "✅ Reply sent to the user.",
        "support_admin_reply_title": "Admin reply",
        "support_config_error": "⚠️ Contact with the administrator is not configured yet. Please try again later.",
        "quiz_ready": "📝 A total of {count} quiz questions for {title} have been successfully generated!",
        "free_quiz_limit_notice": "🔒 *Your free AI quiz limit has ended!*\n\nYou have used all {limit} free AI quizzes. To keep creating quizzes, we recommend 👑 *Premium*.\n\n{tariff_lines}\n\n🚀 Choose a plan in the Premium section.",
        "free_public_limit_notice": "🔒 *Your free public quiz limit has ended!*\n\nYou have used your {limit} free public quizzes for the 30-day period. To continue, we recommend 👑 *Premium*.\n\n🚀 Choose a plan in the Premium section.",
        "free_flashcard_limit_notice": "🔒 *Your free flashcard limit has ended!*\n\nYou have used your {limit} free flashcards for the 30-day period. To continue, we recommend 👑 *Premium*.\n\n🚀 Choose a plan in the Premium section.",
        "paid_limit_notice": "⏳ *Your Premium plan has expired!*\n\nYour {tariff_name} plan has ended. 👑 We recommend renewing Premium to continue using all features without limits.\n\n{tariff_lines}\n\n🚀 Choose a new plan in the Premium section.",
        "free_limits_restored_notice": "🎉 *Your free limits have been restored!*\n\nYour 30-day free period has been renewed. Your free limits are now: AI quizzes — {quiz_limit}, public quizzes — {public_limit}, flashcards — {flashcard_limit}.\n\n🚀 Keep enjoying Quiz Pilot Bot!",
    }
}

def _connect_db(timeout: float = 30):
    """SQLite MASTER uchun yagona ulanish standarti.

    WAL rejimi init_db() da bir marta o'rnatiladi; har bir so'rovda qayta
    PRAGMA journal_mode bajarilmaydi. busy_timeout lock holatida kutishni
    sqlite3.connect timeout bilan bir xil qiymatda boshqaradi.
    """
    wait_seconds = max(1.0, float(timeout))
    conn = sqlite3.connect(DB_PATH, timeout=wait_seconds, check_same_thread=False)
    conn.execute(f"PRAGMA busy_timeout={int(wait_seconds * 1000)}")
    return conn


def get_user_lang(user_id: int) -> str:
    try:
        conn = _connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0] in MESSAGES:
            return row[0]
    except Exception as e:
        logging.error(f"Foydalanuvchi tilini olishda xatolik: {e}")
    return "uz"


def init_db():
    conn = _connect_db()
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")

    cursor.execute("""CREATE TABLE IF NOT EXISTS quizzes (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        title TEXT,
        total INTEGER,
        answered INTEGER,
        quiz_json TEXT,
        created_at INTEGER,
        last_score INTEGER DEFAULT -1,
        last_percent INTEGER DEFAULT -1,
        is_public INTEGER DEFAULT 0)""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        created_at INTEGER,
        language TEXT DEFAULT 'uz',
        status TEXT DEFAULT 'Oddiy foydalanuvchi',
        plan_key TEXT DEFAULT '',
        free_used INTEGER DEFAULT 0,
        public_free_used INTEGER DEFAULT 0,
        flashcard_free_used INTEGER DEFAULT 0,
        premium_until INTEGER DEFAULT 0,
        last_active INTEGER DEFAULT 0,
        last_quiz_free_notice_cycle INTEGER DEFAULT 0,
        last_public_free_notice_cycle INTEGER DEFAULT 0,
        last_flashcard_free_notice_cycle INTEGER DEFAULT 0,
        last_free_reset_notice_cycle INTEGER DEFAULT 0,
        paid_limit_notice_until INTEGER DEFAULT 0,
        premium_source TEXT DEFAULT 'paid')""")

    cursor.execute("PRAGMA table_info(users);")
    columns = [col[1] for col in cursor.fetchall()]

    if "status" not in columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'Oddiy foydalanuvchi';")
        except Exception:
            pass
    if "free_used" not in columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN free_used INTEGER DEFAULT 0;")
        except Exception:
            pass
    if "premium_until" not in columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN premium_until INTEGER DEFAULT 0;")
        except Exception:
            pass
    if "public_free_used" not in columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN public_free_used INTEGER DEFAULT 0;")
        except Exception:
            pass
    if "flashcard_free_used" not in columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN flashcard_free_used INTEGER DEFAULT 0;")
        except Exception:
            pass
    if "plan_key" not in columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN plan_key TEXT DEFAULT '';" )
        except Exception:
            pass

    if "last_active" not in columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN last_active INTEGER DEFAULT 0;")
        except Exception:
            pass
    for _col in (
        "last_quiz_free_notice_cycle",
        "last_public_free_notice_cycle",
        "last_flashcard_free_notice_cycle",
        "last_free_reset_notice_cycle",
        "paid_limit_notice_until",
    ):
        if _col not in columns:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {_col} INTEGER DEFAULT 0;")
            except Exception:
                pass

    if "premium_source" not in columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN premium_source TEXT DEFAULT 'paid';")
        except Exception:
            pass

    # NULL bo'lib qolgan eski qiymatlarni avtomatik to'g'rilash
    cursor.execute("UPDATE users SET free_used = 0 WHERE free_used IS NULL;")
    cursor.execute("UPDATE users SET public_free_used = 0 WHERE public_free_used IS NULL;")
    cursor.execute("UPDATE users SET flashcard_free_used = 0 WHERE flashcard_free_used IS NULL;")
    cursor.execute("UPDATE users SET status = 'Oddiy foydalanuvchi' WHERE status IS NULL;")
    cursor.execute("UPDATE users SET premium_until = 0 WHERE premium_until IS NULL;")
    cursor.execute("UPDATE users SET plan_key = '' WHERE plan_key IS NULL;")

    cursor.execute("""CREATE TABLE IF NOT EXISTS flashcards (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        front TEXT,
        back TEXT,
        created_at INTEGER)""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS payments (
        tx_id TEXT PRIMARY KEY,
        user_id INTEGER,
        tariff_name TEXT,
        tariff_price TEXT,
        status TEXT DEFAULT 'pending',
        created_at INTEGER)""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_sessions (
        id TEXT PRIMARY KEY,
        owner_id INTEGER,
        quiz_id TEXT,
        code TEXT UNIQUE,
        duration_minutes INTEGER DEFAULT 30,
        created_at INTEGER,
        expires_at INTEGER,
        active INTEGER DEFAULT 1,
        deleted INTEGER DEFAULT 0,
        source_type TEXT DEFAULT 'group_test',
        assignment_id TEXT DEFAULT '')""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        user_id INTEGER,
        first_name TEXT,
        username TEXT,
        score INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        percent INTEGER DEFAULT 0,
        started_at INTEGER,
        finished_at INTEGER,
        UNIQUE(session_id, user_id))""")

    # O'qituvchilar uchun professional variantlar/guruhlar. Eski DB bilan mos:
    # IF NOT EXISTS + ehtiyotkor migration mavjud funksiyalarni o'zgartirmaydi.
    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_variants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id TEXT NOT NULL,
        variant_code TEXT NOT NULL,
        variant_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(quiz_id, variant_code))""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_groups (
        id TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        join_code TEXT DEFAULT '',
        created_at INTEGER NOT NULL,
        active INTEGER DEFAULT 1)""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        first_name TEXT DEFAULT '',
        username TEXT DEFAULT '',
        joined_at INTEGER NOT NULL,
        UNIQUE(group_id, user_id))""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_assignments (
        id TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        group_id TEXT NOT NULL,
        quiz_id TEXT NOT NULL,
        variant_code TEXT DEFAULT '',
        title TEXT DEFAULT '',
        due_at INTEGER DEFAULT 0,
        duration_minutes INTEGER DEFAULT 30,
        created_at INTEGER NOT NULL,
        active INTEGER DEFAULT 1)""")

    # Eski Railway/SQLite bazalarida Teacher jadvallari avvalgi versiyadan qolgan
    # bo‘lishi mumkin. CREATE TABLE IF NOT EXISTS mavjud jadvalga yangi ustunlarni
    # qo‘shmaydi, shuning uchun xavfsiz migration qilamiz. Bu faqat yetishmayotgan
    # Teacher ustunlarini qo‘shadi va boshqa funksiyalarga tegmaydi.
    teacher_migrations = {
        "teacher_sessions": {
            "group_id": "TEXT DEFAULT ''",
            "variant_code": "TEXT DEFAULT ''",
            "deleted": "INTEGER DEFAULT 0",
            "source_type": "TEXT DEFAULT 'group_test'",
            "assignment_id": "TEXT DEFAULT ''",
        },
        "teacher_participants": {
            "first_name": "TEXT DEFAULT ''",
            "username": "TEXT DEFAULT ''",
            "score": "INTEGER DEFAULT 0",
            "total": "INTEGER DEFAULT 0",
            "percent": "INTEGER DEFAULT 0",
            "started_at": "INTEGER DEFAULT 0",
            "finished_at": "INTEGER DEFAULT 0",
        },
        "teacher_variants": {
            "variant_code": "TEXT DEFAULT ''",
            "variant_json": "TEXT DEFAULT '{}'",
            "created_at": "INTEGER DEFAULT 0",
        },
        "teacher_groups": {
            "owner_id": "INTEGER DEFAULT 0",
            "name": "TEXT DEFAULT ''",
            "description": "TEXT DEFAULT ''",
            "join_code": "TEXT DEFAULT ''",
            "created_at": "INTEGER DEFAULT 0",
            "active": "INTEGER DEFAULT 1",
        },
        "teacher_group_members": {
            "group_id": "TEXT DEFAULT ''",
            "user_id": "INTEGER DEFAULT 0",
            "first_name": "TEXT DEFAULT ''",
            "username": "TEXT DEFAULT ''",
            "joined_at": "INTEGER DEFAULT 0",
        },
        "teacher_assignments": {
            "owner_id": "INTEGER DEFAULT 0",
            "group_id": "TEXT DEFAULT ''",
            "quiz_id": "TEXT DEFAULT ''",
            "variant_code": "TEXT DEFAULT ''",
            "title": "TEXT DEFAULT ''",
            "due_at": "INTEGER DEFAULT 0",
            "duration_minutes": "INTEGER DEFAULT 30",
            "created_at": "INTEGER DEFAULT 0",
            "active": "INTEGER DEFAULT 1",
        },
    }
    for table_name, columns in teacher_migrations.items():
        try:
            cursor.execute(f"PRAGMA table_info({table_name});")
            existing = {row[1] for row in cursor.fetchall()}
            for column_name, column_def in columns.items():
                if column_name not in existing:
                    try:
                        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
                    except Exception as migration_error:
                        logging.warning("Teacher DB migration %s.%s: %s", table_name, column_name, migration_error)
        except Exception as migration_error:
            logging.warning("Teacher DB schema check %s: %s", table_name, migration_error)

    # Eski yozuvlardagi NULL qiymatlar Teacher endpointlari uchun xavfsiz qiymatga o‘tkaziladi.
    for table_name, updates in {
        "teacher_groups": [("description", "''"), ("active", "1")],
        "teacher_assignments": [("variant_code", "''"), ("title", "''"), ("due_at", "0"), ("duration_minutes", "30"), ("active", "1")],
        "teacher_sessions": [("group_id", "''"), ("variant_code", "'" + "'" + "'"), ("deleted", "0"), ("source_type", "'group_test'"), ("assignment_id", "''")],
    }.items():
        for col, value in updates:
            try:
                cursor.execute(f"UPDATE {table_name} SET {col}={value} WHERE {col} IS NULL")
            except Exception:
                pass

    # Existing groups from older versions receive a 6-character join code.
    try:
        cursor.execute("SELECT id FROM teacher_groups WHERE COALESCE(join_code,'')='' AND active=1")
        for row in cursor.fetchall():
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
            while cursor.execute("SELECT 1 FROM teacher_groups WHERE join_code=? LIMIT 1", (code,)).fetchone():
                code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
            cursor.execute("UPDATE teacher_groups SET join_code=? WHERE id=?", (code, row[0]))
    except Exception as e:
        logging.warning("Teacher group code migration: %s", e)


    # Tez-tez ishlatiladigan filtrlash/join yo'nalishlari uchun xavfsiz indekslar.
    # Faqat non-unique indekslar: mavjud eski ma'lumotlarda migratsiya to'xtab qolmaydi.
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS idx_quizzes_user_created ON quizzes(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_quizzes_public_created ON quizzes(is_public, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_flashcards_user_created ON flashcards(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_payments_user_status_created ON payments(user_id, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_teacher_groups_owner_active ON teacher_groups(owner_id, active, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_teacher_members_user_group ON teacher_group_members(user_id, group_id)",
        "CREATE INDEX IF NOT EXISTS idx_teacher_assignments_owner_group ON teacher_assignments(owner_id, group_id, active)",
        "CREATE INDEX IF NOT EXISTS idx_teacher_sessions_owner_active ON teacher_sessions(owner_id, active, deleted, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_teacher_participants_session_user ON teacher_participants(session_id, user_id)",
    ):
        try:
            cursor.execute(index_sql)
        except Exception as index_error:
            logging.warning("SQLite indeks yaratishda xatolik: %s", index_error)

    conn.commit()
    conn.close()


init_db()

# ==================================================================
# YANGILIKLAR TIZIMI — SQLite MASTER + avtomatik UZ/RU/EN tarjima
# ==================================================================
# Muhim: News alohida app.db yoki PostgreSQL'da emas, asosiy DB_PATH'dagi
# SQLite MASTER bazada saqlanadi. Shu sabab Hybrid V2 uni ham avtomatik mirror qiladi.

NEWS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_uz TEXT NOT NULL,
    content_uz TEXT NOT NULL,
    title_ru TEXT,
    content_ru TEXT,
    title_en TEXT,
    content_en TEXT,
    created_at INTEGER NOT NULL DEFAULT 0
)
"""


def _news_columns(conn):
    return {row[1]: row[2] for row in conn.execute("PRAGMA table_info(news)").fetchall()}


def init_news_master_db():
    """Create/migrate the News table in SQLite MASTER before Hybrid starts."""
    conn = _connect_db(timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute(NEWS_TABLE_SQL)
        columns = _news_columns(conn)

        # Legacy News installations may have been created with SQLAlchemy DateTime.
        # Hybrid V2 uses INTEGER Unix timestamps in SQLite MASTER, so rebuild only
        # this disposable/small table when the old created_at type is incompatible.
        created_type = (columns.get("created_at") or "").upper()
        if "INT" not in created_type:
            conn.execute("ALTER TABLE news RENAME TO news_legacy_migration")
            conn.execute(NEWS_TABLE_SQL)
            legacy_rows = conn.execute("""
                SELECT id,title_uz,content_uz,title_ru,content_ru,title_en,content_en,created_at
                FROM news_legacy_migration ORDER BY id
            """).fetchall()
            for row in legacy_rows:
                raw_ts = row[7]
                if isinstance(raw_ts, (int, float)):
                    ts = int(raw_ts)
                elif raw_ts:
                    try:
                        ts = int(datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")).timestamp())
                    except Exception:
                        ts = int(time.time())
                else:
                    ts = int(time.time())
                conn.execute("""
                    INSERT OR IGNORE INTO news
                    (id,title_uz,content_uz,title_ru,content_ru,title_en,content_en,created_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (*row[:7], ts))
            conn.execute("DROP TABLE news_legacy_migration")
            columns = _news_columns(conn)

        # Keep the schema forward-compatible with older News rows.
        for name, sql_type in {
            "title_ru": "TEXT", "content_ru": "TEXT",
            "title_en": "TEXT", "content_en": "TEXT",
            "created_at": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE news ADD COLUMN {name} {sql_type}")

        # One-time migration from the previous standalone app.db News store.
        legacy_path = str(Path(DB_PATH).with_name("app.db"))
        master_abs = os.path.abspath(DB_PATH)
        legacy_abs = os.path.abspath(legacy_path)
        if legacy_abs != master_abs and os.path.exists(legacy_path):
            try:
                legacy = sqlite3.connect(legacy_path, timeout=10, check_same_thread=False)
                legacy.row_factory = sqlite3.Row
                legacy_cols = {r[1] for r in legacy.execute("PRAGMA table_info(news)").fetchall()}
                if {"id", "title_uz", "content_uz"}.issubset(legacy_cols):
                    rows = legacy.execute("""
                        SELECT id,title_uz,content_uz,title_ru,content_ru,title_en,content_en,created_at
                        FROM news ORDER BY id
                    """).fetchall()
                    for row in rows:
                        raw_ts = row["created_at"]
                        if isinstance(raw_ts, (int, float)):
                            ts = int(raw_ts)
                        elif raw_ts:
                            try:
                                ts = int(datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")).timestamp())
                            except Exception:
                                ts = int(time.time())
                        else:
                            ts = int(time.time())
                        conn.execute("""
                            INSERT OR IGNORE INTO news
                            (id,title_uz,content_uz,title_ru,content_ru,title_en,content_en,created_at)
                            VALUES (?,?,?,?,?,?,?,?)
                        """, (row["id"], row["title_uz"], row["content_uz"],
                               row["title_ru"] if "title_ru" in legacy_cols else "",
                               row["content_ru"] if "content_ru" in legacy_cols else "",
                               row["title_en"] if "title_en" in legacy_cols else "",
                               row["content_en"] if "content_en" in legacy_cols else "", ts))
                legacy.close()
            except Exception as migration_error:
                logging.warning("Legacy News app.db migration skipped: %s", migration_error)

        conn.execute("UPDATE news SET created_at=? WHERE created_at IS NULL OR created_at=0", (int(time.time()),))
        conn.commit()
    finally:
        conn.close()


# Must happen before hybrid_sync.start(), so News gets V2 triggers/outbox too.
init_news_master_db()


# --- FASTAPI ISHGA TUSHISHI (zamonaviy lifespan usuli) ---
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """FastAPI ishga tushishi va yopilishida fon ishchilarini boshqaradi."""
    global _APP_WORKER_THREADS
    _APP_STOP_EVENT.clear()
    _APP_WORKER_THREADS = []

    # P2P OCR alohida tizim; ishga tushmasa asosiy ilova ishlashda davom etadi.
    try:
        p2p_payment_ai.configure(DB_PATH, bot, ADMIN_ID, ADMIN_IDS)
        p2p_payment_ai.start()
    except Exception as p2p_error:
        logging.exception(
            "P2P to'lov AI tizimini ishga tushirib bo'lmadi; asosiy ilova ishlashda davom etadi: %s",
            p2p_error,
        )

    bot_thread = threading.Thread(target=start_bot_polling, name="telegram-bot-polling", daemon=True)
    limit_thread = threading.Thread(target=limit_notification_worker, name="limit-notification-worker", daemon=True)
    for worker_thread in (bot_thread, limit_thread):
        worker_thread.start()
        _APP_WORKER_THREADS.append(worker_thread)

    try:
        yield
    finally:
        logging.info("Ilova yopilmoqda: fon ishchilariga to'xtash signali yuborilmoqda.")
        _APP_STOP_EVENT.set()
        try:
            bot.stop_polling()
        except Exception:
            pass
        try:
            p2p_payment_ai.stop(timeout=8)
        except Exception:
            logging.exception("P2P OCR ishchilarini to'xtatishda xatolik")
        if hybrid_sync is not None:
            try:
                hybrid_sync.stop(timeout=5)
            except Exception:
                logging.exception("Hybrid Sync ishchisini to'xtatishda xatolik")
        for worker_thread in _APP_WORKER_THREADS:
            worker_thread.join(timeout=5)
        logging.info("Ilova fon ishchilarini to'xtatish bosqichi yakunlandi.")

# --- FASTAPI ENDPOINTS ---
app = FastAPI(lifespan=app_lifespan)

# Telegram Mini App autentifikatsiyasi: brauzerdan kelgan user_id ga ishonmaymiz.
# Har bir himoyalangan API so'rovi Telegram WebApp initData imzosini olib kelishi kerak.
try:
    TELEGRAM_INIT_DATA_MAX_AGE = max(60, min(86400, int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE", "86400"))))
except (TypeError, ValueError):
    TELEGRAM_INIT_DATA_MAX_AGE = 86400
    logging.warning("TELEGRAM_INIT_DATA_MAX_AGE noto'g'ri; 86400 soniya ishlatiladi.")

def verify_telegram_init_data(init_data: str):
    """Telegram WebApp initData HMAC-SHA256 imzosini va auth_date ni tekshiradi."""
    if not TELEGRAM_BOT_TOKEN or not init_data or len(init_data) > 16384:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(data) != len(pairs):  # Takroriy kalitlar bilan imzo talqinini chalkashtirishga yo'l qo'ymaymiz.
            return None
        received_hash = data.pop("hash", "")
        if not received_hash or len(received_hash) != 64:
            return None
        auth_date = int(data.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 60 or now - auth_date > TELEGRAM_INIT_DATA_MAX_AGE:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        user_payload = json.loads(data.get("user", "{}"))
        user_id = int(user_payload.get("id", 0))
        return user_id if user_id > 0 else None
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


@app.middleware("http")
async def telegram_webapp_auth_middleware(request: Request, call_next):
    """API'larda Telegram imzosini tek markazda tekshiradi va user_id soxtalashtirilishini to'sadi."""
    path = request.url.path
    if not path.startswith("/api/") or request.method == "OPTIONS":
        return await call_next(request)
    # Bu endpointlar maxfiy foydalanuvchi ma'lumotini qaytarmaydi.
    if request.method == "GET" and path in {"/api/tariff-config", "/api/upload-limits"}:
        return await call_next(request)
    # Bir martalik token bilan himoyalangan yuklash GET so'rovi Telegram WebView
    # tomonidan yuboriladi; bu so'rovda maxsus initData headerini qo'shib bo'lmaydi.
    if request.method == "GET" and path.startswith("/api/teacher-download/"):
        return await call_next(request)

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    verified_user_id = verify_telegram_init_data(init_data)
    if not verified_user_id:
        return JSONResponse(status_code=401, content={"detail": "Telegram autentifikatsiyasi tasdiqlanmadi"})

    # URL query parametri orqali boshqa foydalanuvchi ID sini yuborishga yo'l qo'ymaymiz.
    supplied_ids = request.query_params.getlist("user_id")
    if any(not value.isdigit() or int(value) != verified_user_id for value in supplied_ids):
        return JSONResponse(status_code=403, content={"detail": "Foydalanuvchi ID mos kelmadi"})

    # JSON, form-data va URL-encoded body ichidagi user_id ham tekshiriladi.
    content_type = request.headers.get("content-type", "").lower()
    if any(kind in content_type for kind in ("application/json", "multipart/form-data", "application/x-www-form-urlencoded")):
        try:
            raw_body = await request.body()
            body_user_ids = []
            if "application/json" in content_type and raw_body:
                payload = json.loads(raw_body.decode("utf-8"))
                if isinstance(payload, dict) and "user_id" in payload:
                    body_user_ids.append(payload.get("user_id"))
            elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
                form = await request.form()
                body_user_ids.extend(form.getlist("user_id"))
                # Upload vaqtinchalik fayllarini tekshiruvdan keyin yopamiz; body endpoint uchun qayta uzatiladi.
                for value in form.values():
                    if hasattr(value, "close"):
                        await value.close()
            if any(str(value) != str(verified_user_id) for value in body_user_ids):
                return JSONResponse(status_code=403, content={"detail": "Foydalanuvchi ID mos kelmadi"})

            # Body tekshirildi; original baytlarni FastAPI endpointiga qayta uzatamiz.
            async def receive_body():
                return {"type": "http.request", "body": raw_body, "more_body": False}
            request = Request(request.scope, receive_body)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return JSONResponse(status_code=400, content={"detail": "Noto'g'ri so'rov tanasi"})

    request.state.telegram_user_id = verified_user_id
    response = await call_next(request)
    return response


class NewsCreateSchema(BaseModel):
    title_uz: str = Field(min_length=1, max_length=255)
    content_uz: str = Field(min_length=1, max_length=20000)


def translate_to_ru_and_en(text_uz: str):
    if not text_uz or not text_uz.strip():
        return "", ""
    try:
        text_ru = GoogleTranslator(source="uz", target="ru").translate(text_uz)
    except Exception as e:
        logging.warning("[RU Tarjima Xatosi]: %s", e)
        text_ru = text_uz

    try:
        text_en = GoogleTranslator(source="uz", target="en").translate(text_uz)
    except Exception as e:
        logging.warning("[EN Tarjima Xatosi]: %s", e)
        text_en = text_uz

    return text_ru, text_en


@app.post("/api/news")
def create_news_api(news_data: NewsCreateSchema, request: Request):
    if not is_admin_id(request.state.telegram_user_id):
        raise HTTPException(status_code=403, detail="Admin huquqi talab qilinadi")
    title_ru, title_en = translate_to_ru_and_en(news_data.title_uz)
    content_ru, content_en = translate_to_ru_and_en(news_data.content_uz)
    now = int(time.time())

    conn = _connect_db(timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        cur = conn.execute("""
            INSERT INTO news
            (title_uz,content_uz,title_ru,content_ru,title_en,content_en,created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (news_data.title_uz, news_data.content_uz,
              title_ru, content_ru, title_en, content_en, now))
        news_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "data": {
            "id": news_id,
            "title_uz": news_data.title_uz,
            "content_uz": news_data.content_uz,
            "title_ru": title_ru,
            "content_ru": content_ru,
            "title_en": title_en,
            "content_en": content_en,
            "created_at": now,
        },
    }


@app.get("/api/news")
def get_news_api(lang: str = Query("uz"), user_id: Optional[int] = Query(None)):
    # Foydalanuvchining saqlangan ilova tili News uchun ustuvor.
    if user_id is not None:
        lang = get_user_lang(user_id)
    else:
        lang = lang if lang in ("uz", "ru", "en") else "uz"
    conn = _connect_db(timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        rows = conn.execute("""
            SELECT id,title_uz,content_uz,title_ru,content_ru,title_en,content_en,created_at
            FROM news ORDER BY created_at DESC, id DESC LIMIT 100
        """).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        if lang == "ru":
            title = row["title_ru"] or row["title_uz"]
            content = row["content_ru"] or row["content_uz"]
        elif lang == "en":
            title = row["title_en"] or row["title_uz"]
            content = row["content_en"] or row["content_uz"]
        else:
            title = row["title_uz"]
            content = row["content_uz"]

        ts = int(row["created_at"] or 0)
        created_text = time.strftime("%d-%m-%Y %H:%M", time.gmtime(ts + 5 * 3600)) if ts else ""  # Uzbekistan (UTC+5)
        result.append({
            "id": row["id"],
            "title": title,
            "content": content,
            "created_at": created_text,
        })

    return {"status": "success", "data": result}



# --- HYBRID DATABASE V1 ---
# SQLite remains the MASTER database and all existing application code stays unchanged.
# PostgreSQL is an asynchronous mirror. Any PostgreSQL error must never stop SQLite/app requests.
hybrid_sync = None
try:
    import hybrid_sync
    hybrid_sync.start(DB_PATH)
except Exception as hybrid_db_error:
    logging.exception("Hybrid PostgreSQL mirror could not start; SQLite continues normally: %s", hybrid_db_error)


class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="Jami 4 ta variant ro'yxati (Variant harflarisiz)")
    correct_index: int = Field(description="To'g'ri javob indeks (0 dan 3 gacha)")
    explanation: str = Field(description="Ushbu javob nega to'g'riligini tushuntiruvchi qisqa izoh")


class QuizItemWithoutExplanation(BaseModel):
    """Izoh o'chirilganda Gemini javob sxemasida izoh maydoni bo'lmaydi."""
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="Jami 4 ta variant ro'yxati (Variant harflarisiz)")
    correct_index: int = Field(description="To'g'ri javob indeks (0 dan 3 gacha)")


class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")


class QuizResponseWithoutExplanation(BaseModel):
    quizzes: List[QuizItemWithoutExplanation] = Field(description="Test savollari ro'yxati")


class ProgressUpdateRequest(BaseModel):
    quiz_id: str = Field(min_length=1, max_length=128)
    user_id: int = Field(gt=0)
    correct_count: int = Field(ge=0, le=100000)
    percent: int = Field(ge=0, le=100)


class FlashcardCreateRequest(BaseModel):
    user_id: int = Field(gt=0)
    front: str = Field(min_length=1, max_length=5000)
    back: str = Field(min_length=1, max_length=5000)


class PaymentIntentRequest(BaseModel):
    action: str = Field(min_length=1, max_length=32)
    user_id: int = Field(gt=0)
    tariff_name: Optional[str] = Field(default=None, max_length=100)
    tariff_price: Optional[str] = Field(default=None, max_length=32)
    tariff_key: Optional[str] = Field(default=None, max_length=32)



def _send_limit_message(user_id: int, message_key: str, **kwargs):
    """Send a localized Telegram limit notification without affecting app requests."""
    try:
        lang = get_user_lang(user_id)
        text = MESSAGES.get(lang, MESSAGES["uz"])[message_key].format(**kwargs)
        bot.send_message(user_id, text, parse_mode="Markdown")
        return True
    except Exception as e:
        logging.error(f"Limit Telegram xabari yuborilmadi ({user_id}, {message_key}): {e}")
        return False


def notify_free_limit_reached(user_id: int, kind: str):
    """Notify once per 30-day free-limit cycle when a free feature is exhausted."""
    columns = {
        "quiz": ("free_used", "last_quiz_free_notice_cycle", FREE_QUIZ_LIMIT, "free_quiz_limit_notice"),
        "public": ("public_free_used", "last_public_free_notice_cycle", FREE_PUBLIC_LIMIT, "free_public_limit_notice"),
        "flashcard": ("flashcard_free_used", "last_flashcard_free_notice_cycle", FREE_FLASHCARD_LIMIT, "free_flashcard_limit_notice"),
    }
    if kind not in columns:
        return
    used_col, notice_col, limit, message_key = columns[kind]
    try:
        conn = _connect_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            f"SELECT {used_col}, created_at, {notice_col}, status, premium_until FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if not row or is_active_paid_status(row["status"] or "", row["premium_until"] or 0):
            conn.close()
            return
        used = row[used_col] or 0
        cycle = row["created_at"] or 0
        already = row[notice_col] or 0
        if limit == -1 or used < limit or not cycle or already == cycle:
            conn.close()
            return
        cur.execute(f"UPDATE users SET {notice_col} = ? WHERE user_id = ? AND {notice_col} <> ?", (cycle, user_id, cycle))
        changed = cur.rowcount == 1
        conn.commit()
        conn.close()
        if changed:
            _send_limit_message(user_id, message_key, limit=format_free_limit(limit, get_user_lang(user_id)), tariff_lines=tariff_lines(get_user_lang(user_id)))
    except Exception as e:
        logging.error(f"Bepul limit notification xatosi ({user_id}, {kind}): {e}")


def process_expired_free_limits():
    """Reset expired 30-day free cycles; send Telegram notices after DB commit."""
    now = int(time.time())
    thirty_days = 30 * 24 * 3600
    notifications = []
    conn = None
    try:
        conn = _connect_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, created_at, free_used, public_free_used, flashcard_free_used, "
            "status, premium_until, last_free_reset_notice_cycle "
            "FROM users WHERE created_at > 0 AND created_at <= ?",
            (now - thirty_days,),
        )
        rows = cur.fetchall()
        for row in rows:
            user_id = row["user_id"]
            old_cycle = row["created_at"] or 0
            is_paid = is_active_paid_status(row["status"] or "", row["premium_until"] or 0)
            # Paid tarif davrida mavjud tizim qoidasi saqlanadi: bepul sikl reset qilinmaydi.
            if is_paid:
                continue

            already_notified = row["last_free_reset_notice_cycle"] or 0
            had_exhausted_limit = (
                free_limit_reached(FREE_QUIZ_LIMIT, row["free_used"])
                or free_limit_reached(FREE_PUBLIC_LIMIT, row["public_free_used"])
                or free_limit_reached(FREE_FLASHCARD_LIMIT, row["flashcard_free_used"])
            )
            cur.execute(
                "UPDATE users SET free_used=0, public_free_used=0, flashcard_free_used=0, "
                "created_at=?, last_free_reset_notice_cycle=? "
                "WHERE user_id=? AND created_at=?",
                (now, old_cycle if had_exhausted_limit else already_notified, user_id, old_cycle),
            )
            if cur.rowcount == 1 and had_exhausted_limit and already_notified != old_cycle:
                notifications.append(user_id)

        conn.commit()
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logging.error("Bepul limit reset worker xatosi: %s", e)
        return
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # Telegram tarmoq chaqiruvlari SQLite ulanishi yopilgandan keyin bajariladi.
    for user_id in notifications:
        try:
            lang = get_user_lang(user_id)
            text = MESSAGES.get(lang, MESSAGES["uz"])["free_limits_restored_notice"].format(
                quiz_limit=format_free_limit(FREE_QUIZ_LIMIT, lang),
                public_limit=format_free_limit(FREE_PUBLIC_LIMIT, lang),
                flashcard_limit=format_free_limit(FREE_FLASHCARD_LIMIT, lang),
            )
            bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception as e:
            logging.error("Bepul limit qaytgani haqida xabar yuborilmadi (%s): %s", user_id, e)

def process_expired_paid_limits():
    """Expire PRO plans atomically; send Telegram notices after DB commit."""
    now = int(time.time())
    notifications = []
    conn = None
    try:
        conn = _connect_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, status, plan_key, premium_until, paid_limit_notice_until "
            "FROM users WHERE premium_until > 0 AND premium_until <= ? AND status LIKE '%PRO%'",
            (now,),
        )
        rows = cur.fetchall()
        for row in rows:
            user_id = row["user_id"]
            expiry = row["premium_until"] or 0
            if (row["paid_limit_notice_until"] or 0) == expiry:
                continue
            plan_key = row["plan_key"] or get_plan_key(row["status"] or "")
            cur.execute(
                "UPDATE users SET status='Oddiy foydalanuvchi', plan_key='', premium_until=0, paid_limit_notice_until=? "
                "WHERE user_id=? AND premium_until=?",
                (expiry, user_id, expiry),
            )
            if cur.rowcount == 1:
                notifications.append((user_id, plan_key))

        conn.commit()
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logging.error("Premium limit worker xatosi: %s", e)
        return
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # Tilni aniqlash va Telegram yuborish DB tranzaksiyasidan tashqarida.
    for user_id, plan_key in notifications:
        try:
            lang = get_user_lang(user_id)
            tariff_name = localized_tariff_name(plan_key, lang) if plan_key else "Premium"
            text = MESSAGES.get(lang, MESSAGES["uz"])["paid_limit_notice"].format(
                tariff_name=tariff_name,
                tariff_lines=tariff_lines(lang),
            )
            bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception as e:
            logging.error("Premium tugash xabari yuborilmadi (%s): %s", user_id, e)

def limit_notification_worker():
    while not _APP_STOP_EVENT.is_set():
        try:
            process_expired_paid_limits()
            process_expired_free_limits()
        except Exception as e:
            logging.error("Limit notification worker xatosi: %s", e)
        # Oddiy sleep o'rniga Event.wait: shutdown signali kelganda tez uyg'onadi.
        _APP_STOP_EVENT.wait(30)

def add_user_to_db(user_id: int):
    try:
        conn = _connect_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, created_at, language, status, plan_key, free_used, public_free_used, flashcard_free_used, premium_until, last_active) "
            "VALUES (?, ?, 'uz', 'Oddiy foydalanuvchi', '', 0, 0, 0, 0, ?)",
            (user_id, int(time.time()), int(time.time())),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Foydalanuvchi qo'shishda xato: {e}")


def get_users_count():
    try:
        conn = _connect_db()
        cursor = conn.cursor()
            # Faqat oxirgi 2 daqiqada Mini App ochiq/ko'rinib turgan foydalanuvchilar faol hisoblanadi.
        active_since = int(time.time()) - 2 * 60
        cursor.execute(
            "SELECT COUNT(DISTINCT user_id) FROM users WHERE last_active >= ?",
            (active_since,),
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logging.error(f"Faol foydalanuvchilar sonini olishda xato: {e}")
        return 0


def update_user_activity(user_id: int):
    try:
        conn = _connect_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET last_active = ? WHERE user_id = ?",
            (int(time.time()), user_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Faol foydalanuvchi vaqtini yangilashda xato: {e}")


def trigger_payment_flow(user_id, tariff_name=None, tariff_price=None, tariff_key=None):
    try:
        user_lang = get_user_lang(user_id)
        if tariff_key not in TARIFFS:
            # Eski klientlardan kelgan tarif nomlarini ham saqlab qolamiz.
            low = (tariff_name or "").lower()
            if "o'qit" in low or "учител" in low or "teacher" in low:
                tariff_key = "teachers"
            elif "haft" in low or "недель" in low or "weekly" in low or "7" in low:
                tariff_key = "weekly"
            elif "oy" in low or "месяч" in low or "monthly" in low or "30" in low:
                tariff_key = "monthly"
            else:
                tariff_key = "daily"
        tariff_name = localized_tariff_name(tariff_key, user_lang)
        tariff_price = TARIFFS[tariff_key]["price"]
        tx_id = f"TX{uuid.uuid4().hex[:6].upper()}"

        conn = _connect_db()
        cursor = conn.cursor()
    
        # Avvalgi kutilayotgan to'lovlarni o'chirish/bekor qilish
        cursor.execute("UPDATE payments SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'", (user_id,))
        cursor.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, 'pending', ?)",
            (tx_id, user_id, tariff_name, tariff_price, int(time.time())),
        )
        conn.commit()
        conn.close()

        user_lang = get_user_lang(user_id)
        msg_text = MESSAGES[user_lang]["payment_prompt"].format(
            tariff_name=tariff_name,
            tariff_price=tariff_price,
            tx_id=tx_id
        )
        if P2P_CARD_NUMBER:
            card_label = {"uz": "💳 To'lov uchun karta", "ru": "💳 Карта для оплаты", "en": "💳 Payment card"}.get(user_lang, "💳 To'lov uchun karta")
            msg_text += f"\n\n{card_label}: `{P2P_CARD_NUMBER}`"

        bot.send_message(user_id, msg_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"To'lov jarayonini ishga tushirishda xato: {e}")


@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_id = message.from_user.id
    add_user_to_db(user_id)
    user_lang = get_user_lang(user_id)

    welcome_text = MESSAGES[user_lang]["welcome"].format(name=message.from_user.first_name, quiz_limit=format_free_limit(FREE_QUIZ_LIMIT, user_lang), public_limit=format_free_limit(FREE_PUBLIC_LIMIT, user_lang), flashcard_limit=format_free_limit(FREE_FLASHCARD_LIMIT, user_lang))

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    # Admin uchun yordam tugmasi faqat uning shaxsiy chatida ko'rsatiladi.
    # Oddiy foydalanuvchilarning menyusi avvalgidek faqat /start bo'lib qoladi.
    if _admin_only(message):
        btn_help_admin = telebot.types.KeyboardButton(text="/help_admin")
        markup.row(btn_help_admin)
    btn_start = telebot.types.KeyboardButton(text="/start")
    markup.row(btn_start)
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)


@bot.message_handler(content_types=["web_app_data"])
def handle_webapp_data(message):
    try:
        logging.info(f"WebApp dan kelgan xom ma'lumot: {message.web_app_data.data}")
        data = json.loads(message.web_app_data.data)

        if data.get("action") == "contact_admin":
            user_id = int(data.get("user_id") or message.from_user.id)
            user_lang = get_user_lang(user_id)
            if not ADMIN_ID:
                bot.send_message(user_id, MESSAGES[user_lang]["support_config_error"])
                return
            support_waiting_users.add(user_id)
            bot.send_message(user_id, MESSAGES[user_lang]["support_prompt"])
            return

        if data.get("action") == "payment_intent":
            user_id = data.get("user_id")
            tariff_key = data.get("tariff_key")
            tariff_name = data.get("tariff_name")
            tariff_price = data.get("tariff_price")
            trigger_payment_flow(user_id, tariff_name, tariff_price, tariff_key)
    except Exception as e:
        logging.error(f"WebApp ma'lumotlarini o'qishda jiddiy xato: {e}")


# --- ADMIN SUPPORT: foydalanuvchi suhbatini davom ettirish tugmasi ---
def support_continue_markup(lang: str):
    lang = lang if lang in MESSAGES else "uz"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        MESSAGES[lang]["support_continue_btn"],
        callback_data="support_continue"
    ))
    return markup

# --- ADMIN SUPPORT: murojaat va javob ---
@bot.message_handler(content_types=["text"], func=lambda message: (message.from_user.id in support_waiting_users or message.from_user.id in support_reply_targets) and message.from_user.id not in broadcast_waiting_content)
def handle_support_text(message):
    user_id = message.from_user.id
    if is_admin_id(user_id) and user_id in support_reply_targets:
        target_user_id = support_reply_targets.pop(user_id)
        try:
            target_lang = get_user_lang(target_user_id)
            target_messages = MESSAGES.get(target_lang, MESSAGES["uz"])
            bot.send_message(
                target_user_id,
                f"💬 {target_messages.get('support_admin_reply_title', 'Admin javobi')}:\n\n{message.text}",
                reply_markup=support_continue_markup(target_lang)
            )
            # Admin tomondagi texnik tasdiq doim O'zbek tilida qoladi.
            bot.send_message(user_id, MESSAGES["uz"]["support_reply_sent"])
        except Exception as e:
            logging.error(f"Admin javobini yuborishda xato: {e}")
            bot.send_message(user_id, "❌ Javobni yuborishda xatolik yuz berdi.")
        return
    if user_id in support_waiting_users:
        support_waiting_users.discard(user_id)
        user_lang = get_user_lang(user_id)
        username = f"@{message.from_user.username}" if message.from_user.username else "Mavjud emas"
        first_name = message.from_user.first_name or "Mavjud emas"
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(MESSAGES["uz"]["support_reply_btn"], callback_data=f"support_reply:{user_id}"))
        text = (f"{MESSAGES['uz']['support_admin_title']}\n\n"
                f"👤 {first_name}\n🔗 Username: {username}\n🆔 Telegram ID: {user_id}\n🌐 Til: {user_lang.upper()}\n\n💬 {message.text}")
        try:
            if not ADMIN_ID:
                bot.send_message(user_id, MESSAGES[user_lang]["support_config_error"])
                return
            bot.send_message(ADMIN_ID, text, reply_markup=markup)
            bot.send_message(
                user_id,
                MESSAGES[user_lang]["support_sent"],
                reply_markup=support_continue_markup(user_lang)
            )
        except Exception as e:
            logging.error(f"Admin murojaatini yuborishda xato: {e}")
            bot.send_message(user_id, "❌ Murojaatni yuborishda xatolik yuz berdi.")
        return

@bot.callback_query_handler(func=lambda call: call.data == "support_continue")
def handle_support_continue_callback(call):
    """Allow the user to continue the same admin-support conversation from Telegram."""
    if is_admin_id(call.from_user.id):
        bot.answer_callback_query(call.id, "Bu tugma foydalanuvchi uchun.", show_alert=True)
        return
    try:
        user_id = call.from_user.id
        add_user_to_db(user_id)
        user_lang = get_user_lang(user_id)
        if not ADMIN_ID:
            bot.answer_callback_query(call.id)
            bot.send_message(user_id, MESSAGES[user_lang]["support_config_error"])
            return
        support_waiting_users.add(user_id)
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, MESSAGES[user_lang]["support_prompt"])
        logging.info(f"Admin support davom ettirildi: user_id={user_id}, admin_id={ADMIN_ID}")
    except Exception as e:
        logging.error(f"Admin support davom ettirish callback xatosi: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("support_reply:"))
def handle_support_reply_callback(call):
    if not is_admin_id(call.from_user.id):
        bot.answer_callback_query(call.id, "Siz administrator emassiz!", show_alert=True)
        return
    try:
        target_user_id = int(call.data.split(":", 1)[1])
        support_reply_targets[call.from_user.id] = target_user_id
        bot.answer_callback_query(call.id)
        bot.send_message(call.from_user.id, MESSAGES["uz"]["support_reply_prompt"])
    except Exception as e:
        logging.error(f"Admin reply callback xatosi: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.", show_alert=True)

# --- ADMIN STATISTICS ---
def _admin_only(message):
    return is_admin_id(message.from_user.id)


@bot.message_handler(commands=["test_error_alert"])
def admin_test_error_alert_command(message):
    """Administrator-only, controlled test for the Telegram error alert handler."""
    if not _admin_only(message):
        return

    # Send confirmation first, then emit one controlled ERROR log.
    # The installed AdminTelegramErrorHandler should deliver this log to ADMIN_ID.
    try:
        bot.send_message(
            message.chat.id,
            "🧪 Sinov boshlandi. Agar xatolik xabari tizimi ishlasa, administrator chatiga alohida ogohlantirish keladi."
        )
        logging.error(
            "TEST ALERT: administrator tomonidan avtomatik xatolik xabari tekshirilmoqda."
        )
    except Exception as e:
        logging.exception("Test error alert buyrug'ida xatolik yuz berdi: %s", e)
        try:
            bot.send_message(message.chat.id, "❌ Sinov buyrug'ini bajarishda xatolik yuz berdi.")
        except Exception:
            pass



@bot.message_handler(commands=["monitor"])
def admin_monitor_command(message):
    """Show a lightweight, administrator-only snapshot of server and AI capacity."""
    if not _admin_only(message):
        return

    # Semaphore availability gives a best-effort snapshot; it is not a historical metric.
    try:
        ai_slots_free = max(0, int(getattr(gemini_semaphore, "_value", 0)))
        queue_slots_free = max(0, int(getattr(ai_queue_slots, "_value", 0)))
        ai_slots_busy = max(0, AI_MAX_CONCURRENT - ai_slots_free)
        queue_slots_used = max(0, AI_MAX_QUEUE - queue_slots_free)

        db_status = "✅ Ulandi"
        conn = None
        try:
            conn = _connect_db(timeout=5)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("SELECT 1").fetchone()
        except Exception as db_error:
            db_status = "❌ Xato"
            logging.error("Monitor: SQLite health check xatosi: %s", db_error)
        finally:
            if conn is not None:
                conn.close()

        # Linux / Railway konteynerida joriy RSS xotirasini /proc orqali olamiz.
        # psutil qo'shimcha dependency sifatida talab qilinmaydi.
        process_rss_mb = None
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as proc_status:
                for line in proc_status:
                    if line.startswith("VmRSS:"):
                        process_rss_mb = round(int(line.split()[1]) / 1024, 1)
                        break
        except (OSError, ValueError, IndexError):
            pass

        load_average = None
        try:
            load_average = " / ".join(f"{value:.2f}" for value in os.getloadavg())
        except (AttributeError, OSError):
            pass

        p2p_status = {}
        try:
            p2p_status = p2p_payment_ai.get_status()
        except Exception as p2p_error:
            logging.warning("Monitor: P2P holatini olish imkoni bo'lmadi | sabab=%s", type(p2p_error).__name__)

        p2p_budget = p2p_status.get("budget", {}) if isinstance(p2p_status, dict) else {}
        now_local = datetime.now(timezone(timedelta(hours=5)))
        process_metrics = []
        if process_rss_mb is not None:
            process_metrics.append(f"RAM (jarayon): <b>{process_rss_mb:.1f} MB</b>")
        if load_average is not None:
            process_metrics.append(f"Load average (1/5/15 daq.): <b>{load_average}</b>")
        process_metrics_text = "\n".join(process_metrics) if process_metrics else "Jarayon metrikalari mavjud emas"

        report = (
            "🖥 <b>Quiz AI — Server va AI monitoring</b>\n"
            f"🕒 {now_local.strftime('%d-%m-%Y %H:%M:%S')} (O‘zbekiston)\n\n"
            f"🗄 SQLite bazasi: <b>{db_status}</b>\n"
            f"📈 {process_metrics_text}\n"
            f"🤖 Asosiy model: <code>{GEMINI_MODEL}</code>\n"
            f"🔁 Zaxira model: <code>{GEMINI_FALLBACK_MODEL}</code>\n\n"
            "⚙️ <b>AI parallel so‘rovlar</b>\n"
            f"Band: <b>{ai_slots_busy}/{AI_MAX_CONCURRENT}</b>\n"
            f"Bo‘sh slotlar: <b>{ai_slots_free}</b>\n\n"
            "⏳ <b>AI navbat sig‘imi</b>\n"
            f"Band slotlar: <b>{queue_slots_used}/{AI_MAX_QUEUE}</b>\n"
            f"Bo‘sh slotlar: <b>{queue_slots_free}</b>\n\n"
            "💳 <b>P2P OCR navbati</b>\n"
            f"Navbat: <b>{p2p_status.get('queue_size', 'N/A')}/{p2p_status.get('queue_limit', 'N/A')}</b>\n"
            f"Bugungi OCR: <b>{p2p_budget.get('daily_requests', 'N/A')}/{p2p_budget.get('daily_limit', 'N/A')}</b>\n"
            f"Oylik OCR: <b>{p2p_budget.get('monthly_requests', 'N/A')}/{p2p_budget.get('monthly_request_limit', 'N/A')}</b>\n"
            f"Oylik sarf: <b>${float(p2p_budget.get('monthly_spend_usd', 0) or 0):.4f}</b>\n\n"
            f"🔑 Generation API kalitlari: <b>{len(GOOGLE_API_KEYS)}</b>\n"
            f"⏱ Bitta so‘rov limiti: <b>{AI_REQUEST_TIMEOUT} soniya</b>\n"
            f"⏱ Umumiy vaqt limiti: <b>{AI_TOTAL_TIMEOUT} soniya</b>\n\n"
            "ℹ️ Bu bir martalik joriy holat. Load average CPU foizi emas; tarixiy grafiklar Railway Metrics'da ko‘riladi."
        )
        bot.send_message(message.chat.id, report, parse_mode="HTML")
    except Exception as e:
        logging.exception("Admin monitoring hisobotini tayyorlashda xatolik: %s", e)
        try:
            bot.send_message(message.chat.id, "❌ Monitoring ma’lumotlarini olishda xatolik yuz berdi. Railway loglarini tekshiring.")
        except Exception:
            pass


@bot.message_handler(commands=["stats"])
def admin_stats_command(message):
    """Show private Quiz AI usage statistics to the configured administrator only."""
    if not _admin_only(message):
        return

    conn = None
    try:
        # Uzbekistan local day (UTC+5), independent of the server timezone.
        uz_tz = timezone(timedelta(hours=5))
        now_local = datetime.now(uz_tz)
        today_start = int(now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        now_ts = int(time.time())
        active_since = now_ts - 2 * 60

        conn = _connect_db(timeout=30)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")

        total_users = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        new_users_today = cur.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= ?", (today_start,)
        ).fetchone()[0]
        visitors_today = cur.execute(
            "SELECT COUNT(*) FROM users WHERE last_active >= ?", (today_start,)
        ).fetchone()[0]
        active_now = cur.execute(
            "SELECT COUNT(*) FROM users WHERE last_active >= ?", (active_since,)
        ).fetchone()[0]

        total_quizzes = cur.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0]
        quizzes_today = cur.execute(
            "SELECT COUNT(*) FROM quizzes WHERE created_at >= ?", (today_start,)
        ).fetchone()[0]
        pending_payments = cur.execute(
            "SELECT COUNT(*) FROM payments WHERE status = 'pending'"
        ).fetchone()[0]
        approved_payments_today = cur.execute(
            "SELECT COUNT(*) FROM payments WHERE status = 'approved' AND created_at >= ?",
            (today_start,),
        ).fetchone()[0]

        report = (
            "📊 <b>Quiz AI — Statistika</b>\n"
            f"📅 Sana: {now_local.strftime('%d-%m-%Y')} (O‘zbekiston)\n\n"
            f"👥 Jami ro‘yxatdan o‘tganlar: <b>{total_users:,}</b>\n"
            f"🆕 Bugun ro‘yxatdan o‘tganlar: <b>{new_users_today:,}</b>\n"
            f"📱 Bugun faol bo‘lgan foydalanuvchilar: <b>{visitors_today:,}</b>\n"
            f"🟢 Hozir faol (oxirgi 2 daqiqa): <b>{active_now:,}</b>\n\n"
            f"📝 Jami yaratilgan testlar: <b>{total_quizzes:,}</b>\n"
            f"✨ Bugun yaratilgan testlar: <b>{quizzes_today:,}</b>\n\n"
            f"⏳ Kutilayotgan to‘lovlar: <b>{pending_payments:,}</b>\n"
            f"✅ Bugun yaratilgan va tasdiqlangan to‘lov so‘rovlari: <b>{approved_payments_today:,}</b>"
        )
        bot.send_message(message.chat.id, report, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Admin statistikasini olishda xato: {e}")
        bot.send_message(message.chat.id, "❌ Statistikani olishda xatolik yuz berdi. Server loglarini tekshiring.")
    finally:
        if conn is not None:
            conn.close()




# --- ADMIN FOYDALANUVCHILAR HISOBOTI ---
@bot.message_handler(commands=["users"])
def admin_users_report_command(message):
    """Administrator uchun foydalanuvchilar, obunalar va uzoq faollashmaganlar hisoboti."""
    if not _admin_only(message):
        return

    conn = None
    try:
        now = int(time.time())
        uz_tz = timezone(timedelta(hours=5))
        now_local = datetime.now(uz_tz)
        day_7 = now - 7 * 24 * 3600
        day_30 = now - 30 * 24 * 3600

        conn = _connect_db(timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        cur = conn.cursor()

        total_users = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        created_7_days = cur.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= ?", (day_7,)
        ).fetchone()[0]
        created_30_days = cur.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= ?", (day_30,)
        ).fetchone()[0]
        active_7_days = cur.execute(
            "SELECT COUNT(*) FROM users WHERE last_active >= ?", (day_7,)
        ).fetchone()[0]
        inactive_7_days = cur.execute(
            "SELECT COUNT(*) FROM users WHERE COALESCE(last_active, 0) > 0 AND last_active < ?", (day_7,)
        ).fetchone()[0]
        never_active = cur.execute(
            "SELECT COUNT(*) FROM users WHERE COALESCE(last_active, 0) = 0"
        ).fetchone()[0]
        active_premium = cur.execute(
            "SELECT COUNT(*) FROM users WHERE status LIKE '%PRO%' AND premium_until > ?", (now,)
        ).fetchone()[0]
        expired_premium = cur.execute(
            "SELECT COUNT(*) FROM users WHERE status LIKE '%PRO%' AND premium_until > 0 AND premium_until <= ?", (now,)
        ).fetchone()[0]
        ordinary_users = cur.execute(
            "SELECT COUNT(*) FROM users WHERE NOT (status LIKE '%PRO%' AND premium_until > ?)", (now,)
        ).fetchone()[0]
        paid_premium = cur.execute(
            "SELECT COUNT(*) FROM users WHERE status LIKE '%PRO%' AND premium_until > ? AND COALESCE(premium_source, 'paid') != 'admin_bonus'", (now,)
        ).fetchone()[0]
        bonus_premium = cur.execute(
            "SELECT COUNT(*) FROM users WHERE status LIKE '%PRO%' AND premium_until > ? AND premium_source = 'admin_bonus'", (now,)
        ).fetchone()[0]

        inactive_rows = cur.execute(
            "SELECT user_id, created_at, last_active, status, plan_key, premium_until "
            "FROM users WHERE COALESCE(last_active, 0) > 0 AND last_active < ? "
            "ORDER BY last_active ASC, created_at DESC LIMIT 8", (day_7,)
        ).fetchall()

        def date_text(timestamp):
            timestamp = int(timestamp or 0)
            return time.strftime('%d-%m-%Y %H:%M', time.gmtime(timestamp + 5 * 3600)) if timestamp > 0 else "ma’lumot yo‘q"

        lines = [
            "👥 <b>Quiz AI — Foydalanuvchilar hisoboti</b>",
            f"🕒 {now_local.strftime('%d-%m-%Y %H:%M')} (O‘zbekiston)",
            "",
            "📊 <b>Foydalanuvchilar</b>",
            f"• Jami ro‘yxatdan o‘tganlar: <b>{total_users:,}</b>",
            f"• Oxirgi 7 kunda qo‘shilganlar: <b>{created_7_days:,}</b>",
            f"• Oxirgi 30 kunda qo‘shilganlar: <b>{created_30_days:,}</b>",
            f"• Oxirgi 7 kunda faol bo‘lganlar: <b>{active_7_days:,}</b>",
            f"• 7 kundan beri faol bo‘lmaganlar: <b>{inactive_7_days:,}</b>",
            f"• Faollik qaydi yo‘q: <b>{never_active:,}</b>",
            "",
            "👑 <b>Obuna holati</b>",
            f"• Faol Premium obunachilar: <b>{active_premium:,}</b>",
            f"• Pullik Premium obunachilar: <b>{paid_premium:,}</b>",
            f"• Admin bonusi orqali Premium: <b>{bonus_premium:,}</b>",
            f"• Muddati tugagan PRO statuslar: <b>{expired_premium:,}</b>",
            f"• Hozir faol Premium bo‘lmaganlar: <b>{ordinary_users:,}</b>",
            "",
            f"🕰 <b>7 kundan beri faol bo‘lmaganlar (ko‘pi bilan 8 ta):</b>",
        ]
        if inactive_rows:
            for row in inactive_rows:
                last_seen = date_text(row["last_active"])
                created = date_text(row["created_at"])
                status = row["status"] or "Oddiy foydalanuvchi"
                lines.append(
                    f"• ID <code>{row['user_id']}</code> — {status}\n"
                    f"  Oxirgi faollik: {last_seen}\n"
                    f"  Ro‘yxatdan o‘tgan: {created}"
                )
            if inactive_7_days > len(inactive_rows):
                lines.append(f"… va yana {inactive_7_days - len(inactive_rows)} ta foydalanuvchi.")
        else:
            lines.append("• 7 kundan beri faol bo‘lmagan foydalanuvchi yo‘q.")

        report = "\n".join(lines)
        max_length = 3800
        while len(report) > max_length:
            split_at = report.rfind("\n", 0, max_length)
            if split_at <= 0:
                split_at = max_length
            bot.send_message(message.chat.id, report[:split_at], parse_mode="HTML", disable_web_page_preview=True)
            report = report[split_at:].lstrip("\n")
        if report:
            bot.send_message(message.chat.id, report, parse_mode="HTML", disable_web_page_preview=True)

        logging.info(
            "Foydalanuvchilar hisoboti yuborildi | jami=%s | premium_faol=%s | oxirgi_7_kunda_faol=%s | 7_kun_faol_emas=%s",
            total_users, active_premium, active_7_days, inactive_7_days
        )
    except Exception as e:
        logging.exception("Foydalanuvchilar hisobotini tayyorlashda xatolik: %s", e)
        try:
            bot.send_message(message.chat.id, "❌ Foydalanuvchilar hisobotini tayyorlab bo‘lmadi. Railway loglarini tekshiring.")
        except Exception:
            pass
    finally:
        if conn is not None:
            conn.close()



# --- ADMIN PAYMENTS & SUBSCRIPTION REPORT ---
def _payment_price_to_sum(value):
    """Narx matnidan so'm miqdorini xavfsiz ajratib oladi."""
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else 0


@bot.message_handler(commands=["payments"])
def admin_payments_report_command(message):
    """Administrator uchun tushum, tariflar, tugayotgan obunalar va kutilayotgan to'lovlar."""
    if not _admin_only(message):
        return

    conn = None
    try:
        now = int(time.time())
        uz_tz = timezone(timedelta(hours=5))
        now_local = datetime.now(uz_tz)
        today_start = int(now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        week_end = now + 7 * 24 * 3600

        conn = _connect_db(timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        cur = conn.cursor()

        # Faqat tasdiqlangan to'lovlar tushum hisoblanadi.
        approved_rows = cur.execute(
            "SELECT tariff_name, tariff_price, created_at FROM payments WHERE status='approved'"
        ).fetchall()
        pending_rows = cur.execute(
            "SELECT tx_id, user_id, tariff_name, tariff_price, created_at FROM payments "
            "WHERE status='pending' ORDER BY created_at ASC LIMIT 8"
        ).fetchall()
        pending_total = cur.execute(
            "SELECT COUNT(*) FROM payments WHERE status='pending'"
        ).fetchone()[0]
        approved_today_rows = cur.execute(
            "SELECT tariff_price FROM payments WHERE status='approved' AND created_at >= ?",
            (today_start,)
        ).fetchall()

        # Obuna muddati yaqinlashayotgan PRO foydalanuvchilar; bonuslar alohida hisoblanmaydi.
        expiring_rows = cur.execute(
            "SELECT user_id, status, plan_key, premium_until, premium_source FROM users "
            "WHERE premium_until > ? AND premium_until <= ? AND status LIKE '%PRO%' "
            "ORDER BY premium_until ASC LIMIT 8",
            (now, week_end)
        ).fetchall()
        expiring_total = cur.execute(
            "SELECT COUNT(*) FROM users WHERE premium_until > ? AND premium_until <= ? "
            "AND status LIKE '%PRO%'",
            (now, week_end)
        ).fetchone()[0]

        total_revenue = sum(_payment_price_to_sum(row["tariff_price"]) for row in approved_rows)
        today_revenue = sum(_payment_price_to_sum(row["tariff_price"]) for row in approved_today_rows)

        tariff_stats = {key: {"count": 0, "revenue": 0} for key in TARIFFS}
        for row in approved_rows:
            plan_key = get_plan_key(row["tariff_name"] or "")
            if plan_key in tariff_stats:
                tariff_stats[plan_key]["count"] += 1
                tariff_stats[plan_key]["revenue"] += _payment_price_to_sum(row["tariff_price"])

        def money(amount):
            return f"{amount:,}".replace(",", " ") + " so‘m"

        report_lines = [
            "💳 <b>Quiz AI — Daromad va obunalar hisoboti</b>",
            f"🕒 {now_local.strftime('%d-%m-%Y %H:%M')} (O‘zbekiston)",
            "",
            "💰 <b>Tushum</b>",
            f"• Jami tasdiqlangan to‘lovlar: <b>{len(approved_rows):,}</b>",
            f"• Jami tushum: <b>{money(total_revenue)}</b>",
            f"• Bugungi tushum: <b>{money(today_revenue)}</b>",
            "",
            "📊 <b>Tariflar savdosi</b>",
        ]
        for key, tariff in TARIFFS.items():
            stats = tariff_stats[key]
            report_lines.append(
                f"• {tariff['names']['uz']}: <b>{stats['count']}</b> ta — {money(stats['revenue'])}"
            )

        most_sold = max(tariff_stats.items(), key=lambda item: item[1]["count"]) if approved_rows else None
        if most_sold and most_sold[1]["count"] > 0:
            report_lines.extend([
                "",
                f"🏆 Eng ko‘p sotilgan tarif: <b>{TARIFFS[most_sold[0]]['names']['uz']}</b> "
                f"({most_sold[1]['count']} ta)"
            ])
        else:
            report_lines.extend(["", "🏆 Eng ko‘p sotilgan tarif: hozircha tasdiqlangan savdo yo‘q."])

        report_lines.extend([
            "",
            f"⏳ <b>Tasdiqlanmagan to‘lovlar: {pending_total}</b>",
        ])
        if pending_rows:
            for row in pending_rows:
                created = time.strftime("%d-%m-%Y %H:%M", time.gmtime(int(row["created_at"] or 0) + 5 * 3600))
                report_lines.append(
                    f"• <code>{str(row['tx_id'])[:40]}</code> — ID <code>{row['user_id']}</code>\n"
                    f"  {row['tariff_name'] or 'Tarif noma’lum'} | {row['tariff_price'] or 'Narx noma’lum'} | {created}"
                )
            if pending_total > len(pending_rows):
                report_lines.append(f"… va yana {pending_total - len(pending_rows)} ta to‘lov.")
        else:
            report_lines.append("• Kutilayotgan to‘lov yo‘q.")

        report_lines.extend([
            "",
            f"📅 <b>7 kun ichida muddati tugaydigan obunalar: {expiring_total}</b>",
        ])
        if expiring_rows:
            for row in expiring_rows:
                expiry = time.strftime("%d-%m-%Y %H:%M", time.gmtime(int(row["premium_until"]) + 5 * 3600))
                plan_name = localized_tariff_name(row["plan_key"], "uz") if row["plan_key"] in TARIFFS else (row["status"] or "PRO")
                source_note = " (bonus)" if row["premium_source"] == "admin_bonus" else ""
                report_lines.append(
                    f"• ID <code>{row['user_id']}</code> — {plan_name}{source_note}\n"
                    f"  Tugash vaqti: {expiry}"
                )
            if expiring_total > len(expiring_rows):
                report_lines.append(f"… va yana {expiring_total - len(expiring_rows)} ta obuna.")
        else:
            report_lines.append("• Yaqin 7 kunda muddati tugaydigan obuna yo‘q.")

        # Telegram xabar limiti oshmasligi uchun hisobot bo'laklarga ajratiladi.
        full_report = "\n".join(report_lines)
        max_length = 3800
        chunks = []
        while len(full_report) > max_length:
            split_at = full_report.rfind("\n", 0, max_length)
            if split_at <= 0:
                split_at = max_length
            chunks.append(full_report[:split_at])
            full_report = full_report[split_at:].lstrip("\n")
        if full_report:
            chunks.append(full_report)
        for chunk in chunks:
            bot.send_message(message.chat.id, chunk, parse_mode="HTML", disable_web_page_preview=True)

        logging.info(
            "Daromad va obunalar hisoboti yuborildi | tasdiqlangan_to'lov=%s | tushum=%s | kutilayotgan=%s | tugayotgan_obuna=%s",
            len(approved_rows), total_revenue, pending_total, expiring_total
        )
    except Exception as e:
        logging.exception("Daromad va obunalar hisobotini tayyorlashda xatolik: %s", e)
        try:
            bot.send_message(message.chat.id, "❌ Daromad va obunalar hisobotini tayyorlab bo‘lmadi. Railway loglarini tekshiring.")
        except Exception:
            pass
    finally:
        if conn is not None:
            conn.close()



@bot.message_handler(commands=["db_health"])
def admin_database_health_command(message):
    """Administrator uchun SQLite bazasining yengil holat tekshiruvi."""
    if not _admin_only(message):
        return

    conn = None
    try:
        conn = _connect_db(timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        cur = conn.cursor()

        # quick_check yengil tekshiruv; bazani o'zgartirmaydi.
        check_row = cur.execute("PRAGMA quick_check").fetchone()
        integrity_status = "✅ Sog‘lom" if check_row and check_row[0] == "ok" else "⚠️ Tekshirish kerak"

        table_counts = {}
        for table_name, label in (
            ("users", "foydalanuvchilar"),
            ("quizzes", "testlar"),
            ("payments", "to‘lovlar"),
            ("flashcards", "flash-kartochkalar"),
        ):
            try:
                table_counts[label] = int(
                    cur.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                )
            except sqlite3.Error:
                table_counts[label] = None

        db_size = os.path.getsize(DB_PATH) if os.path.isfile(DB_PATH) else 0
        wal_path = DB_PATH + "-wal"
        wal_size = os.path.getsize(wal_path) if os.path.isfile(wal_path) else 0

        def _format_bytes(size):
            if size < 1024 * 1024:
                return f"{size / 1024:.1f} KB"
            return f"{size / (1024 * 1024):.2f} MB"

        def _count_text(value):
            return f"{value:,}" if value is not None else "Jadval topilmadi"

        now_local = datetime.now(timezone(timedelta(hours=5)))
        report = (
            "🗄 <b>Quiz AI — Ma’lumotlar bazasi holati</b>\n"
            f"🕒 {now_local.strftime('%d-%m-%Y %H:%M:%S')} (O‘zbekiston)\n\n"
            f"🔎 Bazaning yaxlitligi: <b>{integrity_status}</b>\n"
            f"📦 SQLite fayli: <b>{_format_bytes(db_size)}</b>\n"
            f"🧾 WAL jurnali: <b>{_format_bytes(wal_size)}</b>\n\n"
            "📊 <b>Jadval yozuvlari</b>\n"
            f"👥 Foydalanuvchilar: <b>{_count_text(table_counts['foydalanuvchilar'])}</b>\n"
            f"📝 Testlar: <b>{_count_text(table_counts['testlar'])}</b>\n"
            f"💳 To‘lovlar: <b>{_count_text(table_counts['to‘lovlar'])}</b>\n"
            f"🗂 Flash-kartochkalar: <b>{_count_text(table_counts['flash-kartochkalar'])}</b>\n\n"
            "ℹ️ Bu tekshiruv bazani o‘zgartirmaydi va zaxira nusxa yaratilganini anglatmaydi."
        )
        bot.send_message(message.chat.id, report, parse_mode="HTML")
        logging.info("Ma’lumotlar bazasi holati tekshirildi | yaxlitlik=%s | SQLite_hajmi=%s_bayt | WAL_hajmi=%s_bayt",
                     integrity_status, db_size, wal_size)
    except Exception as e:
        logging.exception("Ma’lumotlar bazasi holatini tekshirishda xatolik yuz berdi: %s", e)
        try:
            bot.send_message(
                message.chat.id,
                "❌ Ma’lumotlar bazasi holatini tekshirib bo‘lmadi. Railway loglarini tekshiring."
            )
        except Exception:
            logging.exception("Baza tekshiruvi xatosi haqida Telegram xabarini yuborib bo‘lmadi.")
    finally:
        if conn is not None:
            conn.close()


# --- ADMIN SQLITE BACKUP: BROWSER DOWNLOAD LINK ---
# Har bir havola tasodifiy, 10 daqiqalik va bir martalik bo'ladi.
SQLITE_BACKUP_TOKEN_TTL = 10 * 60
_sqlite_backup_tokens = {}
_sqlite_backup_tokens_lock = threading.Lock()
BACKUP_PUBLIC_BASE_URL = os.getenv("BACKUP_PUBLIC_BASE_URL", "").strip().rstrip("/")
if not BACKUP_PUBLIC_BASE_URL:
    _railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if _railway_domain:
        BACKUP_PUBLIC_BASE_URL = (
            _railway_domain if _railway_domain.startswith(("http://", "https://"))
            else "https://" + _railway_domain
        )


def _issue_sqlite_backup_token():
    now = time.time()
    token = secrets.token_urlsafe(32)
    with _sqlite_backup_tokens_lock:
        expired = [key for key, expires_at in _sqlite_backup_tokens.items() if expires_at <= now]
        for key in expired:
            _sqlite_backup_tokens.pop(key, None)
        _sqlite_backup_tokens[token] = now + SQLITE_BACKUP_TOKEN_TTL
    return token


def _consume_sqlite_backup_token(token: str) -> bool:
    now = time.time()
    with _sqlite_backup_tokens_lock:
        expires_at = _sqlite_backup_tokens.pop(token, None)
    return bool(expires_at and expires_at > now)


@bot.message_handler(commands=["db_backup"])
def admin_database_backup_command(message):
    """Administrator-only: create a short-lived browser download link."""
    if not _admin_only(message):
        return

    if not os.path.isfile(DB_PATH):
        bot.send_message(message.chat.id, "❌ SQLite bazasi fayli topilmadi.")
        return

    if not BACKUP_PUBLIC_BASE_URL:
        bot.send_message(
            message.chat.id,
            "⚠️ Yuklab olish havolasini yaratib bo‘lmadi. Railway Variables bo‘limida "
            "RAILWAY_PUBLIC_DOMAIN mavjudligini tekshiring yoki BACKUP_PUBLIC_BASE_URL "
            "o‘zgaruvchisiga ilovaning to‘liq HTTPS manzilini kiriting."
        )
        logging.error("DB backup link yaratilmadi: public base URL sozlanmagan.")
        return

    try:
        token = _issue_sqlite_backup_token()
        download_url = f"{BACKUP_PUBLIC_BASE_URL}/admin/sqlite-backup?token={token}"
        bot.send_message(
            message.chat.id,
            "🗄 <b>Quiz AI — Baza zaxira nusxasi</b>\n\n"
            "Havola 10 daqiqa davomida amal qiladi va faqat bir marta ishlatiladi.\n"
            "Havolani Chrome yoki Yandex Browser orqali oching — zaxira fayli yuklanadi.\n\n"
            f'⬇️ <a href="{download_url}">Zaxira nusxasini yuklab olish</a>\n\n'
            "🔐 Havolani boshqa odamlarga yubormang.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logging.info("Administrator uchun bir martalik SQLite backup havolasi yaratildi.")
    except Exception:
        logging.exception("SQLite backup havolasini yaratishda xatolik yuz berdi.")
        bot.send_message(message.chat.id, "❌ Zaxira havolasini yaratishda xatolik yuz berdi.")


# --- ADMIN OMMAVIY XABAR YUBORISH ---
@bot.message_handler(commands=["broadcast"])
def admin_broadcast_command(message):
    """Administrator matnini oldindan ko'rsatib, tasdiqdan keyin foydalanuvchilarga yuboradi."""
    if not _admin_only(message):
        return
    if broadcast_active:
        bot.send_message(message.chat.id, "⏳ Hozir boshqa ommaviy xabar yuborilmoqda. Tugashini kuting.")
        return
    broadcast_waiting_content.add(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "📣 <b>Ommaviy xabar yuborish</b>\n\n"
        "Barcha ro‘yxatdan o‘tgan foydalanuvchilarga yuboriladigan matnni yuboring.\n"
        "Yuborishdan oldin xabarni ko‘rib chiqib, alohida tasdiqlaysiz.\n\n"
        "Bekor qilish uchun /broadcast_cancel buyrug‘ini yuboring.",
        parse_mode="HTML",
    )
    logging.info("Administrator ommaviy xabar matnini kiritish jarayonini boshladi.")


@bot.message_handler(commands=["broadcast_cancel"])
def admin_broadcast_cancel_command(message):
    if not _admin_only(message):
        return
    broadcast_waiting_content.discard(message.from_user.id)
    broadcast_pending_messages.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "❎ Ommaviy xabar tayyorlash bekor qilindi.")
    logging.info("Administrator ommaviy xabar tayyorlashni bekor qildi.")


@bot.message_handler(content_types=["text"], func=lambda message: is_admin_id(message.from_user.id) and message.from_user.id in broadcast_waiting_content)
def admin_broadcast_receive_text(message):
    if not _admin_only(message):
        return
    text = (message.text or "").strip()
    if not text:
        bot.send_message(message.chat.id, "⚠️ Xabar bo‘sh. Iltimos, matn yuboring.")
        return
    if len(text) > 4000:
        bot.send_message(message.chat.id, "⚠️ Xabar 4 000 belgidan oshmasligi kerak. Qisqaroq matn yuboring.")
        return

    broadcast_waiting_content.discard(message.from_user.id)
    broadcast_pending_messages[message.from_user.id] = text
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✅ Yuborishni tasdiqlash", callback_data="broadcast_confirm"),
        telebot.types.InlineKeyboardButton("❎ Bekor qilish", callback_data="broadcast_cancel"),
    )
    try:
        bot.send_message(
            message.chat.id,
            "👁 Ommaviy xabar — ko‘rib chiqish\n\n"
            + text
            + "\n\n⚠️ Bu xabar ro‘yxatdan o‘tgan foydalanuvchilarga yuboriladi. Yuborishni boshlash uchun tasdiqlang.",
            parse_mode=None,
            reply_markup=markup,
        )
        logging.info("Ommaviy xabar matni tayyorlandi; administrator tasdig‘i kutilmoqda.")
    except Exception as e:
        broadcast_pending_messages.pop(message.from_user.id, None)
        logging.error("Ommaviy xabarni ko‘rib chiqish xabarini yuborishda xatolik: %s", e)
        bot.send_message(message.chat.id, "❌ Ko‘rib chiqish xabarini yuborishda xatolik yuz berdi.")


@bot.callback_query_handler(func=lambda call: call.data in ("broadcast_confirm", "broadcast_cancel"))
def admin_broadcast_callback(call):
    global broadcast_active
    if not is_admin_id(call.from_user.id):
        bot.answer_callback_query(call.id, "Bu amal faqat administrator uchun.", show_alert=True)
        return

    if call.data == "broadcast_cancel":
        broadcast_pending_messages.pop(call.from_user.id, None)
        bot.answer_callback_query(call.id, "Ommaviy xabar bekor qilindi.")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            bot.send_message(call.message.chat.id, "❎ Ommaviy xabar yuborilmadi.")
        except Exception as e:
            logging.warning("Ommaviy xabarni bekor qilish xabarini yangilashda xatolik: %s", e)
        logging.info("Administrator tasdiqlash bosqichida ommaviy xabarni bekor qildi.")
        return

    text = broadcast_pending_messages.pop(call.from_user.id, None)
    if not text:
        bot.answer_callback_query(call.id, "Tasdiqlash uchun xabar topilmadi.", show_alert=True)
        return
    with broadcast_lock:
        if broadcast_active:
            broadcast_pending_messages[call.from_user.id] = text
            bot.answer_callback_query(call.id, "Boshqa ommaviy xabar yuborilmoqda.", show_alert=True)
            return
        broadcast_active = True

    bot.answer_callback_query(call.id, "Yuborish boshlandi.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception as e:
        logging.warning("Tasdiqlash tugmasini olib tashlashda xatolik: %s", e)

    def _send_broadcast():
        global broadcast_active
        sent_count = 0
        failed_count = 0
        conn = None
        try:
            conn = _connect_db(timeout=30)
            conn.execute("PRAGMA busy_timeout=30000")
            rows = conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
            user_ids = [int(row[0]) for row in rows if row and row[0] is not None]
            total = len(user_ids)
            bot.send_message(
                ADMIN_ID,
                f"📣 Ommaviy xabar yuborish boshlandi.\n👥 Qabul qiluvchilar: {total:,}\n⏱ Tezlik cheklovi: taxminan 10 ta xabar/soniya."
            )
            logging.info("Ommaviy xabar yuborish boshlandi | qabul_qiluvchilar=%s", total)
            for user_id in user_ids:
                try:
                    bot.send_message(user_id, text, disable_web_page_preview=True)
                    sent_count += 1
                except Exception as send_error:
                    failed_count += 1
                    logging.warning("Ommaviy xabar yuborilmadi | user_id=%s | sabab=%s", user_id, str(send_error)[:300])
                time.sleep(0.1)  # Telegram API yukini cheklash: taxminan 10 xabar/soniya.

            summary = (
                "📣 <b>Ommaviy xabar yuborish yakunlandi</b>\n\n"
                f"👥 Jami qabul qiluvchilar: <b>{total:,}</b>\n"
                f"✅ Muvaffaqiyatli yuborildi: <b>{sent_count:,}</b>\n"
                f"❌ Yuborilmadi: <b>{failed_count:,}</b>\n\n"
                "Xatolar tafsilotlari Railway loglarida qayd etildi."
            )
            bot.send_message(ADMIN_ID, summary, parse_mode="HTML")
            logging.info("Ommaviy xabar yuborish yakunlandi | jami=%s | yuborildi=%s | xato=%s", total, sent_count, failed_count)
        except Exception as e:
            logging.exception("Ommaviy xabar yuborish jarayonida umumiy xatolik: %s", e)
            try:
                bot.send_message(ADMIN_ID, "❌ Ommaviy xabar jarayonida umumiy xatolik yuz berdi. Railway loglarini tekshiring.")
            except Exception:
                pass
        finally:
            if conn is not None:
                conn.close()
            with broadcast_lock:
                broadcast_active = False

    threading.Thread(target=_send_broadcast, daemon=True, name="admin-broadcast-worker").start()



@bot.message_handler(commands=["bonus"])
def admin_bonus_command(message):
    if not _admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Format: /bonus USER_ID KUN\nMisol: /bonus 123456789 30")
        return
    try:
        target_id = int(parts[1])
        days = int(parts[2])
        if days <= 0 or days > 3650:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ USER_ID son, KUN esa 1–3650 oralig'ida bo'lishi kerak.")
        return

    add_user_to_db(target_id)
    now = int(time.time())
    duration = days * 24 * 3600
    conn = _connect_db(timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        row = cur.execute("SELECT status, plan_key, premium_until FROM users WHERE user_id=?", (target_id,)).fetchone()
        old_until = int(row["premium_until"] or 0) if row else 0
        old_active = bool(row and is_active_paid_status(row["status"] or "", old_until))
        new_until = max(now, old_until) + duration if old_active else now + duration
        if old_active and (row["plan_key"] or "") in TARIFFS:
            # Existing paid/teacher plan: extend it without destroying its plan type.
            cur.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until, target_id))
        else:
            cur.execute(
                "UPDATE users SET status=?, plan_key=?, premium_until=?, premium_source=? WHERE user_id=?",
                ("PRO ✨ (Admin Bonus)", "bonus", new_until, "admin_bonus", target_id),
            )
        conn.commit()
    finally:
        conn.close()

    uz_time = time.strftime("%d.%m.%Y %H:%M", time.gmtime(new_until + 5 * 3600))
    bot.send_message(message.chat.id, f"🎁 Bonus berildi.\n👤 User: {target_id}\n⏰ Gacha: {uz_time}\n📅 Muddat: {days} kun")
    try:
        lang = get_user_lang(target_id)
        texts = {
            "uz": f"🎁 Sizga administrator tomonidan {days} kunlik bepul Premium berildi!\n\n⏰ Gacha: {uz_time}\n👑 Status: PRO ✨ (Admin Bonus)",
            "ru": f"🎁 Администратор предоставил вам бесплатный Premium на {days} дней!\n\n⏰ До: {uz_time}\n👑 Статус: PRO ✨ (Admin Bonus)",
            "en": f"🎁 The administrator granted you {days} days of free Premium!\n\n⏰ Until: {uz_time}\n👑 Status: PRO ✨ (Admin Bonus)",
        }
        bot.send_message(target_id, texts.get(lang, texts["uz"]))
    except Exception:
        pass


@bot.message_handler(commands=["bonus_teacher"])
def admin_bonus_teacher_command(message):
    """Grant a free Teacher plan for testing; admin-only and never charges the user."""
    if not _admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Format: /bonus_teacher USER_ID KUN\nMisol: /bonus_teacher 123456789 30")
        return
    try:
        target_id = int(parts[1])
        days = int(parts[2])
        if days <= 0 or days > 3650:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ USER_ID son, KUN esa 1–3650 oralig'ida bo'lishi kerak.")
        return

    add_user_to_db(target_id)
    now = int(time.time())
    duration = days * 24 * 3600
    conn = _connect_db(timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        row = cur.execute(
            "SELECT status, plan_key, premium_until, premium_source FROM users WHERE user_id=?",
            (target_id,),
        ).fetchone()
        old_until = int(row["premium_until"] or 0) if row else 0
        old_active = bool(row and is_active_paid_status(row["status"] or "", old_until))
        if old_active:
            bot.send_message(
                message.chat.id,
                f"ℹ️ User {target_id} hozir faol Premium tarifda. Uning pullik tarifiga tegilmadi."
            )
            return

        new_until = now + duration
        cur.execute(
            "UPDATE users SET status=?, plan_key=?, premium_until=?, premium_source=? WHERE user_id=?",
            ("PRO ✨ (Admin Bonus)", "teachers", new_until, "admin_bonus", target_id),
        )
        conn.commit()
    finally:
        conn.close()

    uz_time = time.strftime("%d.%m.%Y %H:%M", time.gmtime(new_until + 5 * 3600))
    bot.send_message(
        message.chat.id,
        f"👨‍🏫 Teacher Admin Bonus berildi.\n👤 User: {target_id}\n⏰ Gacha: {uz_time}\n📅 Muddat: {days} kun",
    )
    try:
        lang = get_user_lang(target_id)
        texts = {
            "uz": f"👨‍🏫 Sizga administrator tomonidan {days} kunlik bepul Teacher Premium berildi!\n\n⏰ Gacha: {uz_time}\n👑 Status: PRO ✨ (Admin Bonus)",
            "ru": f"👨‍🏫 Администратор предоставил вам бесплатный Teacher Premium на {days} дней!\n\n⏰ До: {uz_time}\n👑 Статус: PRO ✨ (Admin Bonus)",
            "en": f"👨‍🏫 The administrator granted you {days} days of free Teacher Premium!\n\n⏰ Until: {uz_time}\n👑 Status: PRO ✨ (Admin Bonus)",
        }
        bot.send_message(target_id, texts.get(lang, texts["uz"]))
    except Exception:
        pass


@bot.message_handler(commands=["bonus_revoke"])
def admin_bonus_revoke_command(message):
    if not _admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Format: /bonus_revoke USER_ID")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ USER_ID noto'g'ri.")
        return
    conn = _connect_db(timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT premium_source FROM users WHERE user_id=?", (target_id,)).fetchone()
        if not row or row["premium_source"] != "admin_bonus":
            bot.send_message(message.chat.id, "ℹ️ Bu foydalanuvchida alohida Admin Bonus topilmadi.")
            return
        conn.execute("UPDATE users SET status='Oddiy foydalanuvchi', plan_key='', premium_until=0, premium_source='paid' WHERE user_id=?", (target_id,))
        conn.commit()
    finally:
        conn.close()
    bot.send_message(message.chat.id, f"✅ Admin Bonus bekor qilindi: {target_id}")


@bot.message_handler(commands=["payment_ai_status"])
def admin_payment_ai_status(message):
    if not _admin_only(message):
        return
    st = p2p_payment_ai.get_status()
    b = st["budget"]
    bot.send_message(message.chat.id,
        "🧾 P2P AI STATUS\n\n"
        f"Model: {st['model']}\n"
        f"API keys: {'Sozlangan' if st['configured'] else 'Sozlanmagan'}\n"
        f"Queue: {st['queue_size']}/{st['queue_limit']}\n"
        f"Bugungi AI so'rovlar: {b.get('daily_requests',0)}/{b.get('daily_limit',0)}\n"
        f"Oylik AI so'rovlar: {b.get('monthly_requests',0)}/{b.get('monthly_request_limit',0)}\n"
        f"Oylik taxminiy xarajat: ${b.get('monthly_spend_usd',0.0):.4f}\n"
        f"Budget: ${b.get('monthly_budget_usd',0.0):.2f}")


# --- ISHONCHLI TO'LOV CHEKI QABUL QILISH (STABLE PHOTO HANDLER) ---
@bot.message_handler(content_types=["photo"])
def handle_receipt_photo(message):
    """Receive a P2P receipt and enqueue it for isolated Gemini Vision processing."""
    user_id = message.from_user.id
    user_lang = get_user_lang(user_id)

    conn = _connect_db(timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        pending_pay = conn.execute(
            "SELECT tx_id, tariff_name, tariff_price FROM payments WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()
    finally:
        conn.close()

    if not pending_pay:
        return

    tx_id = pending_pay["tx_id"]
    file_id = message.photo[-1].file_id
    queued, reason = p2p_payment_ai.enqueue_receipt(tx_id, user_id, file_id)
    if queued:
        # We intentionally do not wait for Gemini here. The Telegram handler stays fast.
        try:
            receipt_received_messages = {
                "uz": "⏳ Chekingiz qabul qilindi. AI tekshiruvi navbatga qo'yildi. To'lov avtomatik tekshiriladi.",
                "ru": "⏳ Чек получен. Проверка AI поставлена в очередь. Платёж будет проверен автоматически.",
                "en": "⏳ Your receipt has been received. AI verification has been queued. Your payment will be checked automatically.",
            }
            bot.send_message(
                message.chat.id,
                receipt_received_messages.get(user_lang, receipt_received_messages["uz"]),
            )
        except Exception:
            pass
    else:
        try:
            bot.send_message(
                message.chat.id,
                "⚠️ Hozir AI tekshiruv navbati to'lib qoldi. Chekingiz saqlandi, keyinroq qayta ishlanadi.",
            )
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("p2p_"))
def handle_p2p_admin_decision(call):
    p2p_payment_ai.handle_admin_decision(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("p_"))
def handle_admin_decision(call):
    if not is_admin_id(call.from_user.id):
        bot.answer_callback_query(call.id, "Siz administrator emassiz!", show_alert=True)
        return

    parts = call.data.split("_")
    action = parts[1]
    tx_id = parts[2]
    user_id = int(parts[3])
    user_lang = get_user_lang(user_id)

    conn = _connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT status, tariff_name FROM payments WHERE tx_id = ?", (tx_id,))
    pay_row = cursor.fetchone()

    if not pay_row or pay_row[0] != "pending":
        bot.answer_callback_query(call.id, "Bu so'rov allaqachon ko'rib chiqilgan!", show_alert=True)
        conn.close()
        return

    tariff_name = pay_row[1]

    if action == "app":
        current_time = int(time.time())
        # Eski to'lovlar uchun nomdan, yangi to'lovlar uchun canonical nomdan reja aniqlanadi.
        plan_key = get_plan_key(tariff_name)
        if not plan_key:
            low = tariff_name.lower()
            if "teacher" in low or "o'qit" in low or "учител" in low:
                plan_key = "teachers"
            elif "weekly" in low or "haft" in low or "недель" in low or "7" in low:
                plan_key = "weekly"
            elif "monthly" in low or "oy" in low or "месяч" in low or "30" in low:
                plan_key = "monthly"
            else:
                plan_key = "daily"
        duration = TARIFFS[plan_key]["duration"]
        premium_until_timestamp = current_time + duration
        cursor.execute("UPDATE payments SET status = 'approved' WHERE tx_id = ?", (tx_id,))
        display_name = localized_tariff_name(plan_key, user_lang)
        cursor.execute(
            "UPDATE users SET status = ?, plan_key = ?, premium_until = ?, premium_source = ? WHERE user_id = ?",
            (f"PRO ✨ ({display_name})", plan_key, premium_until_timestamp, "paid", user_id),
        )
        conn.commit()

        bot.answer_callback_query(call.id, "To'lov tasdiqlandi!")
        try:
            bot.edit_message_caption(
                f"✅ {call.message.caption}\n\n🟢 TASDIQLANDI!",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass
        try:
            succ_msg = MESSAGES[user_lang]["payment_approved"].format(tariff_name=display_name)
            bot.send_message(user_id, succ_msg)
        except Exception:
            pass

    elif action == "rej":
        cursor.execute("UPDATE payments SET status = 'rejected' WHERE tx_id = ?", (tx_id,))
        conn.commit()
        bot.answer_callback_query(call.id, "To'lov rad etildi.")
        try:
            bot.edit_message_caption(
                f"❌ {call.message.caption}\n\n🔴 RAD ETILDI!",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass
        try:
            rej_msg = MESSAGES[user_lang]["payment_rejected"]
            bot.send_message(user_id, rej_msg)
        except Exception:
            pass

    conn.close()



# --- HYBRID V2 DATABASE DIAGNOSTIC (ADMIN ONLY / READ-ONLY) ---
@app.get("/api/admin/hybrid-db-check")
def hybrid_db_check(
    user_id: int,
    table_name: Optional[str] = None,
    record_id: Optional[str] = None,
):
    """SQLite MASTER va PostgreSQL MIRROR holatini o'qish uchun diagnostika."""
    if not is_admin_id(user_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        import hybrid_sync
        return hybrid_sync.diagnostic_snapshot(
            DB_PATH,
            table_name=table_name,
            pk_value=record_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.exception("Hybrid DB diagnostic error")
        raise HTTPException(status_code=500, detail=f"Hybrid DB diagnostic failed: {e}")


# --- SQLITE BACKUP DOWNLOAD ENDPOINT ---
# Faqat botdagi administrator buyrug'i bergan, 10 daqiqalik bir martalik token qabul qilinadi.
BACKUP_DIR = "/tmp/quiz_pilot_backups"


def _remove_temp_backup(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        logging.exception("Vaqtinchalik SQLite backup faylini o'chirishda xatolik.")


@app.get("/admin/sqlite-backup")
def download_sqlite_backup(
    token: str = Query(..., min_length=32, max_length=128),
    background_tasks: BackgroundTasks = None,
):
    if not _consume_sqlite_backup_token(token):
        raise HTTPException(
            status_code=403,
            detail="Havola yaroqsiz, muddati tugagan yoki avval ishlatilgan. Botdan /db_backup buyrug'ini qayta yuboring.",
        )
    if not os.path.isfile(DB_PATH):
        raise HTTPException(status_code=404, detail="SQLite baza fayli topilmadi.")

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=5))).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"quiz_ai_sqlite_backup_{stamp}.db")

    try:
        # SQLite backup API jonli bazaning izchil nusxasini yaratadi;
        # asosiy DB fayli ko'chirilmaydi va o'zgartirilmaydi.
        src_conn = _connect_db(timeout=30)
        dst_conn = sqlite3.connect(backup_path, timeout=30, check_same_thread=False)
        try:
            src_conn.execute("PRAGMA busy_timeout=30000")
            src_conn.backup(dst_conn)
            check_result = dst_conn.execute("PRAGMA quick_check").fetchone()
            if not check_result or check_result[0] != "ok":
                raise sqlite3.DatabaseError("Zaxira nusxasi quick_check tekshiruvidan o'tmadi.")
        finally:
            dst_conn.close()
            src_conn.close()
    except Exception:
        _remove_temp_backup(backup_path)
        logging.exception("SQLite backup yaratishda xatolik yuz berdi.")
        raise HTTPException(status_code=500, detail="Zaxira nusxasini yaratib bo'lmadi.")

    tasks = background_tasks or BackgroundTasks()
    tasks.add_task(_remove_temp_backup, backup_path)
    return FileResponse(
        backup_path,
        media_type="application/octet-stream",
        filename=os.path.basename(backup_path),
        background=tasks,
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(sqlite3.OperationalError)
async def teacher_sqlite_operational_error(request: Request, exc: sqlite3.OperationalError):
    logging.exception("SQLite OperationalError: %s", exc)
    msg = "__TEACHER_DB_BUSY__" if any(x in str(exc).lower() for x in ("locked", "busy", "readonly")) else "__TEACHER_SERVER_ERROR__"
    return JSONResponse(status_code=500, content={"status": "error", "detail": msg})

@app.exception_handler(sqlite3.IntegrityError)
async def teacher_sqlite_integrity_error(request: Request, exc: sqlite3.IntegrityError):
    logging.exception("SQLite IntegrityError: %s", exc)
    return JSONResponse(status_code=500, content={"status": "error", "detail": "__TEACHER_SERVER_ERROR__"})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    """Railway uchun minimal liveness/readiness tekshiruvi; ichki ma'lumotlarni oshkor qilmaydi."""
    conn = None
    try:
        if not os.path.isfile(DB_PATH):
            raise sqlite3.OperationalError("database file is not present")
        conn = _connect_db(timeout=3)
        conn.execute("SELECT 1").fetchone()
        return JSONResponse(
            status_code=200,
            content={"status": "ok"},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        # Tashqi health-check javobida DB yo'li yoki xato tafsilotlari berilmaydi.
        logging.warning("Health check muvaffaqiyatsiz | sabab=%s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable"},
            headers={"Cache-Control": "no-store"},
        )
    finally:
        if conn is not None:
            conn.close()


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    show_quiz_explanations = os.getenv("SHOW_QUIZ_EXPLANATIONS", "false").strip().lower() in ("1", "true", "yes", "on")
    response = templates.TemplateResponse("index.html", {
        "request": request,
        "show_quiz_explanations": show_quiz_explanations,
    })
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/api/tariff-config")
def get_tariff_config():
    """Mini App uchun Railway'dagi joriy tariflar va ommaga ko'rsatiladigan karta raqami."""
    return {
        "tariffs": {
            key: {
                "price": item["price"],
                "duration": item["duration"],
                "duration_value": item["duration_value"],
                "duration_unit": item["duration_unit"],
                "names": item["names"],
            }
            for key, item in TARIFFS.items()
        },
        "card_number": P2P_CARD_NUMBER,
        "free_limits": {
            "quiz": None if FREE_QUIZ_LIMIT == -1 else FREE_QUIZ_LIMIT,
            "public": None if FREE_PUBLIC_LIMIT == -1 else FREE_PUBLIC_LIMIT,
            "flashcard": None if FREE_FLASHCARD_LIMIT == -1 else FREE_FLASHCARD_LIMIT,
        },
        "free_limits_display": {
            "quiz": {lang: format_free_limit(FREE_QUIZ_LIMIT, lang) for lang in ("uz", "ru", "en")},
            "public": {lang: format_free_limit(FREE_PUBLIC_LIMIT, lang) for lang in ("uz", "ru", "en")},
            "flashcard": {lang: format_free_limit(FREE_FLASHCARD_LIMIT, lang) for lang in ("uz", "ru", "en")},
        },
    }


@app.post("/api/payment-intent")
def api_payment_intent(req: PaymentIntentRequest):
    if req.action == "payment_intent":
        if req.tariff_key not in TARIFFS:
            raise HTTPException(status_code=400, detail="Noto'g'ri tarif")
        threading.Thread(
            target=trigger_payment_flow,
            args=(req.user_id, req.tariff_name, req.tariff_price, req.tariff_key),
            daemon=True,
        ).start()
        return {
            "status": "ok",
            "message": "To'lov so'rovi muvaffaqiyatli qabul qilindi",
        }
    raise HTTPException(status_code=400, detail="Noto'g'ri amal")


@app.get("/api/premium-status")
def get_premium_status(user_id: int):
    add_user_to_db(user_id)
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, plan_key, free_used, public_free_used, flashcard_free_used, premium_until, created_at, premium_source "
        "FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {
            "status": "ok",
            "user_status": "Oddiy foydalanuvchi",
            "free_used": 0,
            "public_free_used": 0,
            "flashcard_free_used": 0,
            "public_remaining": free_limit_remaining(FREE_PUBLIC_LIMIT, 0),
            "flashcard_remaining": free_limit_remaining(FREE_FLASHCARD_LIMIT, 0),
            "quiz_remaining": free_limit_remaining(FREE_QUIZ_LIMIT, 0),
            "public_unlimited": FREE_PUBLIC_LIMIT == -1,
            "flashcard_unlimited": FREE_FLASHCARD_LIMIT == -1,
            "quiz_unlimited": FREE_QUIZ_LIMIT == -1,
            "plan_key": "",
            "is_paid": False,
            "is_teacher": False,
        }

    user_status = row["status"] or "Oddiy foydalanuvchi"
    plan_key = row["plan_key"] or get_plan_key(user_status)
    premium_until = row["premium_until"] or 0
    premium_source = row["premium_source"] or "paid"
    free_used = row["free_used"] if row["free_used"] is not None else 0
    public_free_used = row["public_free_used"] if row["public_free_used"] is not None else 0
    flashcard_free_used = row["flashcard_free_used"] if row["flashcard_free_used"] is not None else 0
    created_at = row["created_at"] or int(time.time())
    now = int(time.time())

    if is_active_paid_status(user_status, premium_until):
        pass
    elif "PRO" in user_status and premium_until > 0 and now > premium_until:
        cursor.execute("UPDATE users SET status = 'Oddiy foydalanuvchi', plan_key = '', premium_until = 0, premium_source = 'paid' WHERE user_id = ?", (user_id,))
        conn.commit()
        user_status, plan_key, premium_until = "Oddiy foydalanuvchi", "", 0

    # 30 kunlik bepul hisob davri pullik davrdan mustaqil ishlaydi.
    if now - created_at >= 30 * 24 * 3600 and not is_active_paid_status(user_status, premium_until):
        cursor.execute(
            "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()
        free_used = 0
        public_free_used = 0
        flashcard_free_used = 0

    is_paid = is_active_paid_status(user_status, premium_until)
    is_teacher = is_paid and plan_key == "teachers"
    lang = get_user_lang(user_id)
    display_status = user_status
    if is_paid:
        if premium_source == "admin_bonus" or plan_key == "bonus":
            display_status = "PRO ✨ (Admin Bonus)"
        else:
            display_status = f"PRO ✨ ({localized_tariff_name(plan_key, lang)})"
        uzbek_time = time.gmtime(premium_until + 5 * 3600)
        readable_date = time.strftime("%d.%m.%Y %H:%M", uzbek_time)
        if lang == "ru": display_status += f" (До: {readable_date})"
        elif lang == "en": display_status += f" (Until: {readable_date})"
        else: display_status += f" (Gacha: {readable_date})"
    conn.close()
    return {
        "status": "ok",
        "user_status": display_status,
        "free_used": free_used,
        "public_free_used": public_free_used,
        "flashcard_free_used": flashcard_free_used,
        "public_remaining": free_limit_remaining(FREE_PUBLIC_LIMIT, public_free_used),
        "flashcard_remaining": free_limit_remaining(FREE_FLASHCARD_LIMIT, flashcard_free_used),
        "quiz_remaining": free_limit_remaining(FREE_QUIZ_LIMIT, free_used),
        "public_unlimited": FREE_PUBLIC_LIMIT == -1,
        "flashcard_unlimited": FREE_FLASHCARD_LIMIT == -1,
        "quiz_unlimited": FREE_QUIZ_LIMIT == -1,
        "plan_key": plan_key,
        "is_paid": is_paid,
        "is_teacher": is_teacher,
    }

MAX_FILE_SIZE = MAX_UPLOAD_FILE_BYTES  # MAX_UPLOAD_FILE_MB (baytlarda)

@app.get("/api/upload-limits")
async def get_upload_limits():
    """Mini App uchun Railway orqali sozlangan yuklash limitlari."""
    return {
        "max_upload_file_mb": MAX_UPLOAD_FILE_MB,
        "max_text_input_chars": MAX_TEXT_INPUT_CHARS,
        "max_extracted_text_chars": MAX_EXTRACTED_TEXT_CHARS,
    }

logging.info(
    "Yuklash limitlari | fayl_hajmi=%s MB | PDF_sahifalari=%s ta | kiritilgan_matn=%s belgi | fayldan_ajratilgan_matn=%s belgi",
    MAX_UPLOAD_FILE_MB, MAX_PDF_PAGES, MAX_TEXT_INPUT_CHARS, MAX_EXTRACTED_TEXT_CHARS
)

@app.post("/api/create-quiz-web")
async def create_quiz_web(
    user_id: int = Form(...),
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    quiz_title: Optional[str] = Form(None),
):
    add_user_to_db(user_id)
    user_lang = get_user_lang(user_id)

    conn_check = _connect_db()
    conn_check.row_factory = sqlite3.Row
    cursor_check = conn_check.cursor()
    cursor_check.execute(
        "SELECT status, premium_until, free_used, created_at FROM users WHERE user_id = ?",
        (user_id,),
    )
    user_row = cursor_check.fetchone()

    if user_row:
        current_status = user_row["status"] or "Oddiy foydalanuvchi"
        premium_until = user_row["premium_until"] or 0
        free_used = user_row["free_used"] if user_row["free_used"] is not None else 0
        created_at = user_row["created_at"] or int(time.time())
        current_now = int(time.time())

        thirty_days_sec = 30 * 24 * 3600
        if current_now - created_at >= thirty_days_sec:
            cursor_check.execute(
                "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = ? WHERE user_id = ?",
                (current_now, user_id),
            )
            conn_check.commit()
            free_used = 0

        if (
            "PRO" in current_status
            and premium_until > 0
            and current_now > premium_until
        ):
            cursor_check.execute(
                "UPDATE users SET status = 'Oddiy foydalanuvchi', premium_until = 0 WHERE user_id = ?",
                (user_id,),
            )
            conn_check.commit()
            current_status = "Oddiy foydalanuvchi"

        # 30 kunlik bepul limit: faqat 1 ta.
        # Muhim: bir foydalanuvchi bir vaqtning o'zida 2 ta request yuborsa,
        # ikkalasi ham limitdan o'tib ketmasligi uchun bepul joyni
        # Gemini chaqiruvidan OLDIN atomik tarzda band qilamiz.
        if "PRO" not in current_status and FREE_QUIZ_LIMIT != -1:
            cursor_check.execute(
                "UPDATE users "
                "SET free_used = COALESCE(free_used, 0) + 1 "
                "WHERE user_id = ? AND COALESCE(free_used, 0) < ?",
                (user_id, FREE_QUIZ_LIMIT),
            )
            if cursor_check.rowcount != 1:
                conn_check.close()
                return {
                    "status": "error",
                    "error_code": "free_limit",
                    "message": MESSAGES[user_lang]["quiz_limit_reached"],
                }
            conn_check.commit()
            free_slot_reserved = True
        else:
            free_slot_reserved = False
    else:
        free_slot_reserved = False

    conn_check.close()

    raw_text = ""
    auto_title = "Matnli Test"

    if file and file.filename and len(file.filename.strip()) > 0:
        # Faylni avval xotirada bir marta o'qiymiz va diskka faqat tekshiruvdan
        # o'tgandan keyin yozamiz. Bu server diskini keraksiz yukdan himoya qiladi.
        original_name = Path(file.filename).name
        extension = Path(original_name).suffix.lower()
        if extension not in ALLOWED_UPLOAD_EXTENSIONS:
            if free_slot_reserved:
                conn_restore = _connect_db()
                cur_restore = conn_restore.cursor()
                cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                conn_restore.commit(); conn_restore.close()
            return {"status": "error", "message": file_protection_message(user_lang, "unsupported")}

        try:
            faylni_oqish_boshlanishi = time.perf_counter()
            contents = await file.read()
            faylni_oqish_vaqti = time.perf_counter() - faylni_oqish_boshlanishi
            logging.info(
                "TEST YARATISH | Fayl xotiraga o'qildi | nomi=%s | turi=%s | hajmi=%.2f MB | faylni_o'qish_vaqti=%.3f soniya",
                original_name, extension, len(contents) / (1024 * 1024), faylni_oqish_vaqti
            )
            if len(contents) > MAX_UPLOAD_FILE_BYTES:
                logging.warning(
                    "Fayl yuklanmadi: hajm belgilangan limitdan oshdi | foydalanuvchi=%s | fayl=%s | hajm=%.2f MB | limit=%s MB",
                    user_id, original_name, len(contents) / (1024 * 1024), MAX_UPLOAD_FILE_MB
                )
                if free_slot_reserved:
                    conn_restore = _connect_db()
                    cur_restore = conn_restore.cursor()
                    cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                    conn_restore.commit(); conn_restore.close()
                return {"status": "error", "message": file_protection_message(user_lang, "too_large")}

            if contents:
                # Yuklangan faylni vaqtinchalik diskka yozmasdan RAM ichida o'qiymiz.
                # Bu bir xil nomli fayllar to'qnashuvi va keraksiz disk amallarini yo'q qiladi.
                matn_ajratish_boshlanishi = time.perf_counter()
                pdf_sahifalar_soni = 0
                word_paragraflar_soni = 0
                text_parts = []
                text_len = 0

                if extension == ".pdf":
                    if pymupdf is not None:
                        # PyMuPDF: PDF matnini odatda tezroq ajratadi.
                        with pymupdf.open(stream=contents, filetype="pdf") as pdf_doc:
                            page_count = pdf_doc.page_count
                            pdf_sahifalar_soni = page_count
                            if page_count > MAX_PDF_PAGES:
                                if free_slot_reserved:
                                    conn_restore = _connect_db()
                                    cur_restore = conn_restore.cursor()
                                    cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                                    conn_restore.commit(); conn_restore.close()
                                return {"status": "error", "message": file_protection_message(user_lang, "too_many_pages")}

                            for page in pdf_doc:
                                page_text = page.get_text("text") or ""
                                if page_text:
                                    text_len += len(page_text)
                                    if text_len > MAX_EXTRACTED_TEXT_CHARS:
                                        logging.warning(
                                            "PDF fayli rad etildi: matn belgilar limiti oshdi | foydalanuvchi=%s | belgilar=%s | limit=%s",
                                            user_id, text_len, MAX_EXTRACTED_TEXT_CHARS
                                        )
                                        if free_slot_reserved:
                                            conn_restore = _connect_db()
                                            cur_restore = conn_restore.cursor()
                                            cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                                            conn_restore.commit(); conn_restore.close()
                                        return {"status": "error", "message": file_protection_message(user_lang, "too_much_text")}
                                    text_parts.append(page_text)
                    else:
                        # PyMuPDF mavjud bo'lmasa, eski pypdf usuli bilan davom etamiz.
                        reader = PdfReader(BytesIO(contents))
                        page_count = len(reader.pages)
                        pdf_sahifalar_soni = page_count
                        if page_count > MAX_PDF_PAGES:
                            if free_slot_reserved:
                                conn_restore = _connect_db()
                                cur_restore = conn_restore.cursor()
                                cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                                conn_restore.commit(); conn_restore.close()
                            return {"status": "error", "message": file_protection_message(user_lang, "too_many_pages")}
                        for page in reader.pages:
                            page_text = page.extract_text() or ""
                            if page_text:
                                text_len += len(page_text)
                                if text_len > MAX_EXTRACTED_TEXT_CHARS:
                                    logging.warning(
                                        "PDF fayli rad etildi: matn belgilar limiti oshdi | foydalanuvchi=%s | belgilar=%s | limit=%s",
                                        user_id, text_len, MAX_EXTRACTED_TEXT_CHARS
                                    )
                                    if free_slot_reserved:
                                        conn_restore = _connect_db()
                                        cur_restore = conn_restore.cursor()
                                        cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                                        conn_restore.commit(); conn_restore.close()
                                    return {"status": "error", "message": file_protection_message(user_lang, "too_much_text")}
                                text_parts.append(page_text)
                    raw_text = "\n".join(text_parts)
                    auto_title = Path(original_name).stem

                elif extension == ".docx":
                    # Word fayli ham RAM ichida ochiladi. Paragraflar va jadvallar
                    # hujjatdagi asl ketma-ketlikda o'qiladi.
                    doc = docx.Document(BytesIO(contents))
                    word_paragraflar_soni = len(doc.paragraphs)
                    for block in doc.element.body.iterchildren():
                        if block.tag.endswith("}p"):
                            part = DocxParagraph(block, doc).text or ""
                            if part:
                                text_len += len(part)
                                if text_len > MAX_EXTRACTED_TEXT_CHARS:
                                    logging.warning(
                                        "Word fayli rad etildi: matn belgilar limiti oshdi | foydalanuvchi=%s | belgilar=%s | limit=%s",
                                        user_id, text_len, MAX_EXTRACTED_TEXT_CHARS
                                    )
                                    if free_slot_reserved:
                                        conn_restore = _connect_db()
                                        cur_restore = conn_restore.cursor()
                                        cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                                        conn_restore.commit(); conn_restore.close()
                                    return {"status": "error", "message": file_protection_message(user_lang, "too_much_text")}
                                text_parts.append(part)
                        elif block.tag.endswith("}tbl"):
                            table = DocxTable(block, doc)
                            for row in table.rows:
                                # Jadval ustunlari orasiga ajratuvchi qo'yiladi,
                                # shunda savol va javoblar bir-biriga qo'shilib ketmaydi.
                                part = " | ".join((cell.text or "").strip() for cell in row.cells)
                                if part.strip(" |\t\r\n"):
                                    text_len += len(part)
                                    if text_len > MAX_EXTRACTED_TEXT_CHARS:
                                        logging.warning(
                                            "Word fayli rad etildi: matn belgilar limiti oshdi | foydalanuvchi=%s | belgilar=%s | limit=%s",
                                            user_id, text_len, MAX_EXTRACTED_TEXT_CHARS
                                        )
                                        if free_slot_reserved:
                                            conn_restore = _connect_db()
                                            cur_restore = conn_restore.cursor()
                                            cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                                            conn_restore.commit(); conn_restore.close()
                                        return {"status": "error", "message": file_protection_message(user_lang, "too_much_text")}
                                    text_parts.append(part)
                    raw_text = "\n".join(text_parts)
                    auto_title = Path(original_name).stem

                # Faqat matnni tartibga keltiramiz; savollar, variantlar va formulalarni
                # o'chirmaslik uchun mazmunli qatorlar ichidagi belgilar o'zgartirilmaydi.
                raw_text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())

                matn_ajratish_vaqti = time.perf_counter() - matn_ajratish_boshlanishi
                logging.info(
                    "TEST YARATISH | Fayldan matn ajratildi | turi=%s | PDF_sahifalari=%s | Word_paragraflari=%s | matn_belgilari=%s | matn_ajratish_vaqti=%.3f soniya",
                    extension, pdf_sahifalar_soni, word_paragraflar_soni, len(raw_text), matn_ajratish_vaqti
                )
        except Exception as e:
            logging.error("Faylni o'qish yoki matnni ajratishda xatolik yuz berdi: %s", e)
            if free_slot_reserved:
                try:
                    conn_restore = _connect_db()
                    cur_restore = conn_restore.cursor()
                    cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                    conn_restore.commit(); conn_restore.close()
                except Exception: pass
            return {"status": "error", "message": file_protection_message(user_lang, "unreadable")}

    if not raw_text.strip() and text:
        if len(text) > MAX_TEXT_INPUT_CHARS:
            logging.warning(
                "Matn yuborilmadi: belgilar limiti oshdi | foydalanuvchi=%s | belgilar=%s | limit=%s",
                user_id, len(text), MAX_TEXT_INPUT_CHARS
            )
            if free_slot_reserved:
                try:
                    conn_restore = _connect_db()
                    cur_restore = conn_restore.cursor()
                    cur_restore.execute(
                        "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?",
                        (user_id,),
                    )
                    conn_restore.commit()
                    conn_restore.close()
                except Exception:
                    pass
            return {"status": "error", "message": file_protection_message(user_lang, "text_too_long")}
        raw_text = text
        auto_text_clean = text.replace("\n", " ").strip()
        auto_title = (
            auto_text_clean[:18] + "..."
            if len(auto_text_clean) > 18
            else auto_text_clean
        )

    if not raw_text.strip():
        if free_slot_reserved:
            try:
                conn_restore = _connect_db()
                cur_restore = conn_restore.cursor()
                cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
                conn_restore.commit(); conn_restore.close()
            except Exception: pass
        return {"status": "error", "message": file_protection_message(user_lang, "unreadable")}

    # Gemini SDK chaqiruvi sinxron bo'lgani uchun uni alohida threadga chiqaramiz.
    # Shu bilan boshqa foydalanuvchilarning WebApp requestlari event loopni bloklamaydi.
    logging.info(
        "TEST YARATISH | Gemini so'roviga yuborish boshlandi | matn_belgilari=%s",
        len(raw_text)
    )
    quiz_json_raw = await asyncio.to_thread(generate_quiz_from_gemini, raw_text)
    if not quiz_json_raw:
        if free_slot_reserved:
            try:
                conn_restore = _connect_db()
                cur_restore = conn_restore.cursor()
                cur_restore.execute(
                    "UPDATE users "
                    "SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 "
                    "THEN free_used - 1 ELSE 0 END "
                    "WHERE user_id = ?",
                    (user_id,),
                )
                conn_restore.commit()
                conn_restore.close()
            except Exception as e:
                logging.error(f"Bepul limitni qaytarishda xato: {e}")
        return {"status": "error", "message": "AI test generatsiya qila olmadi."}

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        if not GEMINI_INCLUDE_EXPLANATIONS:
            # UI va saqlash formati bilan moslik uchun izoh maydoni bo'sh qoladi.
            # Gemini sxemasida bu maydon yo'q, shuning uchun undan izoh tokenlari so'ralmaydi.
            for item in items:
                if isinstance(item, dict):
                    item["explanation"] = ""
        # Gemini qanday joylashtirishidan qat'i nazar, javob variantlari shu yerda
        # xavfsiz va muvozanatli random qilinadi.
        items = randomize_quiz_answer_positions(items)
        quiz_data["quizzes"] = items
        if not items:
            return {
                "status": "error",
                "message": "AI savollar ro'yxatini bo'sh qaytardi.",
            }

        quiz_id = f"q_{uuid.uuid4().hex}"
        final_title = (
            quiz_title.strip() if (quiz_title and quiz_title.strip()) else auto_title
        )

        conn = _connect_db()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO quizzes (id, user_id, title, total, answered, quiz_json, created_at, last_score, last_percent, is_public)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                quiz_id,
                user_id,
                final_title[:30],
                len(items),
                0,
                quiz_json_raw,
                int(time.time()),
                -1,
                -1,
            ),
        )
        conn.commit()
        conn.close()

        if free_slot_reserved:
            notify_free_limit_reached(user_id, "quiz")

        try:
            q_ready_msg = MESSAGES[user_lang]["quiz_ready"].format(
                title=final_title[:30],
                count=len(items)
            )
            bot.send_message(user_id, q_ready_msg)
        except Exception as e:
            logging.error(f"Telegram xabari yuborilmadi: {e}")
        return {"status": "ok", "quiz_id": quiz_id, "title": final_title[:30], "total": len(items)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/split-quiz")
def split_quiz(quiz_id: str, user_id: int, question_count: int):
    # Tayyor AI testini tanlangan savol soniga bo'lib, alohida testlar yaratadi.
    # Bu amal yangi AI test yaratmaydi va bepul/Premium limitiga tegmaydi.
    if question_count not in (20, 30, 40):
        raise HTTPException(status_code=400, detail="Noto'g'ri test hajmi")

    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, title, quiz_json, total FROM quizzes WHERE id = ? AND user_id = ?",
        (quiz_id, user_id),
    )
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Test topilmadi")

    try:
        quiz_data = json.loads(row["quiz_json"])
        items = quiz_data.get("quizzes", [])
        if not isinstance(items, list) or not items:
            raise ValueError("Savollar topilmadi")

        chunks = [items[i:i + question_count] for i in range(0, len(items), question_count)]
        if len(chunks) <= 1:
            conn.close()
            return {"status": "ok", "created": 0, "total": len(items), "message": "Bo'lish talab qilinmadi."}

        created_ids = []
        base_title = (row["title"] or "Test").strip() or "Test"
        created_at = int(time.time())

        try:
            cursor.execute("BEGIN")
            for index, chunk in enumerate(chunks, start=1):
                new_id = f"q_{uuid.uuid4().hex}"
                chunk_json = json.dumps({"quizzes": chunk}, ensure_ascii=False)
                chunk_title = f"{base_title[:24]} — {index}/{len(chunks)}"
                cursor.execute(
                    """INSERT INTO quizzes (id, user_id, title, total, answered, quiz_json, created_at, last_score, last_percent, is_public)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                    (new_id, user_id, chunk_title[:30], len(chunk), 0, chunk_json, created_at, -1, -1),
                )
                created_ids.append(new_id)

            # Faqat barcha qismlar muvaffaqiyatli yaratilgandan keyin asl katta test o'chiriladi.
            cursor.execute("DELETE FROM quizzes WHERE id = ? AND user_id = ?", (quiz_id, user_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return {
            "status": "ok",
            "created": len(created_ids),
            "total": len(items),
            "question_count": question_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Testni bo'lishda xatolik: {e}")
    finally:
        conn.close()


def randomize_quiz_answer_positions(items):
    """
    AI tomonidan berilgan variantlarni xavfsiz aralashtiradi va correct_index ni
    yangi joylashuvga moslaydi. To'g'ri javob mazmuni o'zgarmaydi.
    Savollar ko'p bo'lsa A/B/C/D pozitsiyalari muvozanatli taqsimlanadi.
    """
    if not isinstance(items, list) or not items:
        return items

    positions = [i % 4 for i in range(len(items))]
    secrets.SystemRandom().shuffle(positions)

    for item, target_index in zip(items, positions):
        if not isinstance(item, dict):
            continue
        options = list(item.get("options") or [])
        if len(options) < 2:
            continue
        options = options[:4]
        try:
            correct_index = int(item.get("correct_index", 0))
        except (TypeError, ValueError):
            correct_index = 0
        if correct_index < 0 or correct_index >= len(options):
            correct_index = 0

        # Indeks bilan ishlash bir xil matnli variantlarda ham to'g'ri javobni saqlaydi.
        correct_pair = (correct_index, options[correct_index])
        other_pairs = [(i, value) for i, value in enumerate(options) if i != correct_index]
        secrets.SystemRandom().shuffle(other_pairs)
        new_pairs = other_pairs[:]
        insert_at = min(target_index, len(new_pairs))
        new_pairs.insert(insert_at, correct_pair)

        item["options"] = [value for _, value in new_pairs]
        item["correct_index"] = next(i for i, pair in enumerate(new_pairs) if pair[0] == correct_index)

    return items

# --- AI NAVBATI YUKLAMASI BO'YICHA AVTOMATIK ADMIN OGOHLANTIRISHI ---
# Navbat sig'imining 80% yoki undan ko'pi band bo'lsa, adminga xabar yuboradi.
# Takroriy xabarlar 5 daqiqalik cooldown bilan cheklanadi.
_AI_QUEUE_ALERT_THRESHOLD = max(1, int(AI_MAX_QUEUE * 0.80))
_ai_queue_alert_lock = threading.Lock()
_ai_queue_last_alert_at = 0.0


def _safe_ai_error_text(error):
    """AI provider xatolaridan maxfiy kalitlarni olib tashlab, xavfsiz matn qaytaradi."""
    try:
        message = str(error or "Noma'lum AI xatosi")
    except Exception:
        message = "AI provider xatosi (tafsilotni o'qib bo'lmadi)"

    # API kalitlari log yoki /ai_monitor xabariga hech qachon chiqmasin.
    for secret in GOOGLE_API_KEYS:
        if secret:
            message = message.replace(secret, "[MAXFIY_API_KALIT]")

    # Provider xatolarida URL query yoki konfiguratsiya ko'rinishida kelishi mumkin.
    message = re.sub(
        r"(?i)([?&]key=|api[_ -]?key\s*[:=]\s*)([^&\s,;]+)",
        r"\1[MAXFIY]",
        message,
    )
    message = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [MAXFIY]", message)
    return message[:180]


def _maybe_alert_ai_queue_load():
    """Send a rate-limited admin alert when AI queue capacity is nearly exhausted."""
    global _ai_queue_last_alert_at

    if not ADMIN_ID:
        return

    try:
        queue_slots_free = max(0, int(getattr(ai_queue_slots, "_value", 0)))
        queue_slots_used = max(0, AI_MAX_QUEUE - queue_slots_free)
        if queue_slots_used < _AI_QUEUE_ALERT_THRESHOLD:
            return

        now = time.monotonic()
        with _ai_queue_alert_lock:
            if now - _ai_queue_last_alert_at < 300:
                return
            _ai_queue_last_alert_at = now

        percent = min(100.0, queue_slots_used * 100.0 / max(1, AI_MAX_QUEUE))
        alert = (
            "⚠️ <b>Quiz AI — AI navbati band</b>\\n"
            f"⏳ Navbat bandligi: <b>{queue_slots_used}/{AI_MAX_QUEUE} ({percent:.1f}%)</b>\\n"
            f"🟢 Bo'sh joylar: <b>{queue_slots_free}</b>\\n"
            "ℹ️ Bu avtomatik ogohlantirish. Holatni tekshirish uchun /monitor buyrug'idan foydalaning."
        )
        bot.send_message(ADMIN_ID, alert, parse_mode="HTML", disable_web_page_preview=True)
        logging.warning(
            "AI navbati yuqori yuklamada | band=%s | limit=%s | foiz=%.1f",
            queue_slots_used, AI_MAX_QUEUE, percent
        )
    except Exception:
        # Monitoring xatosi test yaratish jarayoniga xalaqit bermasligi kerak.
        logging.exception("AI navbati bo'yicha admin ogohlantirishini yuborishda xatolik")


def _generate_quiz_from_gemini_impl(extracted_text):
    """
    Professional AI Queue + Model Fallback + API Key Rotation + Retry +
    Timeout + Concurrency Protection.

    Primary model: GEMINI_MODEL.
    Fallback model: GEMINI_FALLBACK_MODEL.
    API key rotation and concurrency control remain independent.
    """
    if not GOOGLE_API_KEYS:
        logging.error("AI test API kalitlari topilmadi. GOOGLE_API_KEYS yoki GOOGLE_API_KEY_001... kabi raqamlangan Railway Variables sozlang.")
        return None

    if GEMINI_INCLUDE_EXPLANATIONS:
        if GEMINI_CONCISE_EXPLANATIONS:
            explanation_rule = "3. IZOH: Kirish tiliga mos, mazmunni saqlagan 1-2 qisqa gap yozing; takror va uzun kirishlardan qoching."
        else:
            explanation_rule = "3. IZOH: Har bir savol uchun qisqa izoh yozing."
        response_schema = QuizResponse
        language_rule = "Savollar, javob variantlari va izohlarni kirish matni bilan AYNAN BIR TILDA yarating."
    else:
        explanation_rule = "3. IZOH: IZOH YARATMANG. Javobda izoh maydoni umuman bo'lmasin."
        response_schema = QuizResponseWithoutExplanation
        language_rule = "Savollar va javob variantlarini kirish matni bilan AYNAN BIR TILDA yarating."

    if GEMINI_BALANCE_OPTIONS:
        option_rule = (
            "Variantlar qoidasi: to'g'ri javobni keraksiz tafsilot bilan uzaytirmang. "
            "4 variantning uzunligi va grammatik tuzilishi imkon qadar o'xshash bo'lsin; "
            "noto'g'ri variantlar ham ishonarli, mavzuga mos bo'lsin. "
            "To'g'ri javob faqat eng uzun yoki eng batafsil variant bo'lib qolmasin. "
            "Barcha variantlarni mazmunan aniq, ixcham va bir-biridan farqli yozing."
        )
    else:
        option_rule = ""

    if AI_PROMPT_OPTIMIZATION:
        # Ixcham prompt: savol soni, til, variant sifati va izoh qoidalari saqlanadi.
        system_instruction = f"""Siz test yaratadigan AI yordamchisiz. Qoidalarga qat'iy amal qiling:
1. {language_rule}
2. Tayyor test savollari bo'lsa, barchasini saqlang; tashlab ketmang, birlashtirmang yoki qisqartirmang. Natija soni manbadagi songa teng bo'lsin. 200, 300, 400 yoki undan ko'p savol bo'lsa ham, sonini kamaytirmang. Tayyor savollarga 40-50 cheklovi qo'llanmaydi.
3. Tayyor savollar bo'lmasa va matn nazariy material bo'lsa, undan ko'pi bilan 50 ta yangi savol tuzing.
4. {explanation_rule}
5. {option_rule}
Faqat berilgan materialga tayaning. Javobni belgilangan JSON sxemasiga mos qaytaring."""
    else:
        # Zarur bo'lsa, Railway Variables orqali avvalgi batafsil promptga qaytish mumkin.
        system_instruction = f"""Siz test yaratishga mo'ljallangan ilg'or AI yordamchisiz.
MUHIM QOIDALAR:
1. TIL QOIDASI: {language_rule}
2. SAVOLLAR SONI QOIDASI — BU QOIDA JUDA MUHIM:
   A) Agar kirish matnida oldindan mavjud test savollari yoki savol-javoblar ro'yxati bo'lsa, ularni BIRMA-BIR aniqlang va FAQAT formatini o'zgartirib qaytaring. Hech bir savolni tashlab ketmang, birlashtirmang yoki qisqartirmang. Natijadagi savollar soni manbadagi songa teng bo'lishi kerak. Bunday holatda 40-50 ta savol chegarasini QO'LLAMANG — manbada 63 ta yoki undan ko'p savol bo'lsa, barchasini qaytaring.
   B) Faqat matnda tayyor savollar ro'yxati bo'lmasa va matn uzluksiz darslik/nazariy material bo'lsa, shu material asosida ko'pi bilan 40-50 ta yangi savol yarating.
   C) Tayyor savollar ro'yxati mavjud bo'lsa, uni uzluksiz darslik deb noto'g'ri talqin qilmang. Savollar sonini o'zingizcha kamaytirmang.
3. {explanation_rule}
4. {option_rule}"""

    # Umumiy deadline navbatni kutishdan boshlanadi: queue + semaphore +
    # Gemini chaqiruvi + retry kutishlari bitta vaqt budjetiga kiradi.
    request_started = time.monotonic()
    deadline = request_started + AI_TOTAL_TIMEOUT

    # Navbat va parallel so'rovlar uchun kutish vaqtlarini alohida o'lchaymiz.
    queue_wait_started = request_started
    with _ai_monitor_lock:
        _ai_monitor_state["waiting_for_queue"] += 1
    queue_remaining = max(0.0, deadline - time.monotonic())
    queue_acquired = ai_queue_slots.acquire(timeout=queue_remaining)
    with _ai_monitor_lock:
        _ai_monitor_state["waiting_for_queue"] = max(0, _ai_monitor_state["waiting_for_queue"] - 1)
        if queue_acquired:
            _ai_monitor_state["accepted_requests"] += 1
        else:
            _ai_monitor_state["last_errors"].append({"time": time.time(), "message": "AI navbatida kutish muddati tugadi"})
            _ai_monitor_state["last_errors"] = _ai_monitor_state["last_errors"][-5:]
    if not queue_acquired:
        logging.error("AI testlar navbatida kutish muddati tugadi | umumiy_limit=%s soniya", AI_TOTAL_TIMEOUT)
        return None
    queue_wait_seconds = time.monotonic() - queue_wait_started
    _maybe_alert_ai_queue_load()
    if queue_wait_seconds >= 1.0:
        logging.info("AI NAVBATI | So'rov navbat joyini kutdi | kutish=%.2f soniya", queue_wait_seconds)

    try:
        total_keys = len(GOOGLE_API_KEYS)
        start_index = _next_generation_key_start(total_keys)

        models = []
        for model_name in (GEMINI_MODEL, GEMINI_FALLBACK_MODEL):
            if model_name and model_name not in models:
                models.append(model_name)

        last_error = None
        # Katta matnni har bir kalit/model urinishida qayta slice qilmaslik uchun
        # so'rov mazmunini navbatdan chiqqach bir marta tayyorlaymiz.
        request_contents = extracted_text[:MAX_TEXT_INPUT_CHARS]
        request_char_count = len(request_contents)

        # Har bir retry roundida avval PRIMARY modelning barcha keylari,
        # keyin FALLBACK modelning barcha keylari sinab ko'riladi.
        # Shu bilan 3.6 ishlayotgan paytda 2.5 ga bekorchi request yuborilmaydi.
        for retry_round in range(AI_RETRY_PER_KEY):
            for model_name in models:
                for offset in range(total_keys):
                    if time.monotonic() >= deadline:
                        logging.error("AI test yaratish uchun umumiy vaqt chegarasi tugadi (%s soniya)", AI_TOTAL_TIMEOUT)
                        return None

                    key_idx = (start_index + offset) % total_keys
                    api_key = GOOGLE_API_KEYS[key_idx].strip()
                    if not api_key:
                        continue

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None

                    semaphore_wait_started = time.monotonic()
                    if not gemini_semaphore.acquire(timeout=remaining):
                        last_error = "Gemini parallel so'rovlar chegarasida kutish muddati tugadi"
                        continue
                    semaphore_wait_seconds = time.monotonic() - semaphore_wait_started
                    if semaphore_wait_seconds >= 1.0:
                        logging.info("GEMINI PARALLELLIGI | Bo'sh so'rov o'rnini kutish yakunlandi | kutish=%.2f soniya", semaphore_wait_seconds)

                    started = time.monotonic()
                    with _ai_monitor_lock:
                        _ai_monitor_state["key_attempts"][key_idx] = _ai_monitor_state["key_attempts"].get(key_idx, 0) + 1
                    try:
                        # Har bir HTTP chaqiruv timeouti qolgan umumiy deadline'dan
                        # ham oshmasin; SDK timeout millisekundlarda qabul qilinadi.
                        remaining_for_api = deadline - time.monotonic()
                        if remaining_for_api <= 0:
                            last_error = "AI umumiy vaqt chegarasi Gemini chaqiruvidan oldin tugadi"
                            continue
                        request_timeout_ms = max(
                            1, int(min(AI_REQUEST_TIMEOUT, remaining_for_api) * 1000)
                        )
                        client = genai.Client(
                            api_key=api_key,
                            http_options=genai_types.HttpOptions(
                                timeout=request_timeout_ms
                            ),
                        )
                        response = client.models.generate_content(
                            model=model_name,
                            contents=request_contents,
                            config=genai_types.GenerateContentConfig(
                                system_instruction=system_instruction,
                                response_mime_type="application/json",
                                response_schema=response_schema,
                                max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
                                temperature=0.2,
                            ),
                        )
                        elapsed = time.monotonic() - started
                        finish_reason = "NOMA'LUM"
                        try:
                            candidates = getattr(response, "candidates", None) or []
                            if candidates:
                                finish_reason = str(getattr(candidates[0], "finish_reason", "NOMA'LUM"))
                            if "MAX_TOKENS" in finish_reason.upper():
                                logging.warning(
                                    "GEMINI JAVOBI TOKEN LIMITIGA YETDI | model=%s | limit=%s | sabab=%s. Savollar to'liq bo'lmasligi mumkin.",
                                    model_name, GEMINI_MAX_OUTPUT_TOKENS, finish_reason
                                )
                        except Exception:
                            pass
                        if elapsed > AI_REQUEST_TIMEOUT:
                            logging.warning(
                                "Gemini so'rovi odatdagidan uzoq davom etdi (%.1f soniya; ogohlantirish chegarasi %s soniya), lekin muvaffaqiyatli yakunlandi",
                                elapsed, AI_REQUEST_TIMEOUT
                            )

                        if response and response.text:
                            # Token statistikasi mavjud bo'lsa, ularni xavfsiz o'qiymiz.
                            usage = getattr(response, "usage_metadata", None)
                            prompt_tokens = getattr(usage, "prompt_token_count", None) or 0
                            output_tokens = getattr(usage, "candidates_token_count", None) or 0
                            # Gemini qaytargan jami tokenlar va kirish/chiqish yig'indisi
                            # alohida yoziladi: ular model hisobiga ko'ra farq qilishi mumkin.
                            total_tokens = getattr(usage, "total_token_count", None) or 0
                            input_output_tokens = prompt_tokens + output_tokens

                            # Savollar soni va JSON holati faqat log uchun tekshiriladi.
                            try:
                                parsed_for_log = json.loads(response.text)
                                questions_for_log = parsed_for_log.get("quizzes", [])
                                question_count = len(questions_for_log) if isinstance(questions_for_log, list) else 0
                                json_valid = True
                            except Exception:
                                question_count = 0
                                json_valid = False

                            logging.info(
                                "AI TEST MUVAFFAQIYATLI YARATILDI | model=%s | API kalit indeksi=%s | urinish=%s | "
                                "kiruvchi_matn_belgilari=%s | chiquvchi_matn_belgilari=%s | savollar_soni=%s | "
                                "kirish_tokenlari=%s | chiqish_tokenlari=%s | "
                                "kirish_va_chiqish_yig'indisi=%s | Gemini_jami_tokenlari=%s | "
                                "Gemini_API_vaqti=%.2f soniya | AI_navbatida_kutish=%.2f soniya | "
                                "parallel_so'rov_chegarasida_kutish=%.2f soniya | JSON_to'g'ri=%s | "
                                "izoh_rejimi=%s | umumiy_vaqt=%.2f soniya",
                                model_name, key_idx, retry_round + 1,
                                request_char_count, len(response.text),
                                question_count, prompt_tokens, output_tokens,
                                input_output_tokens, total_tokens,
                                elapsed, queue_wait_seconds, semaphore_wait_seconds,
                                json_valid,
                                "YOQILGAN" if GEMINI_INCLUDE_EXPLANATIONS else "O'CHIRILGAN",
                                time.monotonic() - queue_wait_started
                            )
                            with _ai_monitor_lock:
                                _ai_monitor_state["key_success"][key_idx] = _ai_monitor_state["key_success"].get(key_idx, 0) + 1
                            return response.text

                        last_error = f"{model_name}: Gemini bo'sh javob qaytardi"
                        with _ai_monitor_lock:
                            _ai_monitor_state["key_failures"][key_idx] = _ai_monitor_state["key_failures"].get(key_idx, 0) + 1
                            _ai_monitor_state["last_errors"].append({"time": time.time(), "message": f"{model_name}, Kalit #{key_idx + 1}: Gemini bo'sh javob qaytardi"})
                            _ai_monitor_state["last_errors"] = _ai_monitor_state["last_errors"][-5:]

                    except Exception as e:
                        safe_error = _safe_ai_error_text(e)
                        last_error = f"{type(e).__name__}: {safe_error}"
                        with _ai_monitor_lock:
                            _ai_monitor_state["key_failures"][key_idx] = _ai_monitor_state["key_failures"].get(key_idx, 0) + 1
                            _ai_monitor_state["last_errors"].append({"time": time.time(), "message": f"{model_name}, Kalit #{key_idx + 1}: {type(e).__name__}: {safe_error}"})
                            _ai_monitor_state["last_errors"] = _ai_monitor_state["last_errors"][-5:]
                        logging.warning(
                            "Gemini so'rovi bajarilmadi | model=%s | API_kalit_indeksi=%s | urinish=%s | xato_turi=%s | xato_tafsiloti=%s",
                            model_name, key_idx, retry_round + 1, type(e).__name__, safe_error
                        )
                    finally:
                        gemini_semaphore.release()

                    remaining_after_attempt = deadline - time.monotonic()
                    if remaining_after_attempt > 0:
                        time.sleep(min(5.0, 0.75 * (2 ** retry_round), remaining_after_attempt))

        logging.error(
            "Barcha AI model, kalit va qayta urinishlar muvaffaqiyatsiz yakunlandi. Oxirgi xato tafsiloti: %s",
            last_error
        )
        return None
    finally:
        ai_queue_slots.release()


# --- AI GENERATION OUTCOME STATISTICS (in-memory, since process start) ---
_ai_stats_lock = threading.Lock()
_ai_generation_stats = {
    "success": 0,
    "failed": 0,
    "total_seconds": 0.0,
    "last_success_at": 0.0,
    "last_failure_at": 0.0,
}


def generate_quiz_from_gemini(extracted_text):
    """Track each top-level generation attempt without changing generation logic."""
    started_at = time.monotonic()
    succeeded = False
    try:
        result = _generate_quiz_from_gemini_impl(extracted_text)
        succeeded = bool(result)
        return result
    except Exception:
        # Preserve the existing exception behavior while counting the failed attempt.
        raise
    finally:
        elapsed = max(0.0, time.monotonic() - started_at)
        with _ai_stats_lock:
            if succeeded:
                _ai_generation_stats["success"] += 1
                _ai_generation_stats["last_success_at"] = time.time()
            else:
                _ai_generation_stats["failed"] += 1
                _ai_generation_stats["last_failure_at"] = time.time()
            _ai_generation_stats["total_seconds"] += elapsed


@bot.message_handler(commands=["ai_monitor"])
def admin_ai_monitor_command(message):
    """Administrator uchun AI navbati, xatolar va API kalitlar statistikasini yuboradi."""
    if not _admin_only(message):
        return

    try:
        ai_slots_free = max(0, int(getattr(gemini_semaphore, "_value", 0)))
        queue_slots_free = max(0, int(getattr(ai_queue_slots, "_value", 0)))
        ai_slots_busy = max(0, AI_MAX_CONCURRENT - ai_slots_free)
        queue_slots_used = max(0, AI_MAX_QUEUE - queue_slots_free)

        with _ai_monitor_lock:
            waiting = _ai_monitor_state["waiting_for_queue"]
            accepted = _ai_monitor_state["accepted_requests"]
            key_attempts = dict(_ai_monitor_state["key_attempts"])
            key_success = dict(_ai_monitor_state["key_success"])
            key_failures = dict(_ai_monitor_state["key_failures"])
            recent_errors = list(_ai_monitor_state["last_errors"])

        with _ai_stats_lock:
            successful_generations = _ai_generation_stats["success"]
            failed_generations = _ai_generation_stats["failed"]

        report = (
            "🤖 <b>Quiz AI — AI navbati va API nazorati</b>\n"
            f"🕒 {datetime.now(timezone(timedelta(hours=5))).strftime('%d-%m-%Y %H:%M:%S')} (O‘zbekiston)\n\n"
            "⚙️ <b>Joriy yuklama</b>\n"
            f"🔄 Ishlayotgan AI so‘rovlari: <b>{ai_slots_busy}/{AI_MAX_CONCURRENT}</b>\n"
            f"⏳ Navbat slotlari band: <b>{queue_slots_used}/{AI_MAX_QUEUE}</b>\n"
            f"🕰 Navbat joyini kutayotgan so‘rovlar: <b>{waiting}</b>\n"
            f"🟢 Bo‘sh navbat slotlari: <b>{queue_slots_free}</b>\n\n"
            "📊 <b>Test yaratish natijalari (server ishga tushganidan beri)</b>\n"
            f"✅ Muvaffaqiyatli: <b>{successful_generations}</b>\n"
            f"❌ Muvaffaqiyatsiz: <b>{failed_generations}</b>\n"
            f"📥 Navbatga qabul qilingan jami so‘rovlar: <b>{accepted}</b>\n\n"
            "🔑 <b>API kalitlari statistikasi</b>\n"
            f"Jami sozlangan kalitlar: <b>{len(GOOGLE_API_KEYS)}</b>\n"
        )

        # Telegram bitta xabar uchun taxminan 4096 belgi cheklaydi.
        # Kalitlar ro'yxatini kichik bo'laklarga ajratib yuboramiz.
        messages = [report]
        if GOOGLE_API_KEYS:
            key_lines = []
            for key_idx in range(len(GOOGLE_API_KEYS)):
                attempts = key_attempts.get(key_idx, 0)
                successes = key_success.get(key_idx, 0)
                failures = key_failures.get(key_idx, 0)
                key_lines.append(
                    f"• Kalit #{key_idx + 1}: urinish <b>{attempts}</b>, "
                    f"muvaffaqiyat <b>{successes}</b>, xato <b>{failures}</b>"
                )
            chunk = "🔑 <b>API kalitlari (davomi bo‘lishi mumkin)</b>\n"
            for line in key_lines:
                addition = line + "\n"
                if len(chunk) + len(addition) > 3000:
                    messages.append(chunk.rstrip())
                    chunk = "🔑 <b>API kalitlari (davomi)</b>\n"
                chunk += addition
            if chunk.strip():
                messages.append(chunk.rstrip())
        else:
            messages.append("⚠️ AI test yaratish API kalitlari topilmadi.")

        if recent_errors:
            error_chunk = "⚠️ <b>Oxirgi AI xatolari</b>\n"
            for item in recent_errors[-3:]:
                error_time = datetime.fromtimestamp(
                    item["time"], timezone(timedelta(hours=5))
                ).strftime("%d-%m %H:%M:%S")
                safe_message = (str(item["message"]).replace("&", "&amp;")
                                .replace("<", "&lt;").replace(">", "&gt;"))
                line = f"• <code>{error_time}</code> — {safe_message[:180]}\n"
                if len(error_chunk) + len(line) > 3000:
                    messages.append(error_chunk.rstrip())
                    error_chunk = "⚠️ <b>Oxirgi AI xatolari (davomi)</b>\n"
                error_chunk += line
            messages.append(error_chunk.rstrip())
        else:
            messages.append("✅ Hozircha qayd etilgan AI API xatolari yo‘q.")

        messages.append(
            "ℹ️ Hisoblagichlar xotirada saqlanadi va server qayta ishga tushganda "
            "noldan boshlanadi. API kalitlarining maxfiy qiymatlari ko‘rsatilmaydi."
        )

        for part in messages:
            if part:
                bot.send_message(message.chat.id, part, parse_mode="HTML")
        logging.info("AI navbati va API nazorati hisoboti administratorga yuborildi.")
    except Exception as e:
        logging.exception("AI navbati va API nazorati hisobotini yuborishda xatolik: %s", e)
        try:
            bot.send_message(
                message.chat.id,
                "❌ AI nazorati hisobotini yuborishda xatolik yuz berdi. Batafsil ma’lumot Railway loglarida."
            )
        except Exception:
            pass



@bot.message_handler(commands=["ai_stats"])
def admin_ai_generation_stats_command(message):
    """Show generation success/failure counts since this process started."""
    if not _admin_only(message):
        return

    with _ai_stats_lock:
        success = _ai_generation_stats["success"]
        failed = _ai_generation_stats["failed"]
        total_seconds = _ai_generation_stats["total_seconds"]
        last_success_at = _ai_generation_stats["last_success_at"]
        last_failure_at = _ai_generation_stats["last_failure_at"]

    total = success + failed
    avg_seconds = total_seconds / total if total else 0.0
    success_rate = (success * 100.0 / total) if total else 0.0

    def _format_stat_time(timestamp):
        if not timestamp:
            return "Hali yo‘q"
        return datetime.fromtimestamp(
            timestamp, timezone(timedelta(hours=5))
        ).strftime("%d-%m-%Y %H:%M:%S")

    report = (
        "📊 <b>Quiz AI — AI test yaratish statistikasi</b>\n"
        f"🕒 {datetime.now(timezone(timedelta(hours=5))).strftime('%d-%m-%Y %H:%M:%S')} (O‘zbekiston)\n\n"
        "✅ <b>Muvaffaqiyatli:</b> " + f"<b>{success}</b>\n"
        "❌ <b>Muvaffaqiyatsiz:</b> " + f"<b>{failed}</b>\n"
        f"📦 Jami urinishlar: <b>{total}</b>\n"
        f"📈 Muvaffaqiyat foizi: <b>{success_rate:.1f}%</b>\n"
        f"⏱ O‘rtacha vaqt: <b>{avg_seconds:.1f} soniya</b>\n\n"
        f"🟢 Oxirgi muvaffaqiyat: <code>{_format_stat_time(last_success_at)}</code>\n"
        f"🔴 Oxirgi xato: <code>{_format_stat_time(last_failure_at)}</code>\n\n"
        "ℹ️ Hisob server qayta ishga tushguncha saqlanadi; bu tarixiy DB hisoboti emas."
    )
    try:
        bot.send_message(message.chat.id, report, parse_mode="HTML")
    except Exception as e:
        logging.exception("AI statistikasi yuborilmadi: %s", e)


@app.post("/api/contact-admin")
async def api_contact_admin(request: Request):
    """Start an admin-support conversation from the Mini App.

    The frontend calls this endpoint instead of relying on Telegram
    WebApp.sendData(), which is not reliable for every Mini App launch mode.
    The bot sends the support prompt directly to the user's private chat;
    the next text message is then forwarded to ADMIN_ID by the existing
    support handler.
    """
    try:
        data = await request.json()
        user_id = int(data.get("user_id") or 0)
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id required")
        if not ADMIN_ID:
            user_lang = get_user_lang(user_id)
            return JSONResponse(
                status_code=503,
                content={"status": "error", "message": MESSAGES[user_lang]["support_config_error"]},
            )

        add_user_to_db(user_id)
        user_lang = get_user_lang(user_id)
        support_waiting_users.add(user_id)
        bot.send_message(user_id, MESSAGES[user_lang]["support_prompt"])
        logging.info(f"Admin support boshlandi: user_id={user_id}, admin_id={ADMIN_ID}")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Admin bilan bog'lanish API xatosi: {e}")
        raise HTTPException(status_code=500, detail="contact_admin_failed")


@app.get("/api/heartbeat")
def user_heartbeat(user_id: int):
    add_user_to_db(user_id)
    update_user_activity(user_id)
    return {"status": "ok", "active_users": get_users_count()}


@app.get("/api/quizzes")
def get_user_quizzes(user_id: int):
    add_user_to_db(user_id)
    total_users = get_users_count()
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, total, answered, created_at, last_score, last_percent,"
        " is_public FROM quizzes WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    personal_rows = cursor.fetchall()
    cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    lang_row = cursor.fetchone()
    user_lang = lang_row["language"] if lang_row else "uz"
    conn.close()

    quizzes = [{
        "id": r["id"],
        "title": r["title"],
        "total": r["total"],
        "answered": r["answered"],
        "created_at": r["created_at"],
        "last_score": r["last_score"],
        "last_percent": r["last_percent"],
        "is_public": r["is_public"],
    } for r in personal_rows]
    return {
        "status": "ok",
        "quizzes": quizzes,
        "total_users": total_users,
        "user_lang": user_lang,
    }


@app.get("/api/public-quizzes")
def get_public_quizzes(user_id: int):
    add_user_to_db(user_id)
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, plan_key, premium_until, public_free_used, created_at FROM users WHERE user_id = ?",
        (user_id,),
    )
    u = cursor.fetchone()
    now = int(time.time())
    if u and now - (u["created_at"] or now) >= 30 * 24 * 3600 and not is_active_paid_status(u["status"] or "", u["premium_until"] or 0):
        cursor.execute(
            "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()
        public_free_used = 0
    else:
        public_free_used = (u["public_free_used"] or 0) if u else 0

    is_paid = bool(u and is_active_paid_status(u["status"] or "", u["premium_until"] or 0))
    public_remaining = free_limit_remaining(FREE_PUBLIC_LIMIT, public_free_used)

    cursor.execute("SELECT id, title, total, created_at FROM quizzes WHERE is_public = 1 ORDER BY created_at DESC LIMIT 50")
    rows = cursor.fetchall()
    conn.close()

    quizzes = [
        {
            "id": r["id"],
            "title": r["title"],
            "total": r["total"],
            "created_at": r["created_at"],
            "locked": (not is_paid and FREE_PUBLIC_LIMIT != -1 and public_remaining <= 0),
        }
        for r in rows
    ]
    return {
        "status": "ok",
        "quizzes": quizzes,
        "is_paid": is_paid,
        "public_free_used": public_free_used,
        "public_remaining": public_remaining,
        "public_limit": None if FREE_PUBLIC_LIMIT == -1 else FREE_PUBLIC_LIMIT,
        "public_unlimited": FREE_PUBLIC_LIMIT == -1,
    }


@app.get("/api/public-quiz-detail")
def get_public_quiz_detail(quiz_id: str, user_id: int):
    """Ommaviy testni faqat mavjud va ommaviy bo'lsa ochadi; limitni shundan keyin sarflaydi."""
    add_user_to_db(user_id)
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Avval test mavjudligi va ommaviyligi tekshiriladi. Noto'g'ri ID bepul
    # foydalanish hisoblagichini kamaytirmasligi kerak.
    cursor.execute(
        "SELECT title, quiz_json FROM quizzes WHERE id = ? AND is_public = 1",
        (quiz_id,),
    )
    quiz_row = cursor.fetchone()
    if not quiz_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Ommaviy test topilmadi")

    cursor.execute(
        "SELECT status, premium_until, public_free_used, created_at FROM users WHERE user_id = ?",
        (user_id,),
    )
    user_row = cursor.fetchone()

    now = int(time.time())
    is_paid = bool(
        user_row and is_active_paid_status(user_row["status"] or "", user_row["premium_until"] or 0)
    )

    # 30 kunlik bepul davr tugagan bo'lsa, hisoblagichlarni qayta boshlaymiz.
    if user_row and now - (user_row["created_at"] or now) >= 30 * 24 * 3600 and not is_paid:
        cursor.execute(
            "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()
        public_free_used = 0
    else:
        public_free_used = (user_row["public_free_used"] or 0) if user_row else 0

    # Premium / Teacher foydalanuvchilari cheksiz foydalanadi.
    if not is_paid and FREE_PUBLIC_LIMIT != -1:
        cursor.execute(
            "UPDATE users SET public_free_used = COALESCE(public_free_used, 0) + 1 "
            "WHERE user_id = ? AND COALESCE(public_free_used, 0) < ?",
            (user_id, FREE_PUBLIC_LIMIT),
        )
        if cursor.rowcount != 1:
            conn.close()
            lang = get_user_lang(user_id)
            messages = {
                "uz": MESSAGES["uz"]["public_limit_reached"],
                "ru": MESSAGES["ru"]["public_limit_reached"],
                "en": MESSAGES["en"]["public_limit_reached"],
            }
            return {
                "status": "error",
                "error_code": "public_limit",
                "message": messages.get(lang, messages["uz"]),
            }
        conn.commit()

    conn.close()
    if not is_paid:
        notify_free_limit_reached(user_id, "public")

    return {
        "status": "ok",
        "title": quiz_row["title"] or "Test",
        "quiz_json": json.loads(quiz_row["quiz_json"]),
    }


@app.post("/api/toggle-public")
def toggle_public(quiz_id: str, user_id: int, is_public: int):
    """Faqat test egasi uning ommaviy holatini 0/1 qiymatiga o'zgartira oladi."""
    if is_public not in (0, 1):
        raise HTTPException(status_code=400, detail="Ommaviylik qiymati 0 yoki 1 bo'lishi kerak.")
    conn = _connect_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE quizzes SET is_public = ? WHERE id = ? AND user_id = ?",
            (is_public, quiz_id, user_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Test topilmadi.")
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/api/set-language")
def set_language(user_id: int, lang: str):
    conn = _connect_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id)
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


# --- O'QITUVCHI PROFESSIONAL VOSITALARI ---
TEACHER_VARIANT_CODES = ("A", "B", "C", "D")


def _load_quiz_items(quiz_id: str, owner_id: int):
    def _read(conn):
        cur = conn.cursor()
        cur.execute("SELECT id, title, quiz_json, total FROM quizzes WHERE id = ? AND user_id = ?", (quiz_id, owner_id))
        return cur.fetchone()
    row = teacher_db_read(_read)
    if not row:
        raise HTTPException(status_code=404, detail=teacher_text(owner_id, "quiz_not_found"))
    try:
        data = json.loads(row["quiz_json"])
        items = data.get("quizzes", [])
    except Exception:
        raise HTTPException(status_code=500, detail=teacher_text(owner_id, "quiz_data_broken"))
    if not items:
        raise HTTPException(status_code=400, detail=teacher_text(owner_id, "quiz_questions_missing"))
    return row, items

def _make_variant(items, variant_code: str):
    """Variantlar bir-biridan farqli bo'lishi uchun savollar va variantlar deterministik aralashtiriladi."""
    code_index = TEACHER_VARIANT_CODES.index(variant_code)
    result = deepcopy(items)
    # Har bir variant uchun turlicha, lekin takrorlanadigan seed.
    rng = random.Random(f"QuizPilot:{variant_code}:{len(items)}:{json.dumps(items, ensure_ascii=False, sort_keys=True)}")
    # Variant B/C/D savollar tartibini ham o'zgartiradi; A ham originaldan ko'chirma emas.
    if code_index:
        rng.shuffle(result)
    elif len(result) > 1:
        # A variantda ham testni biroz qayta tartiblaymiz, ammo keyin javob kaliti aniq saqlanadi.
        rng.shuffle(result)
    for item in result:
        options = list(item.get("options") or [])[:4]
        correct = int(item.get("correct_index", 0))
        pairs = list(enumerate(options))
        # Har variantda variantlar tartibi ham aralashadi.
        rng.shuffle(pairs)
        new_options = [p[1] for p in pairs]
        new_correct = next((i for i, p in enumerate(pairs) if p[0] == correct), 0)
        item["options"] = new_options
        item["correct_index"] = new_correct
    return {"variant": variant_code, "quizzes": result}


class TeacherVariantsRequest(BaseModel):
    user_id: int = Field(gt=0)
    quiz_id: str = Field(min_length=1, max_length=128)
    variants: List[str] = Field(default_factory=lambda: ["A", "B", "C", "D"], min_length=1, max_length=4)


@app.post("/api/teacher/generate-variants")
def teacher_generate_variants(req: TeacherVariantsRequest):
    require_teacher(req.user_id)
    row, items = _load_quiz_items(req.quiz_id, req.user_id)
    requested = []
    for code in req.variants:
        code = str(code).strip().upper()
        if code in TEACHER_VARIANT_CODES and code not in requested:
            requested.append(code)
    if not requested:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "select_quiz_variant"))

    conn = teacher_db_connect()
    cur = conn.cursor()
    now = int(time.time())
    result = []
    for code in requested:
        variant = _make_variant(items, code)
        payload_json = json.dumps(variant, ensure_ascii=False)
        cur.execute("SELECT id FROM teacher_variants WHERE quiz_id=? AND variant_code=? ORDER BY id LIMIT 1", (req.quiz_id, code))
        existing_variant = cur.fetchone()
        if existing_variant:
            cur.execute("UPDATE teacher_variants SET variant_json=?, created_at=? WHERE id=?", (payload_json, now, existing_variant[0]))
        else:
            cur.execute("INSERT INTO teacher_variants (quiz_id, variant_code, variant_json, created_at) VALUES (?, ?, ?, ?)", (req.quiz_id, code, payload_json, now))
        result.append({"variant": code, "question_count": len(variant["quizzes"])})
    conn.commit()
    conn.close()
    return {"status": "ok", "quiz_id": req.quiz_id, "title": row["title"], "variants": result}


@app.get("/api/teacher/variants")
def teacher_list_variants(user_id: int, quiz_id: str):
    require_teacher(user_id)
    _load_quiz_items(quiz_id, user_id)
    def _read(conn):
        cur = conn.cursor()
        cur.execute("SELECT variant_code, created_at FROM teacher_variants WHERE quiz_id=? ORDER BY variant_code", (quiz_id,))
        return cur.fetchall()
    rows = teacher_db_read(_read)
    return {"status": "ok", "variants": [dict(r) for r in rows]}

def _get_teacher_variant(quiz_id: str, owner_id: int, variant_code: str):
    _load_quiz_items(quiz_id, owner_id)
    code = (variant_code or "A").strip().upper()
    if code not in TEACHER_VARIANT_CODES:
        raise HTTPException(status_code=400, detail=teacher_text(owner_id, "variant_required"))
    conn = teacher_db_connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT variant_json FROM teacher_variants WHERE quiz_id=? AND variant_code=?", (quiz_id, code))
    row = cur.fetchone()
    conn.close()
    if row:
        return json.loads(row["variant_json"])
    # Variant hali yaratilmagan bo'lsa, uni xavfsiz tarzda yaratib olamiz.
    _, items = _load_quiz_items(quiz_id, owner_id)
    variant = _make_variant(items, code)
    conn = teacher_db_connect()
    conn.execute("INSERT OR REPLACE INTO teacher_variants (quiz_id,variant_code,variant_json,created_at) VALUES (?,?,?,?)",
                 (quiz_id, code, json.dumps(variant, ensure_ascii=False), int(time.time())))
    conn.commit(); conn.close()
    return variant


class TeacherGroupCreateRequest(BaseModel):
    user_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class TeacherGroupJoinRequest(BaseModel):
    group_id: str = Field(default="", max_length=128)
    join_code: str = Field(default="", max_length=12)
    user_id: int = Field(gt=0)
    first_name: str = Field(default="", max_length=100)
    username: str = Field(default="", max_length=64)


def _new_group_code(conn, length=6):
    "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        row = conn.execute("SELECT 1 FROM teacher_groups WHERE join_code=? LIMIT 1", (code,)).fetchone()
        if not row:
            return code
    raise HTTPException(status_code=500, detail="__TEACHER_SERVER_ERROR__")


@app.post("/api/teacher/groups")
def teacher_create_group(req: TeacherGroupCreateRequest):
    require_teacher(req.user_id)
    name = (req.name or "").strip()[:100]
    if not name:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "group_name_required"))
    gid = f"tg_{uuid.uuid4().hex[:10]}"
    now = int(time.time())
    description = (req.description or "")[:500]
    def _write(conn):
        code = _new_group_code(conn)

        # Eski Railway bazalarida teacher_groups PRIMARY KEY ustuni `id` emas,
        # boshqa nomda qolgan bo'lishi mumkin. Hybrid V2 trigger aynan haqiqiy
        # PRIMARY KEY qiymatini outbox'ga yozadi; shuning uchun PK bo'sh qolsa
        # NOT NULL xatosi chiqadi. Joriy va eski sxemalarni buzmasdan bir xil
        # `gid` qiymatini haqiqiy PK ustuniga ham beramiz.
        table_info = conn.execute("PRAGMA table_info(teacher_groups)").fetchall()
        pk_columns = [row[1] for row in table_info if row[5]]
        pk_column = pk_columns[0] if len(pk_columns) == 1 else "id"

        columns = ["id", "owner_id", "name", "description", "join_code", "created_at", "active"]
        values = [gid, req.user_id, name, description, code, now, 1]
        if pk_column not in columns:
            columns.insert(1, pk_column)
            values.insert(1, gid)

        placeholders = ",".join("?" for _ in columns)
        quoted_columns = ",".join('"{}"'.format(col.replace('"', '""')) for col in columns)
        conn.execute(
            "INSERT INTO teacher_groups ({}) VALUES ({})".format(quoted_columns, placeholders),
            values,
        )
        return code
    code = teacher_db_write(_write)
    return {"status":"ok", "group_id":gid, "name":name, "join_code":code}

@app.get("/api/teacher/groups")
def teacher_groups(user_id: int):
    require_teacher(user_id)
    def _read(conn):
        cur = conn.cursor()
        cur.execute(
            "SELECT g.id,g.name,g.description,g.join_code,g.created_at,g.active,COUNT(m.id) AS member_count "
            "FROM teacher_groups g LEFT JOIN teacher_group_members m ON m.group_id=g.id "
            "WHERE g.owner_id=? AND g.active=1 GROUP BY g.id ORDER BY g.created_at DESC",
            (user_id,),
        )
        return cur.fetchall()
    rows = teacher_db_read(_read)
    return {"status":"ok", "groups":[dict(r) for r in rows]}

@app.post("/api/teacher/groups/join")
def teacher_join_group(req: TeacherGroupJoinRequest):
    code = (req.join_code or "").strip().upper().replace(" ", "")
    group_id = (req.group_id or "").strip()
    def _write(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Guruhga qo'shilish faqat maxsus join_code orqali amalga oshadi.
        # Ichki group_id ni bilishning o'zi guruhga kirish huquqini bermasligi kerak.
        if not code:
            raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "group_code_required"))
        cur.execute("SELECT id,name,active FROM teacher_groups WHERE join_code=?", (code,))
        group = cur.fetchone()
        if not group or not group["active"]:
            raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "group_not_found"))
        now = int(time.time())
        cur.execute(
            "INSERT OR IGNORE INTO teacher_group_members (group_id,user_id,first_name,username,joined_at) VALUES (?,?,?,?,?)",
            (group["id"], req.user_id, (req.first_name or "")[:100], (req.username or "")[:100], now),
        )
        return dict(group)
    group = teacher_db_write(_write)
    return {"status":"ok", "group_id":group["id"], "group_name":group["name"]}

@app.get("/api/teacher/my-groups")
def teacher_my_groups(user_id: int):
    def _read(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT g.id,g.name,g.description,g.join_code,g.created_at,m.joined_at "
            "FROM teacher_group_members m JOIN teacher_groups g ON g.id=m.group_id "
            "WHERE m.user_id=? AND g.active=1 ORDER BY m.joined_at DESC",
            (user_id,),
        )
        return cur.fetchall()
    rows = teacher_db_read(_read)
    return {"status":"ok", "groups":[dict(r) for r in rows]}

@app.get("/api/teacher/group-members")
def teacher_group_members(group_id: str, user_id: int):
    require_teacher(user_id)
    def _read(conn):
        conn.row_factory = sqlite3.Row
        cur=conn.cursor()
        cur.execute("SELECT g.id,g.name FROM teacher_groups g WHERE g.id=? AND g.owner_id=?", (group_id,user_id))
        g=cur.fetchone()
        if not g:
            raise HTTPException(status_code=404, detail=teacher_text(user_id, "group_not_found"))
        cur.execute("SELECT user_id,first_name,username,joined_at FROM teacher_group_members WHERE group_id=? ORDER BY joined_at", (group_id,))
        return dict(g), cur.fetchall()
    g, rows = teacher_db_read(_read)
    return {"status":"ok", "group":g, "members":[dict(r) for r in rows]}

@app.delete("/api/teacher/groups/{group_id}")
def teacher_delete_group(group_id: str, user_id: int):
    require_teacher(user_id)
    def _write(conn):
        cur=conn.cursor()
        cur.execute("UPDATE teacher_groups SET active=0 WHERE id=? AND owner_id=?", (group_id,user_id))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail=teacher_text(user_id, "group_not_found"))
    teacher_db_write(_write)
    return {"status":"ok"}

class TeacherAssignmentCreateRequest(BaseModel):
    user_id: int = Field(gt=0)
    group_id: str = Field(min_length=1, max_length=128)
    quiz_id: str = Field(min_length=1, max_length=128)
    variant_code: str = Field(default="", max_length=1)
    title: str = Field(default="", max_length=150)
    due_at: int = Field(default=0, ge=0)
    duration_minutes: int = Field(default=30, ge=5, le=180)


@app.post("/api/teacher/assignments")
def teacher_create_assignment(req: TeacherAssignmentCreateRequest):
    require_teacher(req.user_id)
    group_id=(req.group_id or "").strip(); quiz_id=(req.quiz_id or "").strip()
    if not group_id or not quiz_id:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "group_required"))
    code=(req.variant_code or "").strip().upper()
    duration = max(5, min(int(req.duration_minutes or 30), 180))
    if code and code not in TEACHER_VARIANT_CODES:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "variant_required"))
    if code:
        _get_teacher_variant(quiz_id,req.user_id,code)
    def _write(conn):
        conn.row_factory = sqlite3.Row
        cur=conn.cursor()
        cur.execute("SELECT id,name FROM teacher_groups WHERE id=? AND owner_id=? AND active=1",(group_id,req.user_id)); group=cur.fetchone()
        cur.execute("SELECT id,title FROM quizzes WHERE id=? AND user_id=?",(quiz_id,req.user_id)); quiz=cur.fetchone()
        if not group:
            raise HTTPException(status_code=404,detail=teacher_text(req.user_id, "group_not_found"))
        if not quiz:
            raise HTTPException(status_code=404,detail=teacher_text(req.user_id, "quiz_not_found"))
        aid=f"ta_{uuid.uuid4().hex[:10]}"; now=int(time.time()); title=(req.title or "").strip()[:150] or quiz["title"]; due_at=max(0,int(req.due_at or 0))
        cur.execute("INSERT INTO teacher_assignments (id,owner_id,group_id,quiz_id,variant_code,title,due_at,duration_minutes,created_at,active) VALUES (?,?,?,?,?,?,?,?,?,1)",(aid,req.user_id,group_id,quiz_id,code,title,due_at,duration,now))
        return {"id":aid,"group_id":group_id,"quiz_id":quiz_id,"variant_code":code,"title":title,"due_at":due_at,"duration_minutes":duration,"created_at":now,"active":True,"group_name":group["name"],"quiz_title":quiz["title"]}
    assignment = teacher_db_write(_write)
    return {"status":"ok","assignment":assignment}

@app.get("/api/teacher/assignments")
def teacher_assignments(user_id: int):
    require_teacher(user_id)
    def _read(conn):
        conn.row_factory=sqlite3.Row; cur=conn.cursor()
        cur.execute("""SELECT a.id,a.group_id,a.quiz_id,a.variant_code,a.title,a.due_at,COALESCE(a.duration_minutes,30) AS duration_minutes,a.created_at,a.active,
                             g.name AS group_name,q.title AS quiz_title
                      FROM teacher_assignments a LEFT JOIN teacher_groups g ON g.id=a.group_id
                      LEFT JOIN quizzes q ON q.id=a.quiz_id WHERE a.owner_id=? ORDER BY a.created_at DESC LIMIT 100""",(user_id,))
        return cur.fetchall()
    rows=teacher_db_read(_read)
    return {"status":"ok","assignments":[dict(r) for r in rows]}

@app.delete("/api/teacher/assignments/{assignment_id}")
def teacher_delete_assignment(assignment_id: str, user_id: int):
    require_teacher(user_id)
    def _write(conn):
        cur=conn.cursor()
        cur.execute("UPDATE teacher_assignments SET active=0 WHERE id=? AND owner_id=?",(assignment_id,user_id))
        if cur.rowcount!=1:
            raise HTTPException(status_code=404,detail=teacher_text(user_id, "assignment_not_found"))
    teacher_db_write(_write)
    return {"status":"ok"}

@app.get("/api/teacher/export-assignment")
def teacher_export_assignment(assignment_id: str, user_id: int, format: str):
    require_teacher(user_id)
    conn=teacher_db_connect(); conn.row_factory=sqlite3.Row; cur=conn.cursor()
    cur.execute("""SELECT a.*,g.name AS group_name,q.title AS quiz_title FROM teacher_assignments a
                   LEFT JOIN teacher_groups g ON g.id=a.group_id LEFT JOIN quizzes q ON q.id=a.quiz_id
                   WHERE a.id=? AND a.owner_id=?""",(assignment_id,user_id))
    a=cur.fetchone(); conn.close()
    if not a: raise HTTPException(status_code=404,detail=teacher_text(user_id, "assignment_not_found"))
    return teacher_export_quiz(a["quiz_id"],user_id,format,a["variant_code"] or "A",0)


@app.get("/api/teacher/export-quiz")
def teacher_export_quiz(quiz_id: str, user_id: int, format: str, variant: str = "A", all_variants: int = 0):
    require_teacher(user_id)
    row, items = _load_quiz_items(quiz_id, user_id)
    fmt = format.lower().strip()
    if fmt not in {"pdf", "docx"}:
        raise HTTPException(status_code=400, detail="Faqat PDF va Word formatlari qo‘llab-quvvatlanadi.")
    codes = list(TEACHER_VARIANT_CODES) if int(all_variants) else [variant.upper()]
    for c in codes:
        if c not in TEACHER_VARIANT_CODES: raise HTTPException(status_code=400, detail=teacher_text(user_id, "variant_required"))

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", row["title"] or "quiz")[:40]
    variant_payloads = [(c, _get_teacher_variant(quiz_id, user_id, c)) for c in codes]

    def answer_key(payload):
        return [chr(65 + int(q.get("correct_index", 0))) for q in payload.get("quizzes", [])]

    if fmt == "docx":
        path=os.path.join(DOWNLOADS_DIR, f"{safe}_teacher_{'_'.join(codes)}.docx")
        doc=Document(); doc.add_heading(row["title"], level=1); doc.add_paragraph("Test kaliti bilan | Variantlar: " + ", ".join(codes))
        for code,payload in variant_payloads:
            doc.add_heading(f"Variant {code}", level=2)
            for i,q in enumerate(payload.get("quizzes",[]),1):
                doc.add_paragraph(f"{i}. {q.get('question','')}")
                opts=(q.get("options") or [])+['','','','']
                for j in range(4): doc.add_paragraph(f"{chr(65+j)}) {opts[j]}", style=None)
            doc.add_paragraph("Javoblar: " + ", ".join(f"{i+1}-{a}" for i,a in enumerate(answer_key(payload))))
        doc.save(path)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    if fmt == "pdf":
        path=os.path.join(DOWNLOADS_DIR, f"{safe}_teacher_{'_'.join(codes)}.pdf")
        reg,bld=_register_pdf_font(); font="QuizPilotFont" if reg else "Helvetica"; bold="QuizPilotFontBold" if bld else font
        doc=SimpleDocTemplate(path,pagesize=A4,rightMargin=30,leftMargin=30,topMargin=30,bottomMargin=30)
        styles=getSampleStyleSheet(); title_style=ParagraphStyle("t_title",parent=styles["Title"],fontName=bold,fontSize=16,alignment=TA_CENTER); q_style=ParagraphStyle("t_q",parent=styles["BodyText"],fontName=font,fontSize=9,leading=12)

        # Faqat Teacher PDF eksporti uchun matnni ReportLab va Helvetica bilan moslaymiz.
        # Uzbekcha U+02BB/U+02BC belgilarini WinAnsi shriftida mavjud apostroflarga
        # almashtiramiz; aks holda ular qora kvadrat bo'lib ko'rinishi mumkin.
        # AI savollaridagi &, <, > belgilarini XML/HTML tegi deb talqin qilmasligi uchun escape qilamiz.
        def _teacher_pdf_safe(value):
            text = str(value or "")
            text = text.replace("\u02bb", "\u2018").replace("\u02bc", "\u2019").replace("`", "\u2018")
            # Helvetica fallbackida subscript raqamlar (masalan, I₀ va U₀)
            # ba'zi serverlarda qora kvadrat bo'lib chiqadi. Faqat Teacher PDF
            # eksportida ularni oddiy raqamlarga aylantiramiz.
            text = text.translate(str.maketrans({
                "\u2080": "0", "\u2081": "1", "\u2082": "2",
                "\u2083": "3", "\u2084": "4", "\u2085": "5",
                "\u2086": "6", "\u2087": "7", "\u2088": "8",
                "\u2089": "9",
            }))
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        story=[Paragraph(_teacher_pdf_safe(row["title"]),title_style),Spacer(1,8),Paragraph("Test kaliti bilan | Variantlar: " + ", ".join(codes),q_style),Spacer(1,10)]
        for code,payload in variant_payloads:
            story.append(Paragraph(f"<b>Variant {code}</b>", q_style)); story.append(Spacer(1,5))
            for i,q in enumerate(payload.get("quizzes",[]),1):
                story.append(Paragraph(f"{i}. {_teacher_pdf_safe(q.get('question',''))}",q_style))
                opts=(q.get("options") or [])+['','','','']
                for j in range(4): story.append(Paragraph(f"{chr(65+j)}) {_teacher_pdf_safe(opts[j])}",q_style))
                story.append(Spacer(1,4))
            story.append(Paragraph("<b>Javoblar:</b> " + ", ".join(f"{i+1}-{a}" for i,a in enumerate(answer_key(payload))),q_style)); story.append(Spacer(1,10))
        doc.build(story)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/pdf")
    raise HTTPException(status_code=400, detail=teacher_text(user_id, "format_invalid"))


# --- O'QITUVCHI GURUH REJIMI ---
# --- Teacher DB: barqaror ulanish va SQLite lock himoyasi ---
def _ensure_teacher_schema(conn):
    """Teacher jadvallarini Railway'dagi eski SQLite bazalari bilan ham moslaydi.

    Oldingi Teacher versiyalarida jadvallar qisman yaratilgan bo'lishi mumkin edi.
    CREATE TABLE IF NOT EXISTS bunday jadvalni yangilamaydi, shuning uchun bu yerda
    kerakli ustunlar birma-bir tekshiriladi va yetishmaganlari xavfsiz qo'shiladi.
    Bu faqat teacher_* jadvallariga taalluqli.
    """
    table_defs = {
        "teacher_variants": {
            "id": "INTEGER",
            "quiz_id": "TEXT",
            "variant_code": "TEXT",
            "variant_json": "TEXT",
            "created_at": "INTEGER",
        },
        "teacher_groups": {
            "id": "TEXT",
            "owner_id": "INTEGER",
            "name": "TEXT",
            "description": "TEXT",
            "created_at": "INTEGER",
            "active": "INTEGER",
        },
        "teacher_group_members": {
            "id": "INTEGER",
            "group_id": "TEXT",
            "user_id": "INTEGER",
            "first_name": "TEXT",
            "username": "TEXT",
            "joined_at": "INTEGER",
        },
        "teacher_assignments": {
            "id": "TEXT",
            "owner_id": "INTEGER",
            "group_id": "TEXT",
            "quiz_id": "TEXT",
            "variant_code": "TEXT",
            "title": "TEXT",
            "due_at": "INTEGER",
            "duration_minutes": "INTEGER",
            "created_at": "INTEGER",
            "active": "INTEGER",
        },
        "teacher_sessions": {
            "id": "TEXT",
            "owner_id": "INTEGER",
            "teacher_id": "INTEGER",
            "quiz_id": "TEXT",
            "code": "TEXT",
            "title": "TEXT",
            "duration_minutes": "INTEGER",
            "created_at": "INTEGER",
            "expires_at": "INTEGER",
            "active": "INTEGER",
            "group_id": "TEXT",
            "variant_code": "TEXT",
            "deleted": "INTEGER",
            "source_type": "TEXT",
            "assignment_id": "TEXT",
        },
        "teacher_participants": {
            "id": "INTEGER",
            "session_id": "TEXT",
            "user_id": "INTEGER",
            "first_name": "TEXT",
            "username": "TEXT",
            "score": "INTEGER",
            "total": "INTEGER",
            "percent": "INTEGER",
            "started_at": "INTEGER",
            "finished_at": "INTEGER",
        },
    }

    # Avval jadvalning o'zi mavjudligini kafolatlaymiz.
    create_sql = {
        "teacher_variants": """CREATE TABLE IF NOT EXISTS teacher_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id TEXT NOT NULL, variant_code TEXT NOT NULL,
            variant_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            UNIQUE(quiz_id, variant_code))""",
        "teacher_groups": """CREATE TABLE IF NOT EXISTS teacher_groups (
            id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL,
            description TEXT DEFAULT '', created_at INTEGER NOT NULL,
            active INTEGER DEFAULT 1)""",
        "teacher_group_members": """CREATE TABLE IF NOT EXISTS teacher_group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
            user_id INTEGER NOT NULL, first_name TEXT DEFAULT '',
            username TEXT DEFAULT '', joined_at INTEGER NOT NULL,
            UNIQUE(group_id, user_id))""",
        "teacher_assignments": """CREATE TABLE IF NOT EXISTS teacher_assignments (
            id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, group_id TEXT NOT NULL,
            quiz_id TEXT NOT NULL, variant_code TEXT DEFAULT '', title TEXT DEFAULT '',
            due_at INTEGER DEFAULT 0, duration_minutes INTEGER DEFAULT 30, created_at INTEGER NOT NULL,
            active INTEGER DEFAULT 1)""",
        "teacher_sessions": """CREATE TABLE IF NOT EXISTS teacher_sessions (
            id TEXT PRIMARY KEY, owner_id INTEGER, teacher_id INTEGER, quiz_id TEXT, code TEXT UNIQUE,
            title TEXT DEFAULT '', duration_minutes INTEGER DEFAULT 30, created_at INTEGER,
            expires_at INTEGER, active INTEGER DEFAULT 1,
            group_id TEXT DEFAULT '', variant_code TEXT DEFAULT '', deleted INTEGER DEFAULT 0,
            source_type TEXT DEFAULT 'group_test', assignment_id TEXT DEFAULT '')""",
        "teacher_participants": """CREATE TABLE IF NOT EXISTS teacher_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, user_id INTEGER,
            first_name TEXT, username TEXT, score INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0, percent INTEGER DEFAULT 0,
            started_at INTEGER, finished_at INTEGER,
            UNIQUE(session_id, user_id))""",
    }
    for name, ddl in create_sql.items():
        conn.execute(ddl)

    # Eski jadvallarga yetishmayotgan ustunlarni qo'shamiz.
    for table, columns in table_defs.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, typ in columns.items():
            if col in existing:
                continue
            try:
                # ALTER TABLE ADD COLUMN uchun xavfsiz oddiy tip.
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                logging.info("Teacher DB migration: %s.%s qo'shildi", table, col)
            except sqlite3.OperationalError as e:
                # Bir nechta Railway worker bir paytda migration qilsa, keyingi
                # worker ustun allaqachon qo'shilgan bo'lishi mumkin.
                logging.warning("Teacher DB migration %s.%s: %s", table, col, e)

    # ID ustuni eski bazada mavjud bo'lmagan bo'lsa, qo'shilgan qiymatlarni
    # mavjud rowlar uchun ham to'ldiramiz. Yangi yozuvlar endpointlar tomonidan
    # o'z ID'sini beradi.
    id_prefixes = {
        "teacher_groups": "tg_migrated_",
        "teacher_assignments": "ta_migrated_",
        "teacher_sessions": "ts_migrated_",
    }
    for table, prefix in id_prefixes.items():
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "id" in cols:
                conn.execute(
                    f"UPDATE {table} SET id=? || rowid WHERE id IS NULL OR TRIM(CAST(id AS TEXT))=''",
                    (prefix,),
                )
        except sqlite3.OperationalError as e:
            logging.warning("Teacher DB ID repair %s: %s", table, e)

    # Eski Railway bazasida teacher_sessions.teacher_id mavjud bo'lsa,
    # owner_id bilan bir xil qiymatga to'ldiramiz. Bu yangi endpointlar va
    # eski sessiyalarni bir xil sxemada ishlashini ta'minlaydi.
    try:
        session_cols = {r[1] for r in conn.execute("PRAGMA table_info(teacher_sessions)").fetchall()}
        if "teacher_id" in session_cols and "owner_id" in session_cols:
            conn.execute("UPDATE teacher_sessions SET teacher_id=owner_id WHERE teacher_id IS NULL")
    except sqlite3.OperationalError as e:
        logging.warning("Teacher DB teacher_id repair: %s", e)

    # NULL qiymatlar endpointlar ishlashiga xalaqit bermasin.
    null_defaults = {
        "teacher_groups": {"description": "", "active": 1},
        "teacher_assignments": {"variant_code": "", "title": "", "due_at": 0, "active": 1},
        "teacher_sessions": {"group_id": "", "variant_code": "", "active": 1, "deleted": 0},
        "teacher_participants": {"first_name": "", "username": "", "score": 0, "total": 0, "percent": 0, "started_at": 0, "finished_at": 0},
    }
    for table, values in null_defaults.items():
        for col, value in values.items():
            try:
                conn.execute(f"UPDATE {table} SET {col}=? WHERE {col} IS NULL", (value,))
            except sqlite3.OperationalError as e:
                logging.warning("Teacher DB default repair %s.%s: %s", table, col, e)

    conn.commit()


_teacher_schema_checked = False

def teacher_db_connect():
    """Teacher uchun barqaror SQLite ulanishi.

    Muhim: har bir so'rovda PRAGMA journal_mode=WAL ni qayta yoqish SQLite
    faylini qisqa muddatga lock qilib qo'yishi mumkin. Shu sabab WAL/synchronous
    rejimi init_db() vaqtida o'rnatiladi, bu yerda esa faqat busy_timeout ishlatiladi.
    """
    global _teacher_schema_checked
    conn = _connect_db(timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if not _teacher_schema_checked:
            _ensure_teacher_schema(conn)
            conn.commit()
            _teacher_schema_checked = True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise
    return conn

def teacher_db_read(work, attempts=6):
    """Teacher o'qish so'rovlarini SQLite lock bo'lsa avtomatik qayta urinadi."""
    last_error = None
    for attempt in range(attempts):
        conn = None
        try:
            conn = teacher_db_connect()
            result = work(conn)
            conn.close()
            return result
        except sqlite3.OperationalError as e:
            last_error = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(0.25 * (attempt + 1))
                continue
            raise
        except Exception:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            raise
    raise last_error or sqlite3.OperationalError("SQLite database is busy")

def teacher_db_write(work, attempts=6):
    last_error = None
    for attempt in range(attempts):
        conn = teacher_db_connect()
        try:
            result = work(conn)
            conn.commit()
            conn.close()
            return result
        except sqlite3.OperationalError as e:
            last_error = e
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(0.35 * (attempt + 1))
                continue
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
            raise
    raise last_error or sqlite3.OperationalError("SQLite database is busy")

TEACHER_MESSAGES = {
    "uz": {
        "quiz_data_broken": "Test ma’lumotlari buzilgan.",
        "quiz_questions_missing": "Test savollari mavjud emas.",
        "variant_required": "A, B, C yoki D variantidan foydalaning.",
        "group_required": "Guruh va testni tanlang.",
        "select_group": "Avval guruhni tanlang.",
        "assignment_not_found": "Topshiriq topilmadi.",
        "session_expired": "Sessiya muddati tugagan.",
        "format_invalid": "Fayl formati noto‘g‘ri.",

        "teacher_plan_required": "O‘qituvchi tarifini faollashtiring.",
        "group_name_required": "Guruh nomini kiriting.",
        "group_created": "Guruh yaratildi.",
        "group_not_found": "Guruh topilmadi.",
        "groups_empty": "Hozircha guruhlar yo‘q.",
        "members_count": "a’zo",
        "active": "Faol", "closed": "Yopiq",
        "delete": "O‘chirish",
        "assignment_created": "Topshiriq guruhga biriktirildi.",
        "assignment_empty": "Hozircha topshiriqlar yo‘q.",
        "select_group_quiz": "Guruh va testni tanlang.",
        "variant_invalid": "A, B, C yoki D variantidan foydalaning.",
        "variants_ready": "Variantlar tayyorlandi. Har bir variant uchun test kaliti saqlandi.",
        "select_quiz_variant": "Test va kamida bitta variantni tanlang.",
        "session_created": "Sessiya yaratildi.",
        "no_results": "Hali natijalar yo‘q.",
        "no_students_results": "Hali o‘quvchilar natijasi yo‘q.",
        "server_error": "Serverda ichki xatolik yuz berdi. Iltimos, qayta urinib ko‘ring.",
        "db_busy": "Server ma’lumotlar bazasi band. Bir necha soniyadan so‘ng qayta urinib ko‘ring.",
        "quiz_not_found": "Test topilmadi.",
        "session_not_found": "Sessiya topilmadi.",
        "session_already_completed": "Bu testni siz allaqachon yakunlagansiz. Qayta kirish mumkin emas.",
        "test_first": "Avval test yarating.",
        "copy_link": "Havolani nusxalash",
    },
    "ru": {
        "quiz_data_broken": "Данные теста повреждены.",
        "quiz_questions_missing": "В тесте нет вопросов.",
        "variant_required": "Используйте вариант A, B, C или D.",
        "group_required": "Выберите группу и тест.",
        "select_group": "Сначала выберите группу.",
        "assignment_not_found": "Задание не найдено.",
        "session_expired": "Срок сессии истёк.",
        "format_invalid": "Неверный формат файла.",

        "teacher_plan_required": "Активируйте тариф для учителей.",
        "group_name_required": "Введите название группы.",
        "group_created": "Группа создана.",
        "group_not_found": "Группа не найдена.",
        "groups_empty": "Групп пока нет.",
        "members_count": "уч.",
        "active": "Активна", "closed": "Закрыта",
        "delete": "Удалить",
        "assignment_created": "Задание назначено группе.",
        "assignment_empty": "Заданий пока нет.",
        "select_group_quiz": "Выберите группу и тест.",
        "variant_invalid": "Используйте вариант A, B, C или D.",
        "variants_ready": "Варианты готовы. Ключ для каждого варианта сохранён.",
        "select_quiz_variant": "Выберите тест и хотя бы один вариант.",
        "session_created": "Сессия создана.",
        "no_results": "Результатов пока нет.",
        "no_students_results": "Результатов учеников пока нет.",
        "server_error": "Произошла внутренняя ошибка сервера. Попробуйте ещё раз.",
        "db_busy": "База данных сервера занята. Повторите через несколько секунд.",
        "quiz_not_found": "Тест не найден.",
        "session_not_found": "Сессия не найдена.",
        "session_already_completed": "Вы уже завершили этот тест. Повторный вход недоступен.",
        "test_first": "Сначала создайте тест.",
        "copy_link": "Копировать ссылку",
    },
    "en": {
        "quiz_data_broken": "Quiz data is corrupted.",
        "quiz_questions_missing": "The quiz has no questions.",
        "variant_required": "Use variant A, B, C or D.",
        "group_required": "Select a group and a quiz.",
        "select_group": "Select a group first.",
        "assignment_not_found": "Assignment not found.",
        "session_expired": "The session has expired.",
        "format_invalid": "Invalid file format.",

        "teacher_plan_required": "Activate the Teacher plan.",
        "group_name_required": "Enter a group name.",
        "group_created": "Group created.",
        "group_not_found": "Group not found.",
        "groups_empty": "No groups yet.",
        "members_count": "members",
        "active": "Active", "closed": "Closed",
        "delete": "Delete",
        "assignment_created": "Assignment was assigned to the group.",
        "assignment_empty": "No assignments yet.",
        "select_group_quiz": "Select a group and a quiz.",
        "variant_invalid": "Use variant A, B, C or D.",
        "variants_ready": "Variants are ready. An answer key was saved for each variant.",
        "select_quiz_variant": "Select a quiz and at least one variant.",
        "session_created": "Session created.",
        "no_results": "No results yet.",
        "no_students_results": "No student results yet.",
        "server_error": "An internal server error occurred. Please try again.",
        "db_busy": "The server database is busy. Please try again in a few seconds.",
        "quiz_not_found": "Quiz not found.",
        "session_not_found": "Session not found.",
        "session_already_completed": "You have already completed this test. Re-entry is not allowed.",
        "test_first": "Create a quiz first.",
        "copy_link": "Copy link",
    },
}

def teacher_text(user_id, key, default=None):
    lang = get_user_lang(user_id)
    return TEACHER_MESSAGES.get(lang, TEACHER_MESSAGES["uz"]).get(key, default or key)


def require_teacher(user_id: int):
    add_user_to_db(user_id)
    def _check(conn):
        cur = conn.cursor()
        cur.execute("SELECT status, plan_key, premium_until FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()
    row = teacher_db_read(_check)
    if not row or not is_active_paid_status(row["status"] or "", row["premium_until"] or 0) or (row["plan_key"] or get_plan_key(row["status"] or "")) != "teachers":
        raise HTTPException(status_code=403, detail=teacher_text(user_id, "teacher_plan_required"))

@app.get("/api/teacher-quizzes")
def teacher_quizzes(user_id: int):
    require_teacher(user_id)
    conn = teacher_db_connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, title, total FROM quizzes WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return {"status": "ok", "quizzes": [dict(r) for r in rows]}


class TeacherSessionCreateRequest(BaseModel):
    user_id: int = Field(gt=0)
    quiz_id: str = Field(min_length=1, max_length=128)
    duration_minutes: int = Field(default=30, ge=5, le=180)
    variant_code: str = Field(default="", max_length=1)
    group_id: str = Field(default="", max_length=128)
    assignment_id: str = Field(default="", max_length=128)


@app.post("/api/teacher/create-session")
def teacher_create_session(req: TeacherSessionCreateRequest):
    """Create a fresh teacher group session safely on both new and legacy SQLite schemas.

    The endpoint deliberately does not reuse an old session. Every click creates a new
    session id and an independent 8-character access code. Legacy NOT NULL/extra columns
    are populated dynamically, and rare UNIQUE collisions are retried automatically.
    """
    require_teacher(req.user_id)
    assignment_id = (req.assignment_id or "").strip()
    duration = max(5, min(int(req.duration_minutes or 30), 180))
    variant_code = (req.variant_code or "").strip().upper()
    if variant_code and variant_code not in TEACHER_VARIANT_CODES:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "variant_required"))

    group_id = (req.group_id or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "select_group"))

    # Assignment sessions inherit their configured test duration and are tagged
    # so students and teachers can distinguish them from manually started group tests.
    if assignment_id:
        def _read_assignment(conn):
            conn.row_factory = sqlite3.Row
            row = conn.execute("""SELECT id,group_id,quiz_id,variant_code,title,active,COALESCE(duration_minutes,30) AS duration_minutes
                                  FROM teacher_assignments WHERE id=? AND owner_id=?""", (assignment_id, req.user_id)).fetchone()
            return row
        assignment_row = teacher_db_read(_read_assignment)
        if not assignment_row or not int(assignment_row["active"] or 0):
            raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "assignment_not_found"))
        if str(assignment_row["group_id"] or "") != group_id or str(assignment_row["quiz_id"] or "") != str(req.quiz_id):
            raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "assignment_not_found"))
        duration = max(5, min(int(assignment_row["duration_minutes"] or 30), 180))
        if not variant_code:
            variant_code = str(assignment_row["variant_code"] or "").strip().upper()

    # Validate quiz and group before opening the write transaction.
    quiz_row, _items = _load_quiz_items(req.quiz_id, req.user_id)
    if variant_code:
        _get_teacher_variant(req.quiz_id, req.user_id, variant_code)

    def _write(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name FROM teacher_groups WHERE id=? AND owner_id=? AND active=1",
            (group_id, req.user_id),
        )
        group = cur.fetchone()
        if not group:
            raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "group_not_found"))

        # Read the actual live schema every time. Railway may contain a database
        # created by an older Teacher version with extra required columns.
        rows = cur.execute("PRAGMA table_info(teacher_sessions)").fetchall()
        column_info = {row[1]: row for row in rows}
        required_core = {"id", "owner_id", "quiz_id", "code", "duration_minutes", "created_at", "expires_at", "active", "group_id", "variant_code"}
        missing_core = sorted(required_core - set(column_info))
        if missing_core:
            raise HTTPException(status_code=500, detail=teacher_text(req.user_id, "server_error"))

        session_title = str(quiz_row["title"] or "Guruh testi").strip() or "Guruh testi"
        if assignment_id and assignment_row is not None:
            session_title = str(assignment_row["title"] or session_title).strip() or session_title
        now = int(time.time())
        expires = now + duration * 60

        # A new id/code is generated for every session. We retry the INSERT itself,
        # not just the code lookup, because legacy databases may have additional
        # UNIQUE constraints unknown to this version.
        last_integrity = None
        for _attempt in range(12):
            sid = f"ts_{uuid.uuid4().hex[:12]}"
            code = uuid.uuid4().hex[:8].upper()
            values_by_column = {
                "id": sid,
                "session_id": sid,
                "owner_id": req.user_id,
                "teacher_id": req.user_id,
                "teacher_user_id": req.user_id,
                "user_id": req.user_id,
                "quiz_id": req.quiz_id,
                "code": code,
                "join_code": code,
                "title": session_title,
                "name": session_title,
                "duration_minutes": duration,
                "duration": duration,
                "created_at": now,
                "started_at": now,
                "expires_at": expires,
                "active": 1,
                "group_id": group_id,
                "group_name": group["name"],
                "variant_code": variant_code,
                "deleted": 0,
                "source_type": "assignment" if assignment_id else "group_test",
                "assignment_id": assignment_id,
            }

            insert_columns, insert_values = [], []
            for name, row in column_info.items():
                if name in values_by_column:
                    insert_columns.append(name)
                    insert_values.append(values_by_column[name])
                    continue
                not_null = bool(row[3])
                default_value = row[4]
                is_pk = bool(row[5])
                if not_null and not is_pk and default_value is None:
                    declared_type = str(row[2] or "").upper()
                    # Give unknown legacy required columns a value that is also
                    # unlikely to collide if the column happens to be UNIQUE.
                    if "INT" in declared_type or "REAL" in declared_type or "NUM" in declared_type:
                        fallback = now + random.randint(1, 999999)
                    else:
                        fallback = f"{sid}_{name}"
                    insert_columns.append(name)
                    insert_values.append(fallback)
                    logging.warning("Teacher session legacy required column filled: %s=%r", name, fallback)

            placeholders = ", ".join("?" for _ in insert_columns)
            try:
                cur.execute(
                    f"INSERT INTO teacher_sessions ({', '.join(insert_columns)}) VALUES ({placeholders})",
                    tuple(insert_values),
                )
                return {
                    "session_id": sid,
                    "code": code,
                    "expires_at": expires,
                    "quiz_title": quiz_row["title"],
                    "variant_code": variant_code,
                    "group_id": group_id,
                    "group_name": group["name"],
                    "source_type": "assignment" if assignment_id else "group_test",
                    "assignment_id": assignment_id,
                }
            except sqlite3.IntegrityError as e:
                last_integrity = e
                logging.warning("Teacher session INSERT collision/constraint (attempt %s): %s", _attempt + 1, e)
                continue

        raise HTTPException(status_code=500, detail=teacher_text(req.user_id, "server_error")) from last_integrity

    data = teacher_db_write(_write)
    return {"status": "ok", **data}

@app.get("/api/teacher-sessions")
def teacher_sessions(user_id: int):
    require_teacher(user_id)
    def _read(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""SELECT s.id, s.code, s.quiz_id, q.title, s.duration_minutes, s.created_at, s.expires_at, s.active,
                             COALESCE(s.group_id,'') AS group_id, COALESCE(s.variant_code,'') AS variant_code,
                             COALESCE(s.source_type,'group_test') AS source_type, COALESCE(s.assignment_id,'') AS assignment_id
                      FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id
                      WHERE s.owner_id=? AND COALESCE(s.deleted,0)=0
                      ORDER BY s.created_at DESC LIMIT 30""", (user_id,))
        return cur.fetchall()
    rows = teacher_db_read(_read)
    now = int(time.time())
    result = []
    for r in rows:
        active = bool(r["active"] and r["expires_at"] and r["expires_at"] >= now)
        result.append({**dict(r), "active": active})
    return {"status": "ok", "sessions": result}


@app.delete("/api/teacher/sessions/{session_id}")
def teacher_delete_session(session_id: str, user_id: int):
    require_teacher(user_id)
    def _write(conn):
        cur = conn.cursor()
        # Soft-delete: the teacher no longer sees the session, but participant
        # records remain safe for database integrity/audit purposes.
        cur.execute(
            "UPDATE teacher_sessions SET active=0, deleted=1 WHERE id=? AND owner_id=?",
            (session_id, user_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    teacher_db_write(_write)
    return {"status": "ok"}

@app.get("/api/teacher-session")
def teacher_session_info(code: str, user_id: int):
    def _read(conn):
        conn.row_factory=sqlite3.Row; cur=conn.cursor()
        cur.execute("SELECT s.id, s.owner_id, s.quiz_id, s.code, s.duration_minutes, s.expires_at, s.active, COALESCE(s.variant_code,'') AS variant_code, COALESCE(s.group_id,'') AS group_id, q.title, q.total FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.code=? AND COALESCE(s.deleted,0)=0", (code.upper(),))
        return cur.fetchone()
    row=teacher_db_read(_read)
    if not row: raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    # Session link is private to the teacher who created it and students who
    # belong to the corresponding group.
    def _access(conn):
        return _student_can_access_session(conn, row["id"], user_id)
    if not teacher_db_read(_access):
        raise HTTPException(status_code=403, detail=teacher_text(user_id, "session_not_found"))
    if not row["active"] or int(time.time())>row["expires_at"]: raise HTTPException(status_code=410, detail=teacher_text(user_id, "session_expired"))
    return {"status":"ok", "session_id":row["id"], "quiz_id":row["quiz_id"], "title":row["title"], "total":row["total"], "expires_at":row["expires_at"], "duration_minutes":row["duration_minutes"], "variant_code":row["variant_code"], "group_id":row["group_id"]}

def _session_owner_id(session_id: str, user_id: int):
    conn=teacher_db_connect(); cur=conn.cursor()
    cur.execute("SELECT owner_id FROM teacher_sessions WHERE id=?", (session_id,)); row=cur.fetchone(); conn.close()
    if not row: raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    return row[0]


@app.get("/api/teacher-group-sessions")
def teacher_group_sessions(user_id: int):
    """Return currently active test sessions for groups the student has joined.

    This endpoint intentionally does not require the Teacher plan: students only
    need to be members of the group. The teacher/owner can also see their own
    sessions through the normal Teacher UI.
    """
    now = int(time.time())
    def _read(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT s.id, s.code, s.quiz_id, q.title, q.total,
                   s.duration_minutes, s.created_at, s.expires_at, s.group_id,
                   g.name AS group_name, COALESCE(s.variant_code,'') AS variant_code,
                   COALESCE(s.source_type,'group_test') AS source_type, COALESCE(s.assignment_id,'') AS assignment_id,
                   COALESCE(p.finished_at,0) AS finished_at,
                   COALESCE(p.started_at,0) AS started_at,
                   COALESCE(p.score,0) AS student_score,
                   COALESCE(p.total,q.total) AS student_total,
                   COALESCE(p.percent,0) AS student_percent
            FROM teacher_group_members m
            JOIN teacher_groups g ON g.id=m.group_id AND g.active=1
            JOIN teacher_sessions s ON s.group_id=g.id
            JOIN quizzes q ON q.id=s.quiz_id
            LEFT JOIN teacher_participants p ON p.session_id=s.id AND p.user_id=m.user_id
            WHERE m.user_id=? AND s.active=1 AND COALESCE(s.deleted,0)=0
              AND s.expires_at>?
            ORDER BY s.created_at DESC
            LIMIT 30
        """, (user_id, now))
        return cur.fetchall()
    rows = teacher_db_read(_read)
    return {"status":"ok", "sessions":[dict(r) for r in rows], "server_time":now}


def _student_can_access_session(conn, session_id: str, user_id: int):
    """A session is accessible to its owner or to a member of its group."""
    row = conn.execute("""
        SELECT s.owner_id, s.group_id, s.active, COALESCE(s.deleted,0) AS deleted,
               s.expires_at
        FROM teacher_sessions s WHERE s.id=?
    """, (session_id,)).fetchone()
    if not row:
        return None
    if int(row[0] or 0) == int(user_id):
        return row
    group_id = str(row[1] or '')
    if not group_id:
        return None
    member = conn.execute(
        "SELECT 1 FROM teacher_group_members WHERE group_id=? AND user_id=? LIMIT 1",
        (group_id, user_id),
    ).fetchone()
    return row if member else None


@app.post("/api/teacher-session-start")
def teacher_session_start(session_id: str, user_id: int):
    """Register the moment a student actually enters a group test."""
    now = int(time.time())
    def _write(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id,s.owner_id,s.group_id,s.quiz_id,s.expires_at,s.active,
                   COALESCE(s.deleted,0) AS deleted,s.duration_minutes,q.title,q.total,
                   COALESCE(s.variant_code,'') AS variant_code
            FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id
            WHERE s.id=?
        """, (session_id,))
        s = cur.fetchone()
        if not s:
            raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
        if not _student_can_access_session(conn, session_id, user_id):
            raise HTTPException(status_code=403, detail=teacher_text(user_id, "session_not_found"))
        if s["deleted"] or not s["active"] or now > int(s["expires_at"] or 0):
            raise HTTPException(status_code=410, detail=teacher_text(user_id, "session_expired"))
        # The shared users table intentionally stores only account/status data;
        # Telegram profile names are supplied by the client when the result is submitted.
        first_name = "Telegram User"
        username = ""
        existing = cur.execute(
            "SELECT id, started_at, finished_at, score, total, percent FROM teacher_participants WHERE session_id=? AND user_id=? ORDER BY id LIMIT 1",
            (session_id, user_id),
        ).fetchone()
        if existing:
            # A completed group test is permanently locked for this student.
            # Never reset finished_at/score by opening the same session again.
            if int(existing[2] or 0) > 0:
                raise HTTPException(status_code=409, detail=teacher_text(user_id, "session_already_completed"))
            # If the student simply reopens an unfinished test, preserve the
            # original start time so the server-side session timer cannot reset.
            cur.execute("""
                UPDATE teacher_participants
                SET first_name=?, username=?, total=?
                WHERE id=?
            """, (first_name, username, s["total"], existing[0]))
        else:
            cur.execute("""
                INSERT INTO teacher_participants
                (session_id,user_id,first_name,username,score,total,percent,started_at,finished_at)
                VALUES (?,?,?,?,?,?,?,?,0)
            """, (session_id,user_id,first_name,username,0,s["total"],0,now))
        return dict(s)
    data = teacher_db_write(_write)
    return {"status":"ok", "session_id":data["id"], "quiz_id":data["quiz_id"],
            "title":data["title"], "total":data["total"],
            "duration_minutes":data["duration_minutes"], "expires_at":data["expires_at"],
            "variant_code":data["variant_code"], "server_time":now}


@app.get("/api/teacher-session-quiz")
def teacher_session_quiz(session_id: str, user_id: int):
    conn=teacher_db_connect(); conn.row_factory=sqlite3.Row; cur=conn.cursor()
    cur.execute("SELECT s.quiz_id, q.title, s.expires_at, s.active, COALESCE(s.variant_code,'') AS variant_code, q.quiz_json FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.id=? AND COALESCE(s.deleted,0)=0", (session_id,))
    row=cur.fetchone()
    if not row: conn.close(); raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    access = _student_can_access_session(conn, session_id, user_id)
    conn.close()
    if not access: raise HTTPException(status_code=403, detail=teacher_text(user_id, "session_not_found"))
    if not row["active"] or int(time.time())>row["expires_at"]: raise HTTPException(status_code=410, detail=teacher_text(user_id, "session_expired"))
    if row["variant_code"]:
        payload = _get_teacher_variant(row["quiz_id"], _session_owner_id(session_id, user_id), row["variant_code"])
    else:
        payload = json.loads(row["quiz_json"])
    return {"status":"ok", "quiz_id":row["quiz_id"], "title":row["title"] or "Test", "quiz_json":payload, "variant_code":row["variant_code"]}


class TeacherSubmitRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    user_id: int = Field(gt=0)
    first_name: str = Field(default="", max_length=100)
    username: str = Field(default="", max_length=64)
    score: int = Field(ge=0, le=100000)
    total: int = Field(ge=1, le=100000)
    percent: int = Field(ge=0, le=100)


@app.post("/api/teacher-submit")
def teacher_submit(req: TeacherSubmitRequest):
    conn=teacher_db_connect(); conn.row_factory=sqlite3.Row; cur=conn.cursor()
    cur.execute("SELECT s.id,s.quiz_id,s.group_id,s.owner_id,s.expires_at,s.active,COALESCE(s.deleted,0) AS deleted,q.total AS quiz_total FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.id=?", (req.session_id,)); s=cur.fetchone()
    if not s: conn.close(); raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "session_not_found"))
    now=int(time.time())
    if s["deleted"]: conn.close(); raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "session_not_found"))
    # Natijani faqat sessiya egasi yoki shu guruhga haqiqiy a'zo bo'lgan
    # Telegram foydalanuvchisi yuborishi mumkin. user_id HMAC middleware orqali tasdiqlanadi.
    if not _student_can_access_session(conn, req.session_id, req.user_id):
        conn.close(); raise HTTPException(status_code=403, detail=teacher_text(req.user_id, "session_not_found"))
    cur.execute("SELECT id, started_at, finished_at FROM teacher_participants WHERE session_id=? AND user_id=? ORDER BY id LIMIT 1", (req.session_id, req.user_id))
    existing_participant = cur.fetchone()
    # Sessiyaga oldindan kirish qaydi bo'lmasa, natijani bevosita yuborishga yo'l qo'ymaymiz.
    if not existing_participant or int(existing_participant[1] or 0) <= 0:
        conn.close(); raise HTTPException(status_code=403, detail=teacher_text(req.user_id, "session_not_found"))
    # Natija chegaralari va test savollari soni serverdagi ma'lumot bilan mos bo'lishi shart.
    try:
        score, total, percent = int(req.score), int(req.total), int(req.percent)
    except (TypeError, ValueError):
        conn.close(); raise HTTPException(status_code=400, detail="Natija qiymatlari noto'g'ri.")
    expected_total = int(s["quiz_total"] or 0)
    if total != expected_total or total <= 0 or score < 0 or score > total or percent != int((score * 100 / total) + 0.5) or percent < 0 or percent > 100:
        conn.close(); raise HTTPException(status_code=400, detail="Test natijasi tekshiruvdan o'tmadi.")
    # Timer 00:00 da avtomatik yuborilgan natija ham server tomonidan qabul qilinadi,
    # lekin faqat o'quvchi sessiya tugashidan oldin kirgan bo'lsa.
    if not s["active"] or now>int(s["expires_at"] or 0):
        started = int(existing_participant[1] or 0)
        finished = int(existing_participant[2] or 0)
        if started > int(s["expires_at"] or 0) or finished:
            conn.close(); raise HTTPException(status_code=410, detail=teacher_text(req.user_id, "session_expired"))
    values = (req.first_name[:100], req.username[:100], score, total, percent, now, now)
    if existing_participant:
        # Once a student has submitted a result, the session is locked for that
        # student. A duplicate/retry must never overwrite the completed result.
        if int(existing_participant[2] or 0) > 0:
            conn.close(); raise HTTPException(status_code=409, detail=teacher_text(req.user_id, "session_already_completed"))
        cur.execute("UPDATE teacher_participants SET first_name=?, username=?, score=?, total=?, percent=?, finished_at=? WHERE id=?", (*values[:5], values[6], existing_participant[0]))
    else:
        cur.execute("INSERT INTO teacher_participants (session_id,user_id,first_name,username,score,total,percent,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)", (req.session_id,req.user_id,req.first_name[:100],req.username[:100],req.score,req.total,req.percent,now,now))
    conn.commit(); conn.close(); return {"status":"ok"}


@app.get("/api/teacher-session-results")
def teacher_session_results(session_id: str, user_id: int):
    require_teacher(user_id)
    def _read(conn):
        conn.row_factory=sqlite3.Row; cur=conn.cursor()
        cur.execute("SELECT s.quiz_id, q.title, s.code, s.expires_at, COALESCE(s.source_type,'group_test') AS source_type, COALESCE(s.assignment_id,'') AS assignment_id FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.id=? AND s.owner_id=? AND COALESCE(s.deleted,0)=0", (session_id,user_id)); s=cur.fetchone()
        if not s:
            raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
        cur.execute("SELECT first_name, username, score, total, percent, started_at, finished_at FROM teacher_participants WHERE session_id=? ORDER BY percent DESC, score DESC, finished_at ASC", (session_id,)); rows=cur.fetchall()
        active_students = sum(1 for r in rows if int(r["started_at"] or 0) > 0 and int(r["finished_at"] or 0) == 0)
        completed_students = sum(1 for r in rows if int(r["finished_at"] or 0) > 0)
        return dict(s), rows, active_students, completed_students
    s, rows, active_students, completed_students=teacher_db_read(_read)
    return {"status":"ok", "session":{"id":session_id,"code":s["code"],"quiz_title":s["title"],"expires_at":s["expires_at"],"source_type":s["source_type"],"assignment_id":s["assignment_id"]}, "active_students":active_students, "completed_students":completed_students, "participants":[dict(r) for r in rows]}

def _teacher_export_rows(session_id, owner_id):
    require_teacher(owner_id)
    conn=teacher_db_connect(); conn.row_factory=sqlite3.Row; cur=conn.cursor()
    cur.execute("SELECT s.code, q.title FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.id=? AND s.owner_id=? AND COALESCE(s.deleted,0)=0", (session_id,owner_id)); s=cur.fetchone()
    cur.execute("SELECT first_name, username, score, total, percent, finished_at FROM teacher_participants WHERE session_id=? ORDER BY percent DESC, score DESC", (session_id,)); rows=cur.fetchall(); conn.close()
    if not s: raise HTTPException(status_code=404, detail=teacher_text(owner_id, "session_not_found"))
    return s, rows


def _register_pdf_font():
    candidates=["/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    bolds=["/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    reg=bld=False
    for path in candidates:
        if os.path.exists(path):
            try: pdfmetrics.registerFont(TTFont("QuizPilotFont", path)); reg=True; break
            except Exception: pass
    for path in bolds:
        if os.path.exists(path):
            try: pdfmetrics.registerFont(TTFont("QuizPilotFontBold", path)); bld=True; break
            except Exception: pass
    return reg,bld


@app.get("/api/teacher-export")
def teacher_export(session_id: str, user_id: int, format: str):
    s, rows = _teacher_export_rows(session_id, user_id)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    fmt=format.lower().strip()
    if fmt not in {"pdf", "docx"}:
        raise HTTPException(status_code=400, detail="Faqat PDF va Word formatlari qo‘llab-quvvatlanadi.")
    safe=re.sub(r"[^A-Za-z0-9_-]+", "_", s["title"] or "quiz")[:40]
    if fmt=="docx":
        path=os.path.join(DOWNLOADS_DIR,f"{safe}_{s['code']}.docx")
        doc=Document(); doc.add_heading(s["title"], level=1); doc.add_paragraph(f"Sessiya: {s['code']}")
        table=doc.add_table(rows=1, cols=6); hdr=table.rows[0].cells
        for i,t in enumerate(["№","O'quvchi","Username","To'g'ri","Jami","Foiz"]): hdr[i].text=t
        # Faqat MTA Word eksportida ishtirokchi ismi/username ichidagi
        # Telegram emoji belgilarini olib tashlaymiz: Word'da '?' bo'lib chiqmaydi.
        # PDF eksporti va ilovaning boshqa qismlariga tegilmaydi.
        _mta_word_emoji_pattern = re.compile(
            "["
            "\\U0001F1E6-\\U0001F1FF"
            "\\U0001F300-\\U0001FAFF"
            "\\u2600-\\u27BF"
            "\\uFE0E\\uFE0F\\u200D\\u20E3"
            "\\U0001F3FB-\\U0001F3FF"
            "]"
        )
        def _mta_word_without_telegram_emoji(value):
            return _mta_word_emoji_pattern.sub("", str(value if value is not None else ""))

        for i,r in enumerate(rows,1):
            cells=table.add_row().cells
            vals=[i,_mta_word_without_telegram_emoji(r["first_name"]),_mta_word_without_telegram_emoji(r["username"]),r["score"],r["total"],f"{r['percent']}%"]
            for j,v in enumerate(vals): cells[j].text=str(v)
        doc.save(path); return FileResponse(path, filename=os.path.basename(path), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if fmt=="pdf":
        path=os.path.join(DOWNLOADS_DIR,f"{safe}_{s['code']}.pdf")
        reg,bld=_register_pdf_font(); font="QuizPilotFont" if reg else "Helvetica"; bold="QuizPilotFontBold" if bld else font
        # PDF-only fallback fonts and emoji-to-image support; other export formats stay untouched.
        symbol_font = font
        for _symbol_path in (
            str(Path(__file__).resolve().parent / "fonts" / "NotoSansSymbols2-Regular.ttf"),
            "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            if os.path.exists(_symbol_path):
                try:
                    pdfmetrics.registerFont(TTFont("QuizPilotSymbols", _symbol_path))
                    symbol_font = "QuizPilotSymbols"
                    break
                except Exception:
                    pass

        emoji_font_path = next((p for p in (
            str(Path(__file__).resolve().parent / "fonts" / "NotoColorEmoji.ttf"),
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
        ) if os.path.exists(p)), None)
        _emoji_images = []
        def _pdf_cell_text(value):
            # Escape user-supplied text so markup characters render literally.
            text = str(value if value is not None else "")
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if not emoji_font_path:
                # Keep the original Unicode text; use a broad symbol font where available.
                return text.replace("№", f'<font name="{symbol_font}">№</font>')
            try:
                from PIL import Image, ImageDraw, ImageFont
                pieces = []
                i = 0
                while i < len(text):
                    ch = text[i]
                    cp = ord(ch)
                    # Emoji/pictograph ranges, including flags and supplementary emoji.
                    is_emoji = (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
                                or 0x1F1E6 <= cp <= 0x1F1FF)
                    if not is_emoji:
                        pieces.append(ch)
                        i += 1
                        continue
                    seq = ch
                    i += 1
                    while i < len(text) and (ord(text[i]) in (0xFE0E, 0xFE0F, 0x200D, 0x20E3)
                                             or 0x1F3FB <= ord(text[i]) <= 0x1F3FF
                                             or 0x1F1E6 <= ord(text[i]) <= 0x1F1FF):
                        seq += text[i]
                        i += 1
                        if seq.endswith("\u200d") and i < len(text):
                            seq += text[i]
                            i += 1
                    try:
                        _efont = ImageFont.truetype(emoji_font_path, 109)
                        _bbox = _efont.getbbox(seq)
                        _im = Image.new("RGBA", (max(72, _bbox[2]-_bbox[0]+12), max(72, _bbox[3]-_bbox[1]+12)), (255,255,255,0))
                        ImageDraw.Draw(_im).text((6-_bbox[0], 6-_bbox[1]), seq, font=_efont, embedded_color=True)
                        _emoji_path = os.path.join(DOWNLOADS_DIR, f"_emoji_{uuid.uuid4().hex}.png")
                        _im.save(_emoji_path)
                        _emoji_images.append(_emoji_path)
                        pieces.append(f'<img src="{_emoji_path}" width="10" height="10" valign="middle"/>')
                    except Exception:
                        pieces.append(seq)
                return "".join(pieces).replace("№", f'<font name="{symbol_font}">№</font>')
            except Exception:
                return text.replace("№", f'<font name="{symbol_font}">№</font>')

        doc=SimpleDocTemplate(path,pagesize=A4,rightMargin=28,leftMargin=28,topMargin=28,bottomMargin=28)
        styles=getSampleStyleSheet(); title_style=ParagraphStyle("qp_title",parent=styles["Title"],fontName=bold,fontSize=16,alignment=TA_CENTER)
        body_style=ParagraphStyle("qp_body",parent=styles["BodyText"],fontName=font,fontSize=8)
        story=[Paragraph(_pdf_cell_text(s["title"]),title_style),Spacer(1,8),Paragraph(_pdf_cell_text(f"Session: {s['code']}"),body_style),Spacer(1,8)]
        data=[[Paragraph(_pdf_cell_text(v), body_style) for v in ["№","O'quvchi","Username","To'g'ri","Jami","Foiz"]]]
        for i,r in enumerate(rows,1):
            data.append([Paragraph(_pdf_cell_text(v), body_style) for v in
                         [str(i),r["first_name"],r["username"],str(r["score"]),str(r["total"]),f"{r['percent']}%"]])
        table=Table(data,repeatRows=1,colWidths=[24,170,100,45,40,45])
        table.setStyle(TableStyle([("FONTNAME",(0,0),(-1,-1),font),("FONTNAME",(0,0),(-1,0),bold),("FONTSIZE",(0,0),(-1,-1),8),("GRID",(0,0),(-1,-1),0.4,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
        story.append(table)
        try:
            doc.build(story)
        finally:
            for _emoji_path in _emoji_images:
                try: os.remove(_emoji_path)
                except OSError: pass
        return FileResponse(path, filename=os.path.basename(path), media_type="application/pdf")
    raise HTTPException(status_code=400, detail=teacher_text(user_id, "format_invalid"))


# Telegram WebView blob URL fayllarini telefonga saqlashda muammo berishi mumkin.
# Shu sababli autentifikatsiyalangan so'rovdan keyin qisqa muddatli bir martalik URL beriladi.
_TEACHER_DOWNLOAD_TICKETS = {}
_TEACHER_DOWNLOAD_TICKETS_LOCK = threading.Lock()
_TEACHER_DOWNLOAD_TICKET_TTL = 300


@app.post("/api/teacher-download-ticket")
def create_teacher_download_ticket(user_id: int, source_url: str, request: Request):
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != request.url.netloc:
        raise HTTPException(status_code=400, detail="Yuklash manzili noto'g'ri.")

    params = parse_qs(parsed.query)
    allowed_paths = {
        "/api/teacher-export",
        "/api/teacher/export-assignment",
        "/api/teacher/export-quiz",
    }
    if parsed.path not in allowed_paths:
        raise HTTPException(status_code=400, detail="Bu fayl turini yuklashga ruxsat yo'q.")

    try:
        source_user_id = int((params.get("user_id") or [""])[0])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Foydalanuvchi ID noto'g'ri.")
    if source_user_id != user_id:
        raise HTTPException(status_code=403, detail="Foydalanuvchi ID mos kelmadi.")

    fmt = (params.get("format") or [""])[0].lower().strip()
    if fmt not in {"pdf", "docx"}:
        raise HTTPException(status_code=400, detail="Fayl formati noto'g'ri.")

    # Mavjud eksport funksiyalari ishlatiladi; fayl mazmuni o'zgartirilmaydi.
    if parsed.path == "/api/teacher-export":
        session_id = (params.get("session_id") or [""])[0]
        file_response = teacher_export(session_id=session_id, user_id=user_id, format=fmt)
    elif parsed.path == "/api/teacher/export-assignment":
        assignment_id = (params.get("assignment_id") or [""])[0]
        file_response = teacher_export_assignment(assignment_id=assignment_id, user_id=user_id, format=fmt)
    else:
        quiz_id = (params.get("quiz_id") or [""])[0]
        variant = (params.get("variant") or ["A"])[0]
        try:
            all_variants = int((params.get("all_variants") or ["0"])[0])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Variant parametri noto'g'ri.")
        file_response = teacher_export_quiz(
            quiz_id=quiz_id, user_id=user_id, format=fmt,
            variant=variant, all_variants=all_variants
        )

    file_path = getattr(file_response, "path", None)
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=500, detail="Fayl tayyorlanmadi.")

    token = secrets.token_urlsafe(32)
    now = time.time()
    with _TEACHER_DOWNLOAD_TICKETS_LOCK:
        expired = [key for key, value in _TEACHER_DOWNLOAD_TICKETS.items()
                   if value["expires_at"] <= now]
        for key in expired:
            _TEACHER_DOWNLOAD_TICKETS.pop(key, None)
        _TEACHER_DOWNLOAD_TICKETS[token] = {
            "path": file_path,
            "filename": getattr(file_response, "filename", None) or os.path.basename(file_path),
            "media_type": getattr(file_response, "media_type", None) or "application/octet-stream",
            "expires_at": now + _TEACHER_DOWNLOAD_TICKET_TTL,
        }

    return {"status": "ok", "download_url": f"/api/teacher-download/{token}"}


@app.get("/api/teacher-download/{token}")
def download_teacher_ticket_file(token: str):
    with _TEACHER_DOWNLOAD_TICKETS_LOCK:
        ticket = _TEACHER_DOWNLOAD_TICKETS.pop(token, None)
    if not ticket or ticket["expires_at"] <= time.time() or not os.path.isfile(ticket["path"]):
        raise HTTPException(status_code=404, detail="Yuklash havolasi eskirgan. Qayta urinib ko'ring.")
    return FileResponse(
        ticket["path"],
        filename=ticket["filename"],
        media_type=ticket["media_type"],
    )


@app.get("/api/flashcards")
def get_flashcards(user_id: int):
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, front, back FROM flashcards WHERE user_id = ? ORDER BY"
        " created_at DESC",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    cards = [{"id": r["id"], "front": r["front"], "back": r["back"]} for r in rows]
    return {"status": "ok", "cards": cards}


@app.post("/api/create-flashcard")
def create_flashcard(req: FlashcardCreateRequest):
    add_user_to_db(req.user_id)
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT status, premium_until, flashcard_free_used, created_at FROM users WHERE user_id = ?",
        (req.user_id,),
    )
    u = cursor.fetchone()
    now = int(time.time())
    is_paid = bool(u and is_active_paid_status(u["status"] or "", u["premium_until"] or 0))

    # 30 kunlik bepul davr tugagan bo'lsa, uchala hisoblagichni reset qilamiz.
    if u and now - (u["created_at"] or now) >= 30 * 24 * 3600 and not is_paid:
        cursor.execute(
            "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = ? WHERE user_id = ?",
            (now, req.user_id),
        )
        conn.commit()

    # Premium / Teacher: cheksiz.
    if not is_paid and FREE_FLASHCARD_LIMIT != -1:
        cursor.execute(
            "UPDATE users SET flashcard_free_used = COALESCE(flashcard_free_used, 0) + 1 "
            "WHERE user_id = ? AND COALESCE(flashcard_free_used, 0) < ?",
            (req.user_id, FREE_FLASHCARD_LIMIT),
        )
        if cursor.rowcount != 1:
            conn.close()
            lang = get_user_lang(req.user_id)
            messages = {
                "uz": MESSAGES["uz"]["flashcard_limit_reached"],
                "ru": MESSAGES["ru"]["flashcard_limit_reached"],
                "en": MESSAGES["en"]["flashcard_limit_reached"],
            }
            return {
                "status": "error",
                "error_code": "flashcard_limit",
                "message": messages.get(lang, messages["uz"]),
            }
        conn.commit()

    card_id = f"c_{int(time.time())}_{os.urandom(2).hex()}"
    cursor.execute(
        "INSERT INTO flashcards VALUES (?, ?, ?, ?, ?)",
        (card_id, req.user_id, req.front, req.back, int(time.time())),
    )
    conn.commit()
    conn.close()
    if not is_paid:
        notify_free_limit_reached(req.user_id, "flashcard")
    return {"status": "ok"}


@app.delete("/api/delete-flashcard")
def delete_flashcard(card_id: str, user_id: int):
    conn = _connect_db()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM flashcards WHERE id = ? AND user_id = ?", (card_id, user_id)
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/quiz-detail")
def get_quiz_detail(quiz_id: str, user_id: int):
    """Shaxsiy testni faqat uning egasiga qaytaradi."""
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT title, quiz_json FROM quizzes WHERE id = ? AND user_id = ?",
        (quiz_id, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"status": "ok", "title": row["title"] or "Test", "quiz_json": json.loads(row["quiz_json"])}
    # Begona test mavjudligini ham oshkor qilmaymiz.
    raise HTTPException(status_code=404, detail="Test topilmadi")


@app.post("/api/update-progress")
def update_progress(data: ProgressUpdateRequest):
    """Natijani faqat test egasi uchun, testdagi haqiqiy savollar soniga mos saqlaydi."""
    conn = _connect_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT total FROM quizzes WHERE id = ? AND user_id = ?",
            (data.quiz_id, data.user_id),
        )
        quiz_row = cursor.fetchone()
        if not quiz_row:
            raise HTTPException(status_code=404, detail="Test topilmadi.")

        total = int(quiz_row["total"] or 0)
        if total <= 0 or data.correct_count > total:
            raise HTTPException(status_code=400, detail="To'g'ri javoblar soni test hajmiga mos emas.")
        expected_percent = int((data.correct_count * 100 / total) + 0.5)
        if data.percent != expected_percent:
            raise HTTPException(status_code=400, detail="Test foizi hisob-kitobga mos kelmaydi.")

        cursor.execute(
            "UPDATE quizzes SET answered = total, last_score = ?, last_percent = ?"
            " WHERE id = ? AND user_id = ?",
            (data.correct_count, data.percent, data.quiz_id, data.user_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Test topilmadi.")
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/api/delete-quiz")
def delete_quiz(quiz_id: str, user_id: int):
    try:
        conn = _connect_db()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM quizzes WHERE id = ? AND user_id = ?", (quiz_id, user_id)
        )
        conn.commit()
        conn.close()
        return {"status": "ok", "message": "Test o'chirildi."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Xatolik.")


def start_bot_polling():
    while not _APP_STOP_EVENT.is_set():
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except Exception:
            if not _APP_STOP_EVENT.is_set():
                logging.exception("Telegram polling vaqtinchalik xatoga uchradi; qayta uriniladi.")
                _APP_STOP_EVENT.wait(5)


@app.exception_handler(Exception)
async def api_exception_handler(request: Request, exc: Exception):
    # Teacher/frontend fetchlari 500 paytida "Unexpected token I..." kabi JSON parse
    # xatosini bermasligi uchun API xatolarini ham JSON ko‘rinishida qaytaramiz.
    if request.url.path.startswith("/api/teacher") or request.url.path.startswith("/api/teacher-"):
        logging.exception("Teacher API internal error: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"status": "error", "detail": "__TEACHER_SERVER_ERROR__"})
    if request.url.path.startswith("/api/"):
        logging.exception("API internal error: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"status": "error", "detail": "Server ichki xatosi"})
    raise exc





# ==================================================================
# QUIZ AI ADMIN PANEL — barcha mavjud admin amallarini bitta menyuga jamlash
# ==================================================================
def _admin_panel_main_markup():
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("📊 Umumiy statistika", callback_data="adminpanel:run:stats"),
        telebot.types.InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="adminpanel:run:users"),
        telebot.types.InlineKeyboardButton("💳 Daromad va obunalar", callback_data="adminpanel:run:payments"),
        telebot.types.InlineKeyboardButton("🖥 Server monitoring", callback_data="adminpanel:run:monitor"),
        telebot.types.InlineKeyboardButton("🤖 AI test statistikasi", callback_data="adminpanel:run:ai_stats"),
        telebot.types.InlineKeyboardButton("🔎 AI navbati va API nazorati", callback_data="adminpanel:run:ai_monitor"),
        telebot.types.InlineKeyboardButton("🧾 To‘lov AI holati", callback_data="adminpanel:run:payment_ai_status"),
        telebot.types.InlineKeyboardButton("🗄 Baza holati", callback_data="adminpanel:run:db_health"),
        telebot.types.InlineKeyboardButton("📥 Baza backup", callback_data="adminpanel:run:db_backup"),
        telebot.types.InlineKeyboardButton("📢 Ommaviy xabar", callback_data="adminpanel:run:broadcast"),
        telebot.types.InlineKeyboardButton("🎁 Bonuslar", callback_data="adminpanel:submenu:bonuses"),
        telebot.types.InlineKeyboardButton("🛠 Xatolik ogohlantirish sinovi", callback_data="adminpanel:run:test_error_alert"),
    )
    return markup


def _admin_panel_bonus_markup():
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        telebot.types.InlineKeyboardButton("🎁 Oddiy Premium bonus", callback_data="adminpanel:guide:bonus"),
        telebot.types.InlineKeyboardButton("👨‍🏫 O‘qituvchi bonusi", callback_data="adminpanel:guide:bonus_teacher"),
        telebot.types.InlineKeyboardButton("🗑 Bonusni bekor qilish", callback_data="adminpanel:guide:bonus_revoke"),
        telebot.types.InlineKeyboardButton("⬅️ Asosiy menyu", callback_data="adminpanel:home"),
    )
    return markup


@bot.message_handler(commands=["help_admin"])
def admin_help_panel_command(message):
    """Administrator uchun tugmali boshqaruv markazi."""
    if not _admin_only(message):
        return
    text = (
        "🛡 <b>Quiz AI — Admin Panel</b>\n\n"
        "Kerakli bo‘limni tanlang. Hisobotlar shu yerning o‘zida ochiladi.\n\n"
        "📊 Statistika — foydalanuvchilar, testlar va bugungi faollik.\n"
        "👥 Foydalanuvchilar — yangi, faol va Premium foydalanuvchilar.\n"
        "💳 Daromad — tushum, tariflar va to‘lovlar.\n"
        "🖥 Monitoring — AI navbati sozlamalari va SQLite ulanishi.\n"
        "🗄 Baza — SQLite holati yoki backup yuklab olish.\n"
        "📢 Ommaviy xabar — matnni tayyorlash, ko‘rish va tasdiqlash.\n"
        "🎁 Bonuslar — buyruq formati bo‘yicha yordam.\n\n"
        "🔐 Ushbu menyu faqat administrator uchun ochiq."
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=_admin_panel_main_markup())
    logging.info("Admin Panel ochildi.")


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("adminpanel:"))
def admin_help_panel_callback(call):
    if not is_admin_id(call.from_user.id):
        bot.answer_callback_query(call.id, "Bu menyu faqat administrator uchun.", show_alert=True)
        return

    action = call.data.split(":", 2)
    kind = action[1] if len(action) > 1 else "home"
    value = action[2] if len(action) > 2 else ""

    if kind == "home":
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                "🛡 <b>Quiz AI — Admin Panel</b>\n\nKerakli funksiyani tanlang:",
                call.message.chat.id, call.message.message_id,
                parse_mode="HTML", reply_markup=_admin_panel_main_markup()
            )
        except Exception:
            bot.send_message(call.message.chat.id, "🛡 <b>Quiz AI — Admin Panel</b>\n\nKerakli funksiyani tanlang:", parse_mode="HTML", reply_markup=_admin_panel_main_markup())
        return

    if kind == "submenu" and value == "bonuses":
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text("🎁 <b>Bonuslarni boshqarish</b>\n\nKerakli amalni tanlang:", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=_admin_panel_bonus_markup())
        except Exception:
            bot.send_message(call.message.chat.id, "🎁 <b>Bonuslarni boshqarish</b>\n\nKerakli amalni tanlang:", parse_mode="HTML", reply_markup=_admin_panel_bonus_markup())
        return

    if kind == "guide":
        guide_texts = {
            "bonus": "🎁 <b>Oddiy Premium bonus</b>\nFormat: <code>/bonus USER_ID KUN</code>\nMisol: <code>/bonus 123456789 30</code>",
            "bonus_teacher": "👨‍🏫 <b>O‘qituvchi bonusi</b>\nFormat: <code>/bonus_teacher USER_ID KUN</code>\nMisol: <code>/bonus_teacher 123456789 30</code>",
            "bonus_revoke": "🗑 <b>Bonusni bekor qilish</b>\nFormat: <code>/bonus_revoke USER_ID</code>\nMisol: <code>/bonus_revoke 123456789</code>",
        }
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, guide_texts.get(value, "Buyruq topilmadi."), parse_mode="HTML", reply_markup=telebot.types.InlineKeyboardMarkup().add(telebot.types.InlineKeyboardButton("⬅️ Admin Panel", callback_data="adminpanel:home")))
        return

    if kind == "run":
        handlers = {
            "stats": admin_stats_command,
            "users": admin_users_report_command,
            "payments": admin_payments_report_command,
            "monitor": admin_monitor_command,
            "ai_stats": admin_ai_generation_stats_command,
            "ai_monitor": admin_ai_monitor_command,
            "payment_ai_status": admin_payment_ai_status,
            "db_health": admin_database_health_command,
            "db_backup": admin_database_backup_command,
            "broadcast": admin_broadcast_command,
            "test_error_alert": admin_test_error_alert_command,
        }
        handler = handlers.get(value)
        if not handler:
            bot.answer_callback_query(call.id, "Bu funksiya ushbu faylda mavjud emas.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Funksiya ishga tushirilmoqda…")
        # Mavjud handlerlarni qayta ishlatamiz; ularning asosiy funksional kodi o‘zgarmaydi.
        from types import SimpleNamespace
        synthetic_message = SimpleNamespace(
            chat=call.message.chat,
            from_user=call.from_user,
            text="/" + value,
            message_id=call.message.message_id,
        )
        try:
            handler(synthetic_message)
            logging.info("Admin Panel orqali funksiya ishga tushirildi | funksiya=%s", value)
        except Exception:
            logging.exception("Admin Panel funksiyasini ishga tushirishda xatolik | funksiya=%s", value)
            bot.send_message(call.message.chat.id, "❌ Funksiyani ishga tushirishda xatolik yuz berdi. Railway loglarini tekshiring.")
        return

    bot.answer_callback_query(call.id, "Noma’lum menyu amali.", show_alert=True)




    # ==================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
