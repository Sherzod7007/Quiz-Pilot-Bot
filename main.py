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
        "support_prompt": "💬 Связаться с администратором. Напишите ваш вопрос или проблему здесь. Сообщение будет отправлено администратору.",
        "support_sent": "✅ Ваше обращение отправлено администратору. Ожидайте ответа.",
        "support_admin_title": "📩 Новое обращение",
        "support_reply_btn": "✉️ Ответить",
        "support_reply_prompt": "✍️ Напишите ответ. Он будет отправлен пользователю.",
        "support_reply_sent": "✅ Ответ отправлен пользователю.",
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
        "support_prompt": "💬 Contact Admin. Write your question or problem here. Your message will be sent to the administrator.",
        "support_sent": "✅ Your message has been sent to the administrator. Please wait for a reply.",
        "support_admin_title": "📩 New support request",
        "support_reply_btn": "✉️ Reply",
        "support_reply_prompt": "✍️ Write your reply. It will be sent to the user.",
        "support_reply_sent": "✅ Reply sent to the user.",
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

    # Eski Railway/SQLite bazalarida Teacher jadvallari avvalgi versiyadan qolgan
    # bo‘lishi mumkin. CREATE TABLE IF NOT EXISTS mavjud jadvalga yangi ustunlarni
    # qo‘shmaydi, shuning uchun xavfsiz migration qilamiz. Bu faqat yetishmayotgan
    # Teacher ustunlarini qo‘shadi va boshqa funksiyalarga tegmaydi.
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

    # Eski yozuvlardagi NULL qiymatlar Teacher endpointlari uchun xavfsiz qiymatga o‘tkaziladi.
    for table_name, updates in {
        "teacher_groups": [("description", "''"), ("active", "1")],
        "teacher_assignments": [("variant_code", "''"), ("title", "''"), ("due_at", "0"), ("active", "1")],
        "teacher_sessions": [("group_id", "''"), ("variant_code", "'" + "'" + "'")],
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

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")

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
        return  # Kutilayotgan to'lov yo'q bo'lsa javob berilmaydi

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
            "UPDATE users SET status = ?, plan_key = ?, premium_until = ? WHERE user_id = ?",
            (f"PRO ✨ ({display_name})", plan_key, premium_until_timestamp, user_id),
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


# --- FASTAPI ENDPOINTS ---
app = FastAPI()

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


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    response = templates.TemplateResponse("index.html", {"request": request})
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


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
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, plan_key, free_used, public_free_used, flashcard_free_used, premium_until, created_at "
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
            "public_remaining": FREE_PUBLIC_LIMIT,
            "flashcard_remaining": FREE_FLASHCARD_LIMIT,
            "quiz_remaining": FREE_QUIZ_LIMIT,
            "plan_key": "",
            "is_paid": False,
            "is_teacher": False,
        }

    user_status = row["status"] or "Oddiy foydalanuvchi"
    plan_key = row["plan_key"] or get_plan_key(user_status)
    premium_until = row["premium_until"] or 0
    free_used = row["free_used"] if row["free_used"] is not None else 0
    public_free_used = row["public_free_used"] if row["public_free_used"] is not None else 0
    flashcard_free_used = row["flashcard_free_used"] if row["flashcard_free_used"] is not None else 0
    created_at = row["created_at"] or int(time.time())
    now = int(time.time())

    if is_active_paid_status(user_status, premium_until):
        pass
    elif "PRO" in user_status and premium_until > 0 and now > premium_until:
        cursor.execute("UPDATE users SET status = 'Oddiy foydalanuvchi', plan_key = '', premium_until = 0 WHERE user_id = ?", (user_id,))
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
        "public_remaining": max(0, FREE_PUBLIC_LIMIT - public_free_used),
        "flashcard_remaining": max(0, FREE_FLASHCARD_LIMIT - flashcard_free_used),
        "quiz_remaining": max(0, FREE_QUIZ_LIMIT - free_used),
        "plan_key": plan_key,
        "is_paid": is_paid,
        "is_teacher": is_teacher,
    }


