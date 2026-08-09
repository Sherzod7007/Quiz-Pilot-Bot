# -*- coding: utf-8 -*-
import docx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google import genai
from google.genai import types as genai_types
import json
import logging
import os
from pydantic import BaseModel, Field
from pypdf import PdfReader
import sqlite3
import telebot
import threading
import time
from typing import List, Optional
import uvicorn
import uuid

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)
templates = Jinja2Templates(directory="templates")

raw_admin_id = os.getenv("ADMIN_ID")
try:
    ADMIN_ID = int(str(raw_admin_id).strip()) if raw_admin_id else None
except Exception as e:
    logging.error(f"ADMIN_ID ni int ga o'tkazishda xato: {e}")
    ADMIN_ID = None

raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = (
    [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
)
current_key_index = 0
key_lock = threading.Lock()

DOWNLOADS_DIR = "downloads"
DB_PATH = (
    "/data/quiz_pilot_v2.db" if os.path.exists("/data") else "quiz_pilot_v2.db"
)

# --- MULTILINGUAL (BILDIRISHNOMALAR) ---
MESSAGES = {
    "uz": {
        "welcome": (
            "👋 Salom, {name}! Quiz Pilot Super Mini App tizimiga xush kelibsiz.\n\n"
            "🚀 O'qituvchi va Premium rejalarni faollashtirib, cheksiz testlar va guruhlar yarating!\n\n"
            "👇 Marhamat, pastdagi tugmani bosib ilovani oching!"
        ),
        "open_app": "Ilovani ochish 📱",
        "payment_prompt": (
            "🧾 Siz {tariff_name} ({tariff_price}) tarifini tanlagingiz keldi.\n\n"
            "Iltimos, to'lov qilganingiz haqidagi To'lov Chekini "
            "(Rasm/Skrinshot ko'rinishida) shu yerga yuboring.\n"
            "Sizning buyurtma raqamingiz: {tx_id}"
        ),
        "receipt_received": "✅ Rahmat! To'lov chekingiz administratorga yuborildi. Tez orada tekshirilib, tarifingiz faollashtiriladi.",
        "receipt_error": "⚠️ To'lov chekingiz qabul qilindi, biroq adminga bildirishnoma yuborishda muammo bo'ldi. Admin paneldan tekshiriladi.",
        "payment_approved": "🎉 Tabriklaymiz! Sizning {tariff_name} tarifi uchun qilgan to me'yoringiz tasdiqlandi. Ilovada O'qituvchi / PRO status faollashdi! 👑",
        "payment_rejected": "❌ Siz yuborgan to'lov cheki qabul qilinmadi yoki rad etildi.",
        "quiz_limit_reached": "Sizning 30 kun ichida bepul 2 ta test yaratish limitingiz tugadi. Iltimos, O'qituvchi yoki Premium tarifga o'ting! 👑",
        "quiz_ready": "📝 {title} darsligi bo'yicha jami {count} ta test savoli muvaffaqiyatli tayyorlandi!",
        "teacher_only": "🚫 Bu funksiyadan faqat O'qituvchilar tarifidagilar foydalana oladi!",
    },
    "ru": {
        "welcome": "👋 Привет, {name}! Добро пожаловать в Quiz Pilot Super Mini App.",
        "open_app": "Открыть приложение 📱",
        "payment_prompt": "🧾 Вы выбрали тариф {tariff_name} ({tariff_price}). Пожалуйста, отправьте чек (фото/скриншот) сюда.\nЗаказ: {tx_id}",
        "receipt_received": "✅ Ваш чек отправлен администратору.",
        "receipt_error": "⚠️ Ошибка отправки уведомления администратору.",
        "payment_approved": "🎉 Ваш платеж по тарифу {tariff_name} подтвержден! 👑",
        "payment_rejected": "❌ Ваш чек об оплате был отклонен.",
        "quiz_limit_reached": "Бесплатный лимит исчерпан. Перейдите на Премиум/Учитель тариф! 👑",
        "quiz_ready": "📝 Успешно подготовлено {count} вопросов по {title}!",
        "teacher_only": "🚫 Доступно только для тарифа 'Учитель'!",
    },
    "en": {
        "welcome": "👋 Hello, {name}! Welcome to Quiz Pilot.",
        "open_app": "Open App 📱",
        "payment_prompt": "🧾 Plan: {tariff_name} ({tariff_price}). Send payment receipt screenshot here.\nOrder ID: {tx_id}",
        "receipt_received": "✅ Receipt sent to admin.",
        "receipt_error": "⚠️ Error notifying admin.",
        "payment_approved": "🎉 Payment for {tariff_name} confirmed! 👑",
        "payment_rejected": "❌ Payment receipt rejected.",
        "quiz_limit_reached": "Limit reached. Upgrade plan! 👑",
        "quiz_ready": "📝 {count} questions generated for {title}!",
        "teacher_only": "🚫 Teacher Plan required!",
    }
}

def get_user_lang(user_id: int) -> str:
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT language FROM users WHERE user_id = ?", (int(user_id),))
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
        free_used INTEGER DEFAULT 0,
        premium_until INTEGER DEFAULT 0)""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS flashcards (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        front TEXT,
        back TEXT,
        created_at INTEGER)""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_groups (
        group_id TEXT PRIMARY KEY,
        teacher_id INTEGER,
        group_name TEXT,
        created_at INTEGER)""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS payments (
        tx_id TEXT PRIMARY KEY,
        user_id INTEGER,
        tariff_name TEXT,
        tariff_price TEXT,
        status TEXT DEFAULT 'pending',
        created_at INTEGER)""")

    conn.commit()
    conn.close()

init_db()

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="Jami 4 ta variant")
    correct_index: int = Field(description="To'g'ri javob indeksi (0-3)")
    explanation: str = Field(description="Izoh")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem]

class GroupCreateRequest(BaseModel):
    user_id: int
    group_name: str

class PaymentIntentRequest(BaseModel):
    action: str
    user_id: int
    tariff_name: str
    tariff_price: str

def add_user_to_db(user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, created_at, language, status, free_used, premium_until) "
            "VALUES (?, ?, 'uz', 'Oddiy foydalanuvchi', 0, 0)",
            (int(user_id), int(time.time())),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Foydalanuvchi qo'shishda xato: {e}")

def is_teacher_or_pro(status: str, premium_until: int) -> bool:
    current_now = int(time.time())
    if not status:
        return False
    is_active = (premium_until > current_now) or (premium_until == -1)
    has_status = ("PRO" in status) or ("O'qituvchi" in status) or ("Teacher" in status)
    return has_status and is_active

def trigger_payment_flow(user_id: int, tariff_name: str, tariff_price: str):
    try:
        user_id = int(user_id)
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
    mini_app_url = os.getenv("MINI_APP_URL", "https://your-railway-url.up.railway.app")
    btn_app = telebot.types.KeyboardButton(
        text=MESSAGES[user_lang]["open_app"], web_app=telebot.types.WebAppInfo(url=mini_app_url)
    )
    markup.row(btn_app)
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(content_types=["photo"])
def handle_receipt_photo(message):
    user_id = message.from_user.id
    user_lang = get_user_lang(user_id)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    # PENDING BO'LGAN OXIRGI TRANSAKSIYANI OLAMIZ
    cursor.execute(
        "SELECT tx_id, tariff_name, tariff_price FROM payments WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    pending_pay = cursor.fetchone()
    conn.close()

    if not pending_pay:
        bot.reply_to(message, "⚠️ Sizda faol to'lov so'rovi topilmadi. Avval ilovadan tarifni tanlang.")
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
        f"💰 **YANGI TO'LOV SO'ROVI!**\n\n"
        f"👤 **Foydalanuvchi:** {first_name} ({username})\n"
        f"🆔 **Telegram ID:** `{user_id}`\n"
        f"📦 **Tanlangan Tarif:** {tariff_name}\n"
        f"💵 **To'lov Summasi:** {tariff_price}\n"
        f"🧩 **Tranzaksiya ID:** `{tx_id}`\n\n"
        f"Chekni tekshirib tasdiqlang:"
    )

    # ADMIN_ID MAVJUD BO'LSA ADMINGA, AKSLDA FOYDALANUVCHINING O'ZIGA YUBORILADI (TEST UCHUN)
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
        logging.error(f"Adminga rasm yuborishda xatolik: {e}")
        bot.send_message(message.chat.id, MESSAGES[user_lang]["receipt_error"])

@bot.callback_query_handler(func=lambda call: call.data.startswith("p_"))
def handle_admin_decision(call):
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
        t_name_lower = tariff_name.lower()

        if "o'qituvchi" in t_name_lower or "teacher" in t_name_lower:
            duration = 30 * 24 * 3600
            status_title = "O'qituvchi 👨‍🏫"
        elif "oylik" in t_name_lower or "30" in t_name_lower:
            duration = 30 * 24 * 3600
            status_title = "PRO ✨"
        elif "haftalik" in t_name_lower or "7" in t_name_lower:
            duration = 7 * 24 * 3600
            status_title = "PRO ✨"
        else:
            duration = 24 * 3600
            status_title = "PRO ✨"

        premium_until_timestamp = current_time + duration

        cursor.execute("UPDATE payments SET status = 'approved' WHERE tx_id = ?", (tx_id,))
        cursor.execute(
            "UPDATE users SET status = ?, premium_until = ? WHERE user_id = ?",
            (f"{status_title} ({tariff_name})", premium_until_timestamp, user_id),
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
            succ_msg = MESSAGES[user_lang]["payment_approved"].format(tariff_name=tariff_name)
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
            bot.send_message(user_id, MESSAGES[user_lang]["payment_rejected"])
        except Exception:
            pass

    conn.close()

# --- FASTAPI ENDPOINTS ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/payment-intent")
def api_payment_intent(req: PaymentIntentRequest):
    if req.action == "payment_intent":
        threading.Thread(
            target=trigger_payment_flow,
            args=(req.user_id, req.tariff_name, req.tariff_price),
            daemon=True,
        ).start()
        return {"status": "ok", "message": "To'lov so'rovi qabul qilindi"}
    raise HTTPException(status_code=400, detail="Noto'g'ri amal")

@app.get("/api/premium-status")
def get_premium_status(user_id: int):
    add_user_to_db(user_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT status, free_used, premium_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        user_status = row["status"] or "Oddiy foydalanuvchi"
        premium_until = row["premium_until"] or 0
        free_used = row["free_used"] or 0
        
        is_teacher = ("O'qituvchi" in user_status or "Teacher" in user_status) and (premium_until > int(time.time()))
        
        return {
            "status": "ok",
            "user_status": user_status,
            "free_used": free_used,
            "is_teacher": is_teacher
        }
    return {"status": "ok", "user_status": "Oddiy foydalanuvchi", "free_used": 0, "is_teacher": False}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
