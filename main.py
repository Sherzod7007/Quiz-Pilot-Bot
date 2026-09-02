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
import secrets
from copy import deepcopy
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)

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
gemini_semaphore = threading.Semaphore(max(1, min(7, len(GOOGLE_API_KEYS))))

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
DB_PATH = (
    "/data/quiz_pilot_v2.db" if os.path.exists("/data") else "quiz_pilot_v2.db"
)

# --- TARIFLAR ---
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

MESSAGES = {
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

    for col_name, col_def in [
        ("status", "TEXT DEFAULT 'Oddiy foydalanuvchi'"),
        ("free_used", "INTEGER DEFAULT 0"),
        ("premium_until", "INTEGER DEFAULT 0"),
        ("public_free_used", "INTEGER DEFAULT 0"),
        ("flashcard_free_used", "INTEGER DEFAULT 0"),
        ("plan_key", "TEXT DEFAULT ''"),
        ("last_active", "INTEGER DEFAULT 0")
    ]:
        if col_name not in columns:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def};")
            except Exception:
                pass

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

def support_continue_markup(lang: str):
    lang = lang if lang in MESSAGES else "uz"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        MESSAGES[lang]["support_continue_btn"],
        callback_data="support_continue"
    ))
    return markup

@bot.message_handler(content_types=["text"], func=lambda message: message.from_user.id in support_waiting_users or message.from_user.id in support_reply_targets)
def handle_support_text(message):
    user_id = message.from_user.id
    if ADMIN_ID and user_id == ADMIN_ID and user_id in support_reply_targets:
        target_user_id = support_reply_targets.pop(user_id)
        try:
            target_lang = get_user_lang(target_user_id)
            target_messages = MESSAGES.get(target_lang, MESSAGES["uz"])
            bot.send_message(
                target_user_id,
                f"💬 {target_messages.get('support_admin_reply_title', 'Admin javobi')}:\n\n{message.text}",
                reply_markup=support_continue_markup(target_lang)
            )
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
    if ADMIN_ID and call.from_user.id == ADMIN_ID:
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
        logging.error(f"Admin support davom ettirishda xato: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("support_reply:"))
def handle_support_reply_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Ruxsat etilmagan!", show_alert=True)
        return
    target_id = int(call.data.split(":")[1])
    support_reply_targets[ADMIN_ID] = target_id
    bot.answer_callback_query(call.id)
    bot.send_message(ADMIN_ID, MESSAGES["uz"]["support_reply_prompt"])

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_pay:") or call.data.startswith("reject_pay:"))
def handle_payment_approval(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Ruxsat berilmagan!", show_alert=True)
        return

    action, tx_id = call.data.split(":")
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, tariff_name FROM payments WHERE tx_id = ?", (tx_id,))
    row = cursor.fetchone()

    if not row:
        bot.answer_callback_query(call.id, "To'lov topilmadi!", show_alert=True)
        conn.close()
        return

    user_id, tariff_name = row
    user_lang = get_user_lang(user_id)

    if action == "approve_pay":
        plan_key = get_plan_key("", tariff_name)
        duration = TARIFFS.get(plan_key, {}).get("duration", 30 * 24 * 3600)
        until_time = int(time.time()) + duration

        cursor.execute("UPDATE payments SET status = 'approved' WHERE tx_id = ?", (tx_id,))
        cursor.execute("UPDATE users SET status = 'PRO', plan_key = ?, premium_until = ? WHERE user_id = ?", (plan_key, until_time, user_id))
        conn.commit()

        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption=f"{call.message.caption}\n\n✅ TASDIQLANDI")
        bot.send_message(user_id, MESSAGES[user_lang]["payment_approved"].format(tariff_name=tariff_name))
    else:
        cursor.execute("UPDATE payments SET status = 'rejected' WHERE tx_id = ?", (tx_id,))
        conn.commit()

        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption=f"{call.message.caption}\n\n❌ RAD ETILDI")
        bot.send_message(user_id, MESSAGES[user_lang]["payment_rejected"])

    conn.close()
    bot.answer_callback_query(call.id)

@bot.message_handler(content_types=["photo"])
def handle_receipt_photo(message):
    user_id = message.from_user.id
    user_lang = get_user_lang(user_id)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT tx_id, tariff_name, tariff_price FROM payments WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return

    tx_id, tariff_name, tariff_price = row
    file_id = message.photo[-1].file_id

    bot.send_message(user_id, MESSAGES[user_lang]["receipt_received"])

    if ADMIN_ID:
        try:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(
                telebot.types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"approve_pay:{tx_id}"),
                telebot.types.InlineKeyboardButton("❌ Rad etish", callback_data=f"reject_pay:{tx_id}")
            )
            caption = (f"🧾 YANGI TO'LOV CHEKI!\n\n"
                       f"👤 Foydalanuvchi: {message.from_user.first_name} (@{message.from_user.username or 'yoq'})\n"
                       f"🆔 ID: {user_id}\n"
                       f"📦 Tarif: {tariff_name} ({tariff_price})\n"
                       f"🆔 Buyurtma ID: {tx_id}")
            bot.send_photo(ADMIN_ID, file_id, caption=caption, reply_markup=markup)
        except Exception as e:
            logging.error(f"Adminga chek yuborishda xatolik: {e}")

# --- FASTAPI APP ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/active-users-count")
def api_active_users():
    return {"active_users": get_users_count()}

@app.post("/update-activity")
def api_update_activity(data: dict):
    user_id = data.get("user_id")
    if user_id:
        update_user_activity(int(user_id))
    return {"status": "ok"}

@app.post("/create-payment-intent")
def api_payment_intent(req: PaymentIntentRequest):
    trigger_payment_flow(req.user_id, req.tariff_name, req.tariff_price, req.tariff_key)
    return {"status": "ok"}

# --- BOT THREADING ---
def run_bot():
    while True:
        try:
            bot.polling(non_stop=True, interval=1, timeout=20)
        except Exception as e:
            logging.error(f"Bot polling xatosi: {e}")
            time.sleep(5)

threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