@app.post("/api/create-quiz-web")
async def create_quiz_web(
    user_id: int = Form(...),
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    quiz_title: Optional[str] = Form(None),
):
    add_user_to_db(user_id)
    user_lang = get_user_lang(user_id)

    conn_check = sqlite3.connect(DB_PATH, check_same_thread=False)
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
        if "PRO" not in current_status:
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
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        file_path = os.path.join(DOWNLOADS_DIR, file.filename)
        try:
            contents = await file.read()
            if len(contents) > 0:
                with open(file_path, "wb") as f:
                    f.write(contents)
                if file.filename.endswith(".pdf"):
                    reader = PdfReader(file_path)
                    raw_text = "".join(
                        [p.extract_text() + "\n" for p in reader.pages if p.extract_text()]
                    )
                    auto_title = file.filename.replace(".pdf", "")
                elif file.filename.endswith(".docx"):
                    doc = docx.Document(file_path)
                    raw_text = "\n".join([p.text for p in doc.paragraphs])
                    auto_title = file.filename.replace(".docx", "")
        except Exception as e:
            logging.error(f"Foydalanuvchi fayl yuklashda xato: {e}")

    if not raw_text.strip() and text:
        raw_text = text
        auto_text_clean = text.replace("\n", " ").strip()
        auto_title = (
            auto_text_clean[:18] + "..."
            if len(auto_text_clean) > 18
            else auto_text_clean
        )

    if not raw_text.strip():
        return {"status": "error", "message": "Matn yoki darslikni o'qib bo'lmadi."}

    # Gemini SDK chaqiruvi sinxron bo'lgani uchun uni alohida threadga chiqaramiz.
    # Shu bilan boshqa foydalanuvchilarning WebApp requestlari event loopni bloklamaydi.
    quiz_json_raw = await asyncio.to_thread(generate_quiz_from_gemini, raw_text)
    if not quiz_json_raw:
        if free_slot_reserved:
            try:
                conn_restore = sqlite3.connect(DB_PATH, check_same_thread=False)
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
        if not items:
            return {
                "status": "error",
                "message": "AI savollar ro'yxatini bo'sh qaytardi.",
            }

        quiz_id = f"q_{uuid.uuid4().hex}"
        final_title = (
            quiz_title.strip() if (quiz_title and quiz_title.strip()) else auto_title
        )

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
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

        try:
            q_ready_msg = MESSAGES[user_lang]["quiz_ready"].format(
                title=final_title[:30],
                count=len(items)
            )
            bot.send_message(user_id, q_ready_msg)
        except Exception as e:
            logging.error(f"Telegram xabari yuborilmadi: {e}")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def generate_quiz_from_gemini(extracted_text):
    global current_key_index

    if not GOOGLE_API_KEYS:
        logging.error("GOOGLE_API_KEYS topilmadi yoki bo'sh!")
        return None

    system_instruction = """You are an advanced AI quiz generator.
CRITICAL RULES:
1. LANGUAGE RULE: Detect the language of the provided text. You MUST generate the questions, choices, and explanations in the EXACT SAME language as the input text.
2. QUESTION COUNT RULE: Look at the input text. If the user provided a strict list of questions, you MUST ONLY extract and format THOSE EXACT questions into the quiz structure. If it's a huge continuous textbook, you can generate up to 40-50 questions maximum."""

    total_keys = len(GOOGLE_API_KEYS)

    # Har bir yangi test yaratish jarayoniga navbatdagi key beriladi.
    # Lock parallel requestlar bir xil start_index olishini oldini oladi.
    with key_lock:
        start_index = current_key_index
        current_key_index = (current_key_index + 1) % total_keys

    # 7 ta key bo'lsa, bir vaqtning o'zida maksimal 7 ta Gemini request.
    # Qolgan requestlar navbat kutadi va serverni birdaniga bosib yubormaydi.
    with gemini_semaphore:
        for i in range(total_keys):
            key_idx = (start_index + i) % total_keys
            api_key = GOOGLE_API_KEYS[key_idx].strip()

            if not api_key:
                continue

            try:
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=extracted_text[:80000],
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        response_schema=QuizResponse,
                        temperature=0.2,
                    ),
                )

                if response and response.text:
                    logging.info(
                        f"Muvaffaqiyatli AI so'rovi! Ishlatilgan kalit indeksi: [{key_idx}]"
                    )
                    return response.text

            except Exception as e:
                logging.warning(
                    f"API kalit [{key_idx}] ishlamadi yoki limit tugadi. "
                    f"Xatolik: {e}. Keyingi kalitga o'tilmoqda..."
                )

    logging.error("Barcha API kalitlar bo'yicha so'rovlar muvaffaqiyatsiz bo'ldi.")
    return None


