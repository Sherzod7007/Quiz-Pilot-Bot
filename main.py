# -*- coding: utf-8 -*-
import docx
import asyncio
import re
from docx import Document
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from google import genai
from google.genai import types as genai_types
import json
import logging
import os
from pydantic import BaseModel, Field
from pypdf import PdfReader
from openpyxl import Workbook
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
from copy import deepcopy
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)
templates = Jinja2Templates(directory="templates")

raw_admin_id = os.getenv("ADMIN_ID")
try:
    ADMIN_ID = int(raw_admin_id.strip()) if raw_admin_id else None
except Exception as e:
    logging.error(f"ADMIN_ID ni int ga o'tkazishda xato: {e}")
    ADMIN_ID = None

# --- ADMIN SUPPORT ---
support_waiting_users = set()
support_reply_targets = {}

raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = (
    [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
)
current_key_index = 0
key_lock = threading.Lock()
# Bir vaqtning o'zida Gemini'ga juda ko'p so'rov yuborilib ketmasligi uchun.
# 7 ta key mavjud bo'lgani sababli 7 ta parallel Gemini requestga ruxsat beriladi.
gemini_semaphore = threading.Semaphore(max(1, min(7, len(GOOGLE_API_KEYS))))

DOWNLOADS_DIR = "downloads"
DB_PATH = (
    "/data/quiz_pilot_v2.db" if os.path.exists("/data") else "quiz_pilot_v2.db"
)

# --- TARIFLAR: yagona manba (narx va nomlar UZ/RU/EN) ---
TARIFFS = {
    "daily": {
        "price": "10 000 so'm",
        "duration": 24 * 3600,
        "names": {"uz": "Kunlik Cheksiz (24 soat)", "ru": "Суточный Безлимит (24 ч)", "en": "Daily Unlimited (24h)"},
    },
    "weekly": {
        "price": "35 000 so'm",
        "duration": 7 * 24 * 3600,
        "names": {"uz": "Haftalik Cheksiz (7 kun)", "ru": "Недельный Безлимит (7 дн)", "en": "Weekly Unlimited (7d)"},
    },
    "monthly": {
        "price": "65 000 so'm",
        "duration": 30 * 24 * 3600,
        "names": {"uz": "Oylik Cheksiz (30 kun)", "ru": "Месячный Безлимит (30 дн)", "en": "Monthly Unlimited (30d)"},
    },
    "teachers": {
        "price": "95 000 so'm",
        "duration": 30 * 24 * 3600,
        "names": {"uz": "O'qituvchilar Uchun (30 kun)", "ru": "Для Учителей (30 дн)", "en": "For Teachers (30d)"},
    },
}
FREE_QUIZ_LIMIT = 3
FREE_PUBLIC_LIMIT = 3
FREE_FLASHCARD_LIMIT = 3

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
    "support_prompt": "💬 Admin bilan bog'lanish. Savolingiz yoki muammoingizni shu yerga yozing. Xabaringiz administratorga yuboriladi.",
    "support_sent": "✅ Murojaatingiz administratorga yuborildi. Javob kelishini kuting.",
    "support_admin_title": "📩 Yangi murojaat",
    "support_reply_btn": "✉️ Javob berish",
    "support_reply_prompt": "✍️ Javobingizni yozing. U foydalanuvchiga yuboriladi.",
    "support_reply_sent": "✅ Javob foydalanuvchiga yuborildi.",
    "uz": {
        "welcome": (
            "👋 Salom, {name}! Quiz Pilot Super Mini App tizimiga xush kelibsiz.\n\n"
            "🚀 Yangi Yangilanish:\n🔒 Bizning aqlli to'lov tizimimiz ishga tushdi. "
            "Premium rejalarni faollashtirib, cheksiz testlar yarating!\n\n"
            "👇 Marhamat, pastdagi tugmani bosib ilovani oching!"
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
        "quiz_ready": "📝 {title} darsligi bo'yicha jami {count} ta test savoli muvaffaqiyatli tayyorlandi!",
    },
    "ru": {
        "welcome": (
            "👋 Привет, {name}! Добро пожаловать в Quiz Pilot Super Mini App.\n\n"
            "🚀 Новое обновление:\n🔒 Запущена наша умная система оплаты. "
            "Активируйте Premium тарифы и создавайте неограниченное количество тестов!\n\n"
            "👇 Нажмите кнопку ниже, чтобы открыть приложение!"
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
        "quiz_ready": "📝 Успешно подготовлено {count} тестовых вопросов по материалу {title}!",
    },
    "en": {
        "welcome": (
            "👋 Hello, {name}! Welcome to Quiz Pilot Super Mini App.\n\n"
            "🚀 New Update:\n🔒 Our smart payment system is now live. "
            "Activate Premium plans to generate unlimited quizzes!\n\n"
            "👇 Tap the button below to open the app!"
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
        "quiz_ready": "📝 A total of {count} quiz questions for {title} have been successfully generated!",
    }
}

def get_user_lang(user_id: int) -> str:
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")

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
        last_active INTEGER DEFAULT 0)""")

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
        active INTEGER DEFAULT 1)""")

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
        created_at INTEGER NOT NULL,
        active INTEGER DEFAULT 1)""")

    teacher_migrations = {
        "teacher_sessions": {
            "group_id": "TEXT DEFAULT ''",
            "variant_code": "TEXT DEFAULT ''",
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

    for table_name, updates in {
        "teacher_groups": [("description", "''"), ("active", "1")],
        "teacher_assignments": [("variant_code", "''"), ("title", "''"), ("due_at", "0"), ("active", "1")],
        "teacher_sessions": [("group_id", "''"), ("variant_code", "''")],
    }.items():
        for col, value in updates:
            try:
                cursor.execute(f"UPDATE {table_name} SET {col}={value} WHERE {col} IS NULL")
            except Exception:
                pass

    conn.commit()
    conn.close()


init_db()


class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="Jami 4 ta variant ro'yxati (Variant harflarisiz)")
    correct_index: int = Field(description="To'g'ri javob indeks (0 dan 3 gacha)")
    explanation: str = Field(description="Ushbu javob nega to'g'riligini tushuntiruvchi qisqa izoh")


class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")


class ProgressUpdateRequest(BaseModel):
    quiz_id: str
    user_id: int
    correct_count: int
    percent: int


class FlashcardCreateRequest(BaseModel):
    user_id: int
    front: str
    back: str


class PaymentIntentRequest(BaseModel):
    action: str
    user_id: int
    tariff_name: Optional[str] = None
    tariff_price: Optional[str] = None
    tariff_key: Optional[str] = None


def add_user_to_db(user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
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
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
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
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
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

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")

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

        bot.send_message(user_id, msg_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"To'lov jarayonini ishga tushirishda xato: {e}")


@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_id = message.from_user.id
    add_user_to_db(user_id)
    user_lang = get_user_lang(user_id)

    welcome_text = MESSAGES[user_lang]["welcome"].format(name=message.from_user.first_name)

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
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


# --- ADMIN SUPPORT: murojaat va javob ---
@bot.message_handler(content_types=["text"], func=lambda message: message.from_user.id in support_waiting_users or message.from_user.id in support_reply_targets)
def handle_support_text(message):
    user_id = message.from_user.id
    if ADMIN_ID and user_id == ADMIN_ID and user_id in support_reply_targets:
        target_user_id = support_reply_targets.pop(user_id)
        try:
            bot.send_message(target_user_id, f"💬 Admin javobi:\n\n{message.text}")
            bot.send_message(user_id, MESSAGES.get(get_user_lang(user_id), MESSAGES["uz"])["support_reply_sent"])
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
        markup.add(telebot.types.InlineKeyboardButton(MESSAGES[user_lang]["support_reply_btn"], callback_data=f"support_reply:{user_id}"))
        text = (f"{MESSAGES[user_lang]['support_admin_title']}\n\n"
                f"👤 {first_name}\n🔗 Username: {username}\n🆔 Telegram ID: {user_id}\n🌐 Til: {user_lang.upper()}\n\n💬 {message.text}")
        try:
            target_admin = ADMIN_ID if ADMIN_ID else user_id
            bot.send_message(target_admin, text, reply_markup=markup)
            bot.send_message(user_id, MESSAGES[user_lang]["support_sent"])
        except Exception as e:
            logging.error(f"Admin murojaatini yuborishda xato: {e}")
            bot.send_message(user_id, "❌ Murojaatni yuborishda xatolik yuz berdi.")
        return

@bot.callback_query_handler(func=lambda call: call.data.startswith("support_reply:"))
def handle_support_reply_callback(call):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Siz administrator emassiz!", show_alert=True)
        return
    try:
        target_user_id = int(call.data.split(":", 1)[1])
        support_reply_targets[call.from_user.id] = target_user_id
        bot.answer_callback_query(call.id)
        bot.send_message(call.from_user.id, MESSAGES.get(get_user_lang(call.from_user.id), MESSAGES["uz"])["support_reply_prompt"])
    except Exception as e:
        logging.error(f"Admin reply callback xatosi: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.", show_alert=True)

# --- ISHONCHLI TO'LOV CHEKI QABUL QILISH (STABLE PHOTO HANDLER) ---
@bot.message_handler(content_types=["photo"])
def handle_receipt_photo(message):
    user_id = message.from_user.id
    user_lang = get_user_lang(user_id)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT tx_id, tariff_name, tariff_price FROM payments WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    pending_pay = cursor.fetchone()
    conn.close()

    if not pending_pay:
        return

    tx_id, tariff_name, tariff_price = pending_pay

    username = f"@{message.from_user.username}" if message.from_user.username else "Mavjud emas"
    first_name = message.from_user.first_name
    file_id = message.photo[-1].file_id

    admin_markup = telebot.types.InlineKeyboardMarkup()
    btn_approve = telebot.types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"p_app_{tx_id}_{user_id}")
    btn_reject = telebot.types.InlineKeyboardButton("❌ Rad etish", callback_data=f"p_rej_{tx_id}_{user_id}")
    admin_markup.row(btn_approve, btn_reject)

    admin_text = (
        f"💰 YANGI TO'LOV SO'ROVI!\n\n"
        f"👤 Foydalanuvchi: {first_name} ({username})\n"
        f"🆔 Telegram ID: {user_id}\n"
        f"🌐 Til kodi: {user_lang.upper()}\n"
        f"📦 Tanlangan Tarif: {tariff_name}\n"
        f"💵 To'lov Summasi: {tariff_price}\n"
        f"🧩 Tranzaksiya ID: {tx_id}\n\n"
        f"Chek to'g'riligini tekshiring va pastdagi tugmalardan birini bosing."
    )

    target_admin = ADMIN_ID if ADMIN_ID else user_id

    try:
        bot.send_photo(
            target_admin,
            file_id,
            caption=admin_text,
            parse_mode="Markdown",
            reply_markup=admin_markup,
        )
        bot.send_message(message.chat.id, MESSAGES[user_lang]["receipt_received"])
    except Exception as e:
        logging.error(f"Admin ga rasm yuborishda xatolik yuz berdi: {e}")
        bot.send_message(message.chat.id, MESSAGES[user_lang]["receipt_error"])


@bot.callback_query_handler(func=lambda call: call.data.startswith("p_"))
def handle_admin_decision(call):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Siz administrator emassiz!", show_alert=True)
        return

    parts = call.data.split("_")
    action = parts[1]
    tx_id = parts[2]
    user_id = int(parts[3])
    user_lang = get_user_lang(user_id)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("SELECT status, tariff_name FROM payments WHERE tx_id = ?", (tx_id,))
    pay_row = cursor.fetchone()

    if not pay_row or pay_row[0] != "pending":
        bot.answer_callback_query(call.id, "Bu so'rov allaqachon ko'rib chiqilgan!", show_alert=True)
        conn.close()
        return

    tariff_name = pay_row[1]

    if action == "app":
        current_time = int(time.time())
        plan_key = get_plan_key(tariff_name)
        duration = TARIFFS.get(plan_key, {}).get("duration", 30 * 24 * 3600)
        new_until = current_time + duration

        cursor.execute("UPDATE payments SET status = 'approved' WHERE tx_id = ?", (tx_id,))
        cursor.execute(
            "UPDATE users SET status = ?, plan_key = ?, premium_until = ? WHERE user_id = ?",
            (f"PRO ({tariff_name})", plan_key, new_until, user_id),
        )
        conn.commit()

        bot.answer_callback_query(call.id, "To'lov tasdiqlandi!")
        try:
            bot.edit_message_caption(
                f"✅ TASDIQLANDI!\nTranzaksiya: {tx_id}\nFoydalanuvchi: {user_id}",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass

        try:
            bot.send_message(
                user_id,
                MESSAGES[user_lang]["payment_approved"].format(tariff_name=tariff_name),
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga tasdiq xabarini yuborishda xato: {e}")

    elif action == "rej":
        cursor.execute("UPDATE payments SET status = 'rejected' WHERE tx_id = ?", (tx_id,))
        conn.commit()

        bot.answer_callback_query(call.id, "To'lov rad etildi!")
        try:
            bot.edit_message_caption(
                f"❌ RAD ETILDI!\nTranzaksiya: {tx_id}\nFoydalanuvchi: {user_id}",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass

        try:
            bot.send_message(user_id, MESSAGES[user_lang]["payment_rejected"])
        except Exception as e:
            logging.error(f"Foydalanuvchiga rad xabarini yuborishda xato: {e}")

    conn.close()