@app.get("/api/heartbeat")
def user_heartbeat(user_id: int):
    add_user_to_db(user_id)
    update_user_activity(user_id)
    return {"status": "ok", "active_users": get_users_count()}


@app.get("/api/quizzes")
def get_user_quizzes(user_id: int):
    add_user_to_db(user_id)
    total_users = get_users_count()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
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
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
    public_remaining = max(0, FREE_PUBLIC_LIMIT - public_free_used)

    cursor.execute("SELECT id, title, total, created_at FROM quizzes WHERE is_public = 1 ORDER BY created_at DESC LIMIT 50")
    rows = cursor.fetchall()
    conn.close()

    quizzes = [
        {
            "id": r["id"],
            "title": r["title"],
            "total": r["total"],
            "created_at": r["created_at"],
            "locked": (not is_paid and public_remaining <= 0),
        }
        for r in rows
    ]
    return {
        "status": "ok",
        "quizzes": quizzes,
        "is_paid": is_paid,
        "public_free_used": public_free_used,
        "public_remaining": public_remaining,
        "public_limit": FREE_PUBLIC_LIMIT,
    }


@app.get("/api/public-quiz-detail")
def get_public_quiz_detail(quiz_id: str, user_id: int):
    add_user_to_db(user_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT status, premium_until, public_free_used, created_at FROM users WHERE user_id = ?",
        (user_id,),
    )
    u = cursor.fetchone()

    now = int(time.time())
    is_paid = bool(u and is_active_paid_status(u["status"] or "", u["premium_until"] or 0))

    # 30 kunlik bepul davr tugagan bo'lsa, uchala bepul hisoblagichni reset qilamiz.
    if u and now - (u["created_at"] or now) >= 30 * 24 * 3600 and not is_paid:
        cursor.execute(
            "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()
        public_free_used = 0
    else:
        public_free_used = (u["public_free_used"] or 0) if u else 0

    # Premium / Teacher: cheksiz.
    if not is_paid:
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

    cursor.execute("SELECT quiz_json, is_public FROM quizzes WHERE id = ?", (quiz_id,))
    row = cursor.fetchone()
    conn.close()

    if not row or row["is_public"] != 1:
        raise HTTPException(status_code=404, detail=teacher_text(owner_id, "quiz_not_found"))

    return {"status": "ok", "quiz_json": json.loads(row["quiz_json"])}


@app.post("/api/toggle-public")
def toggle_public(quiz_id: str, user_id: int, is_public: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(
        "UPDATE quizzes SET is_public = ? WHERE id = ? AND user_id = ?",
        (is_public, quiz_id, user_id),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/api/set-language")
def set_language(user_id: int, lang: str):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
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
    user_id: int
    quiz_id: str
    variants: List[str] = Field(default_factory=lambda: ["A", "B", "C", "D"])


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
    user_id: int
    name: str
    description: str = ""


class TeacherGroupJoinRequest(BaseModel):
    group_id: str
    user_id: int
    first_name: str = ""
    username: str = ""


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
        conn.execute(
            "INSERT INTO teacher_groups (id,owner_id,name,description,created_at,active) VALUES (?,?,?,?,?,1)",
            (gid, req.user_id, name, description, now),
        )
    teacher_db_write(_write)
    return {"status":"ok", "group_id":gid, "name":name}

@app.get("/api/teacher/groups")
def teacher_groups(user_id: int):
    require_teacher(user_id)
    def _read(conn):
        cur = conn.cursor()
        cur.execute(
            "SELECT g.id,g.name,g.description,g.created_at,g.active,COUNT(*) AS member_count "
            "FROM teacher_groups g LEFT JOIN teacher_group_members m ON m.group_id=g.id "
            "WHERE g.owner_id=? GROUP BY g.id ORDER BY g.created_at DESC",
            (user_id,),
        )
        return cur.fetchall()
    rows = teacher_db_read(_read)
    return {"status":"ok", "groups":[dict(r) for r in rows]}

@app.post("/api/teacher/groups/join")
def teacher_join_group(req: TeacherGroupJoinRequest):
    def _write(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id,active FROM teacher_groups WHERE id=?", (req.group_id,))
        group = cur.fetchone()
        if not group or not group["active"]:
            raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "group_not_found"))
        now = int(time.time())
        cur.execute(
            "INSERT OR IGNORE INTO teacher_group_members (group_id,user_id,first_name,username,joined_at) VALUES (?,?,?,?,?)",
            (req.group_id, req.user_id, (req.first_name or "")[:100], (req.username or "")[:100], now),
        )
    teacher_db_write(_write)
    return {"status":"ok", "group_id":req.group_id}

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
    user_id: int
    group_id: str
    quiz_id: str
    variant_code: str = ""
    title: str = ""
    due_at: int = 0


@app.post("/api/teacher/assignments")
def teacher_create_assignment(req: TeacherAssignmentCreateRequest):
    require_teacher(req.user_id)
    group_id=(req.group_id or "").strip(); quiz_id=(req.quiz_id or "").strip()
    if not group_id or not quiz_id:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "group_required"))
    code=(req.variant_code or "").strip().upper()
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
        cur.execute("INSERT INTO teacher_assignments (id,owner_id,group_id,quiz_id,variant_code,title,due_at,created_at,active) VALUES (?,?,?,?,?,?,?,?,1)",(aid,req.user_id,group_id,quiz_id,code,title,due_at,now))
        return {"id":aid,"group_id":group_id,"quiz_id":quiz_id,"variant_code":code,"title":title,"due_at":due_at,"created_at":now,"active":True,"group_name":group["name"],"quiz_title":quiz["title"]}
    assignment = teacher_db_write(_write)
    return {"status":"ok","assignment":assignment}

@app.get("/api/teacher/assignments")
def teacher_assignments(user_id: int):
    require_teacher(user_id)
    def _read(conn):
        conn.row_factory=sqlite3.Row; cur=conn.cursor()
        cur.execute("""SELECT a.id,a.group_id,a.quiz_id,a.variant_code,a.title,a.due_at,a.created_at,a.active,
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
    codes = list(TEACHER_VARIANT_CODES) if int(all_variants) else [variant.upper()]
    for c in codes:
        if c not in TEACHER_VARIANT_CODES: raise HTTPException(status_code=400, detail=teacher_text(user_id, "variant_required"))

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", row["title"] or "quiz")[:40]
    variant_payloads = [(c, _get_teacher_variant(quiz_id, user_id, c)) for c in codes]

    def answer_key(payload):
        return [chr(65 + int(q.get("correct_index", 0))) for q in payload.get("quizzes", [])]

    if fmt == "xlsx":
        path=os.path.join(DOWNLOADS_DIR, f"{safe}_teacher_{'_'.join(codes)}.xlsx")
        wb=Workbook(); ws=wb.active; ws.title="Testlar"
        ws.append(["Variant","№","Savol","A","B","C","D","To'g'ri javob"])
        for code,payload in variant_payloads:
            for i,q in enumerate(payload.get("quizzes",[]),1):
                opts=(q.get("options") or [])+['','','','']
                ws.append([code,i,q.get("question",""),opts[0],opts[1],opts[2],opts[3],chr(65+int(q.get("correct_index",0)))])
        ws.freeze_panes="A2"; wb.save(path)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

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
        story=[Paragraph(row["title"],title_style),Spacer(1,8),Paragraph("Test kaliti bilan | Variantlar: " + ", ".join(codes),q_style),Spacer(1,10)]
        for code,payload in variant_payloads:
            story.append(Paragraph(f"<b>Variant {code}</b>", q_style)); story.append(Spacer(1,5))
            for i,q in enumerate(payload.get("quizzes",[]),1):
                story.append(Paragraph(f"{i}. {q.get('question','')}",q_style))
                opts=(q.get("options") or [])+['','','','']
                for j in range(4): story.append(Paragraph(f"{chr(65+j)}) {opts[j]}",q_style))
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
            "created_at": "INTEGER",
            "active": "INTEGER",
        },
        "teacher_sessions": {
            "id": "TEXT",
            "owner_id": "INTEGER",
            "quiz_id": "TEXT",
            "code": "TEXT",
            "duration_minutes": "INTEGER",
            "created_at": "INTEGER",
            "expires_at": "INTEGER",
            "active": "INTEGER",
            "group_id": "TEXT",
            "variant_code": "TEXT",
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
            due_at INTEGER DEFAULT 0, created_at INTEGER NOT NULL,
            active INTEGER DEFAULT 1)""",
        "teacher_sessions": """CREATE TABLE IF NOT EXISTS teacher_sessions (
            id TEXT PRIMARY KEY, owner_id INTEGER, quiz_id TEXT, code TEXT UNIQUE,
            duration_minutes INTEGER DEFAULT 30, created_at INTEGER,
            expires_at INTEGER, active INTEGER DEFAULT 1,
            group_id TEXT DEFAULT '', variant_code TEXT DEFAULT '')""",
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
        "teacher_sessions": {"group_id": "", "variant_code": "", "active": 1},
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
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
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
    user_id: int
    quiz_id: str
    duration_minutes: int = 30
    variant_code: str = ""
    group_id: str = ""


@app.post("/api/teacher/create-session")
def teacher_create_session(req: TeacherSessionCreateRequest):
    """Teacher guruh sessiyasini yaratish.

    Bu endpoint avvalgi versiyadagi to'g'ridan-to'g'ri SQLite SELECT/INSERT
    aralashmasini bitta teacher_db_write oqimiga yig'adi. Shu sabab eski
    teacher_* sxemasi migrationdan o'tgach ham guruh testi barqaror yaratiladi.
    Oddiy testda variant bo'sh qoladi; A/B/C/D tanlansa saqlangan variant
    ishlatiladi.
    """
    require_teacher(req.user_id)
    duration = max(5, min(int(req.duration_minutes or 30), 180))
    variant_code = (req.variant_code or "").strip().upper()
    if variant_code and variant_code not in TEACHER_VARIANT_CODES:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "variant_required"))

    # Quiz va savollar haqiqatan ham shu teacherga tegishli ekanini oldindan
    # tekshiramiz. Variant tanlangan bo'lsa, variant ham shu yerda tayyorlanadi.
    quiz_row, _items = _load_quiz_items(req.quiz_id, req.user_id)
    if variant_code:
        _get_teacher_variant(req.quiz_id, req.user_id, variant_code)

    group_id = (req.group_id or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "select_group"))

    def _write(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Guruh teacherning o'ziga tegishli va faol bo'lishi shart.
        cur.execute(
            "SELECT id, name FROM teacher_groups WHERE id=? AND owner_id=? AND active=1",
            (group_id, req.user_id),
        )
        group = cur.fetchone()
        if not group:
            raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "group_not_found"))

        sid = f"ts_{uuid.uuid4().hex[:10]}"
        now = int(time.time())
        expires = now + duration * 60

        # Juda kam ehtimol bo'lsa ham kod to'qnashsa qayta generatsiya qilamiz.
        code = ""
        for _ in range(5):
            candidate = uuid.uuid4().hex[:8].upper()
            cur.execute("SELECT 1 FROM teacher_sessions WHERE code=? LIMIT 1", (candidate,))
            if not cur.fetchone():
                code = candidate
                break
        if not code:
            raise HTTPException(status_code=500, detail=teacher_text(req.user_id, "server_error"))

        # Railway'dagi eski Teacher bazasida teacher_sessions jadvalida
        # owner_id bilan birga NOT NULL teacher_id ham mavjud bo'lishi mumkin.
        # V4 faqat owner_id yozgani uchun aynan shu holatda SQLite:
        # "NOT NULL constraint failed: teacher_sessions.teacher_id" beradi.
        # Yangi va eski sxemalarni bir xil ishlatish uchun mavjud ustunlarni
        # tekshirib, kerak bo'lsa ikkala identifikatorni ham birga yozamiz.
        session_columns = {r[1] for r in cur.execute("PRAGMA table_info(teacher_sessions)").fetchall()}
        if "teacher_id" in session_columns:
            cur.execute(
                """INSERT INTO teacher_sessions
                   (id, owner_id, teacher_id, quiz_id, code, duration_minutes, created_at,
                    expires_at, active, group_id, variant_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (sid, req.user_id, req.user_id, req.quiz_id, code, duration, now, expires, group_id, variant_code),
            )
        else:
            cur.execute(
                """INSERT INTO teacher_sessions
                   (id, owner_id, quiz_id, code, duration_minutes, created_at,
                    expires_at, active, group_id, variant_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (sid, req.user_id, req.quiz_id, code, duration, now, expires, group_id, variant_code),
            )
        return {
            "session_id": sid,
            "code": code,
            "expires_at": expires,
            "quiz_title": quiz_row["title"],
            "variant_code": variant_code,
            "group_id": group_id,
            "group_name": group["name"],
        }

    data = teacher_db_write(_write)
    return {"status": "ok", **data}

@app.get("/api/teacher-sessions")
def teacher_sessions(user_id: int):
    require_teacher(user_id)
    def _read(conn):
        conn.row_factory = sqlite3.Row
        cur=conn.cursor()
        cur.execute("""SELECT s.id, s.code, s.quiz_id, q.title, s.duration_minutes, s.created_at, s.expires_at, s.active,
                             COALESCE(s.group_id,'') AS group_id, COALESCE(s.variant_code,'') AS variant_code
                      FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id
                      WHERE s.owner_id=? ORDER BY s.created_at DESC LIMIT 30""", (user_id,))
        return cur.fetchall()
    rows=teacher_db_read(_read); now=int(time.time())
    result=[]
    for r in rows:
        active=bool(r["active"] and r["expires_at"]>=now)
        result.append({**dict(r), "active": active})
    return {"status":"ok", "sessions":result}

@app.get("/api/teacher-session")
def teacher_session_info(code: str, user_id: int):
    def _read(conn):
        conn.row_factory=sqlite3.Row; cur=conn.cursor()
        cur.execute("SELECT s.id, s.owner_id, s.quiz_id, s.code, s.duration_minutes, s.expires_at, s.active, COALESCE(s.variant_code,'') AS variant_code, COALESCE(s.group_id,'') AS group_id, q.title, q.total FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.code=?", (code.upper(),))
        return cur.fetchone()
    row=teacher_db_read(_read)
    if not row: raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    if not row["active"] or int(time.time())>row["expires_at"]: raise HTTPException(status_code=410, detail=teacher_text(user_id, "session_expired"))
    return {"status":"ok", "session_id":row["id"], "quiz_id":row["quiz_id"], "title":row["title"], "total":row["total"], "expires_at":row["expires_at"], "duration_minutes":row["duration_minutes"], "variant_code":row["variant_code"], "group_id":row["group_id"]}

def _session_owner_id(session_id: str, user_id: int):
    conn=teacher_db_connect(); cur=conn.cursor()
    cur.execute("SELECT owner_id FROM teacher_sessions WHERE id=?", (session_id,)); row=cur.fetchone(); conn.close()
    if not row: raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    return row[0]


@app.get("/api/teacher-session-quiz")
def teacher_session_quiz(session_id: str, user_id: int):
    conn=teacher_db_connect(); conn.row_factory=sqlite3.Row; cur=conn.cursor()
    cur.execute("SELECT s.quiz_id, s.expires_at, s.active, COALESCE(s.variant_code,'') AS variant_code, q.quiz_json FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.id=?", (session_id,))
    row=cur.fetchone(); conn.close()
    if not row: raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    if not row["active"] or int(time.time())>row["expires_at"]: raise HTTPException(status_code=410, detail=teacher_text(user_id, "session_expired"))
    if row["variant_code"]:
        payload = _get_teacher_variant(row["quiz_id"], _session_owner_id(session_id, user_id), row["variant_code"])
    else:
        payload = json.loads(row["quiz_json"])
    return {"status":"ok", "quiz_id":row["quiz_id"], "quiz_json":payload, "variant_code":row["variant_code"]}


class TeacherSubmitRequest(BaseModel):
    session_id: str
    user_id: int
    first_name: str = ""
    username: str = ""
    score: int
    total: int
    percent: int


@app.post("/api/teacher-submit")
def teacher_submit(req: TeacherSubmitRequest):
    conn=teacher_db_connect(); conn.row_factory=sqlite3.Row; cur=conn.cursor()
    cur.execute("SELECT expires_at, active FROM teacher_sessions WHERE id=?", (req.session_id,)); s=cur.fetchone()
    if not s: conn.close(); raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "session_not_found"))
    now=int(time.time())
    if not s["active"] or now>s["expires_at"]: conn.close(); raise HTTPException(status_code=410, detail=teacher_text(req.user_id, "session_expired"))
    cur.execute("SELECT id FROM teacher_participants WHERE session_id=? AND user_id=? ORDER BY id LIMIT 1", (req.session_id, req.user_id))
    existing_participant = cur.fetchone()
    values = (req.first_name[:100], req.username[:100], req.score, req.total, req.percent, now, now)
    if existing_participant:
        cur.execute("UPDATE teacher_participants SET first_name=?, username=?, score=?, total=?, percent=?, finished_at=? WHERE id=?", (*values[:5], values[6], existing_participant[0]))
    else:
        cur.execute("INSERT INTO teacher_participants (session_id,user_id,first_name,username,score,total,percent,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)", (req.session_id,req.user_id,req.first_name[:100],req.username[:100],req.score,req.total,req.percent,now,now))
    conn.commit(); conn.close(); return {"status":"ok"}


@app.get("/api/teacher-session-results")
def teacher_session_results(session_id: str, user_id: int):
    require_teacher(user_id)
    def _read(conn):
        conn.row_factory=sqlite3.Row; cur=conn.cursor()
        cur.execute("SELECT s.quiz_id, q.title, s.code, s.expires_at FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.id=? AND s.owner_id=?", (session_id,user_id)); s=cur.fetchone()
        if not s:
            raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
        cur.execute("SELECT first_name, username, score, total, percent, finished_at FROM teacher_participants WHERE session_id=? ORDER BY percent DESC, score DESC, finished_at ASC", (session_id,)); rows=cur.fetchall()
        return dict(s), rows
    s, rows=teacher_db_read(_read)
    return {"status":"ok", "session":{"id":session_id,"code":s["code"],"quiz_title":s["title"],"expires_at":s["expires_at"]}, "participants":[dict(r) for r in rows]}

def _teacher_export_rows(session_id, owner_id):
    require_teacher(owner_id)
    conn=teacher_db_connect(); conn.row_factory=sqlite3.Row; cur=conn.cursor()
    cur.execute("SELECT s.code, q.title FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.id=? AND s.owner_id=?", (session_id,owner_id)); s=cur.fetchone()
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
    fmt=format.lower(); safe=re.sub(r"[^A-Za-z0-9_-]+", "_", s["title"] or "quiz")[:40]
    if fmt=="xlsx":
        path=os.path.join(DOWNLOADS_DIR,f"{safe}_{s['code']}.xlsx")
        wb=Workbook(); ws=wb.active; ws.title="Natijalar"
        ws.append(["№","O'quvchi","Username","To'g'ri","Jami","Foiz"]);
        for i,r in enumerate(rows,1): ws.append([i,r["first_name"],r["username"],r["score"],r["total"],r["percent"]])
        ws.freeze_panes="A2"; wb.save(path)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt=="docx":
        path=os.path.join(DOWNLOADS_DIR,f"{safe}_{s['code']}.docx")
        doc=Document(); doc.add_heading(s["title"], level=1); doc.add_paragraph(f"Sessiya: {s['code']}")
        table=doc.add_table(rows=1, cols=6); hdr=table.rows[0].cells
        for i,t in enumerate(["№","O'quvchi","Username","To'g'ri","Jami","Foiz"]): hdr[i].text=t
        for i,r in enumerate(rows,1):
            cells=table.add_row().cells
            vals=[i,r["first_name"],r["username"],r["score"],r["total"],f"{r['percent']}%"]
            for j,v in enumerate(vals): cells[j].text=str(v)
        doc.save(path); return FileResponse(path, filename=os.path.basename(path), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if fmt=="pdf":
        path=os.path.join(DOWNLOADS_DIR,f"{safe}_{s['code']}.pdf")
        reg,bld=_register_pdf_font(); font="QuizPilotFont" if reg else "Helvetica"; bold="QuizPilotFontBold" if bld else font
        doc=SimpleDocTemplate(path,pagesize=A4,rightMargin=28,leftMargin=28,topMargin=28,bottomMargin=28)
        styles=getSampleStyleSheet(); title_style=ParagraphStyle("qp_title",parent=styles["Title"],fontName=bold,fontSize=16,alignment=TA_CENTER)
        body_style=ParagraphStyle("qp_body",parent=styles["BodyText"],fontName=font,fontSize=8)
        story=[Paragraph(s["title"],title_style),Spacer(1,8),Paragraph(f"Session: {s['code']}",body_style),Spacer(1,8)]
        data=[["№","O'quvchi","Username","To'g'ri","Jami","Foiz"]]
        for i,r in enumerate(rows,1): data.append([str(i),str(r["first_name"]),str(r["username"]),str(r["score"]),str(r["total"]),f"{r['percent']}%"] )
        table=Table(data,repeatRows=1,colWidths=[24,170,100,45,40,45]); table.setStyle(TableStyle([("FONTNAME",(0,0),(-1,-1),font),("FONTNAME",(0,0),(-1,0),bold),("FONTSIZE",(0,0),(-1,-1),8),("GRID",(0,0),(-1,-1),0.4,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("VALIGN",(0,0),(-1,-1),"MIDDLE")])); story.append(table); doc.build(story)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/pdf")
    raise HTTPException(status_code=400, detail=teacher_text(user_id, "format_invalid"))


@app.get("/api/flashcards")
def get_flashcards(user_id: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
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
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
    if not is_paid:
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
    return {"status": "ok"}


@app.delete("/api/delete-flashcard")
def delete_flashcard(card_id: str, user_id: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(
        "DELETE FROM flashcards WHERE id = ? AND user_id = ?", (card_id, user_id)
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/quiz-detail")
def get_quiz_detail(quiz_id: str):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("SELECT quiz_json FROM quizzes WHERE id = ?", (quiz_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"status": "ok", "quiz_json": json.loads(row["quiz_json"])}
    raise HTTPException(status_code=404, detail="Test topilmadi")


@app.post("/api/update-progress")
def update_progress(data: ProgressUpdateRequest):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(
        "UPDATE quizzes SET answered = total, last_score = ?, last_percent = ?"
        " WHERE id = ? AND user_id = ?",
        (data.correct_count, data.percent, data.quiz_id, data.user_id),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/api/delete-quiz")
def delete_quiz(quiz_id: str, user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute(
            "DELETE FROM quizzes WHERE id = ? AND user_id = ?", (quiz_id, user_id)
        )
        conn.commit()
        conn.close()
        return {"status": "ok", "message": "Test o'chirildi."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Xatolik.")


def start_bot_polling():
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except Exception:
            time.sleep(5)


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


@app.on_event("startup")
async def startup_event():
    threading.Thread(target=start_bot_polling, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
