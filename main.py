# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import sqlite3
import threading
import time
from typing import List, Optional
import uuid

import docx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from pypdf import PdfReader
import telebot
import uvicorn

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

raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = (
    [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
)
current_key_index = 0
key_lock = threading.Lock()  # Asinxron poygalarning oldini olish uchun lock

DOWNLOADS_DIR = "downloads"
DB_PATH = (
    "/data/quiz_pilot_v2.db" if os.path.exists("/data") else "quiz_pilot_v2.db"
)


# --- UTC+5 (O'ZBEKISTON VAQTI) YORDAMCHI FUNKSIYASI ---
def format_uzb_time(timestamp: int) -> str:
    """Timestamp ni O'zbekiston vaqti (UTC+5) bo'yicha DD.MM.YYYY HH:MM formatiga o'tkazadi"""
    uzb_tz = timezone(timedelta(hours=5))
    dt = datetime.fromtimestamp(timestamp, tz=uzb_tz)
    return dt.strftime("%d.%m.%Y %H:%M")


# --- MULTI-LANGUAGE DICTIONARY ---
MESSAGES = {
    "uz": {
        "welcome": (
            "👋 Salom, {name}! Quiz Pilot Super Mini App tizimiga xush kelibsiz.\n\n"
            "🚀 Yangi Yangilanish:\n🔒 Bizning aqlli to'lov tizimimiz ishga tushdi. "
            "Premium rejalarni faollashtirib, cheksiz testlar yarating!\n\n"
            "👇 Marhamat, pastdagi tugmani bosib ilovani oching!"
        ),
        "btn_app": "Ilovani ochish 📱",
        "payment_prompt": (
            "🧾 Siz {tariff_name} ({tariff_price}) tarifini tanladingiz.\n\n"
            "Iltimos, plastik kartaga to'lov qilganingiz haqidagi To'lov Chekini "
            "(Rasm/Skrinshot ko'rinishida) shu yerga yuboring.\n"
            "Sizning buyurtma raqamingiz: {tx_id}"
        ),
        "receipt_err": "❌ Iltimos, faqat rasm (skrinshot) ko'rinishidagi to'lov chekini yuboring. Qaytadan urinib ko'ring:",
        "receipt_sent": "✅ Rahmat! To'lov chekingiz administratorga yuborildi. Tez orada tekshirilib, tarifingiz faollashtiriladi.",
        "receipt_admin_err": "⚠️ To'lov chekingiz qabul qilindi, biroq adminga bildirishnoma yuborishda muammo bo'ldi. Admin paneldan tekshiriladi.",
        "limit_exceeded": "Sizning 30 kun ichida bepul 2 ta test yaratish limitingiz tugadi. Iltimos, Premium tarifga o'ting! 👑",
        "read_error": "Matn yoki darslikni o'qib bo'lmadi.",
        "ai_error": "AI test generatsiya qila olmadi.",
        "ai_empty": "AI savollar ro'yxatini bo'sh qaytardi.",
        "quiz_ready": "📝 {title} darsligi bo'yicha jami {count} ta test savoli muvaffaqiyatli tayyorlandi!",
        "pay_approved": "🎉 Tabriklaymiz! Sizning {tariff_name} tarifi uchun qilgan to'lovingiz tasdiqlandi. Ilovada PRO status faollashdi! 👑",
        "pay_rejected": "❌ Siz yuborgan to'lov cheki qabul qilinmadi yoki rad etildi. Agar xatolik bo'lgan deb o'ylasangiz, administratorga murojaat qiling.",
        "default_title": "Matnli Test",
    },
    "ru": {
        "welcome": (
            "👋 Привет, {name}! Добро пожаловать в Quiz Pilot Super Mini App.\n\n"
            "🚀 Новое обновление:\n🔒 Запущена наша умная система оплаты. "
            "Активируйте Премиум и создавайте неограниченное количество тестов!\n\n"
            "👇 Нажмите кнопку ниже, чтобы открыть приложение!"
        ),
        "btn_app": "Открыть приложение 📱",
        "payment_prompt": (
            "🧾 Вы выбрали тариф {tariff_name} ({tariff_price}).\n\n"
            "Пожалуйста, отправьте сюда чек об оплате (в виде фото/скриншота).\n"
            "Ваш номер заказа: {tx_id}"
        ),
        "receipt_err": "❌ Пожалуйста, отправьте чек об оплате только в виде фото (скриншота). Попробуйте еще раз:",
        "receipt_sent": "✅ Спасибо! Ваш чек отправлен администратору. Он будет проверен в ближайшее время.",
        "receipt_admin_err": "⚠️ Ваш чек принят, но возникла проблема с отправкой уведомления администратору. Проверяется через админ-панель.",
        "limit_exceeded": "Ваш бесплатный лимит на создание 2 тестов в течение 30 дней исчерпан. Пожалуйста, перейдите на Премиум! 👑",
        "read_error": "Не удалось прочитать текст или учебник.",
        "ai_error": "ИИ не удалось сгенерировать тест.",
        "ai_empty": "ИИ вернул пустой список вопросов.",
        "quiz_ready": "📝 По учебнику {title} успешно подготовлено {count} тестовых вопросов!",
        "pay_approved": "🎉 Поздравляем! Ваш платеж за тариф {tariff_name} подтвержден. В приложении активирован статус PRO! 👑",
        "pay_rejected": "❌ Ваш чек об оплате не принят или отклонен. Если вы считаете, что это ошибка, обратитесь к администратору.",
        "default_title": "Текстовый тест",
    },
    "en": {
        "welcome": (
            "👋 Hello, {name}! Welcome to Quiz Pilot Super Mini App.\n\n"
            "🚀 New Update:\n🔒 Our smart payment system is now live. "
            "Activate Premium plans and create unlimited quizzes!\n\n"
            "👇 Tap the button below to open the app!"
        ),
        "btn_app": "Open App 📱",
        "payment_prompt": (
            "🧾 You selected {tariff_name} ({tariff_price}).\n\n"
            "Please send your payment receipt (as a photo/screenshot) here.\n"
            "Your order ID: {tx_id}"
        ),
        "receipt_err": "❌ Please send the payment receipt as a photo or screenshot only. Try again:",
        "receipt_sent": "✅ Thank you! Your payment receipt has been sent to the admin for verification.",
        "receipt_admin_err": "⚠️ Receipt received, but there was an issue notifying the admin. It will be checked manually.",
        "limit_exceeded": "Your free limit of 2 tests per 30 days has expired. Please upgrade to Premium! 👑",
        "read_error": "Could not read the provided text or document.",
        "ai_error": "AI failed to generate the quiz.",
        "ai_empty": "AI returned an empty list of questions.",
        "quiz_ready": "📝 A total of {count} quiz questions for {title} have been created successfully!",
        "pay_approved": "🎉 Congratulations! Your payment for {tariff_name} has been approved. PRO status is now active! 👑",
        "pay_rejected": "❌ Your payment receipt was rejected. If you think this is a mistake, please contact the administrator.",
        "default_title": "Text Quiz",
    },
}


def get_user_lang(user_id: int) -> str:
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT language FROM users WHERE user_id = ?", (user_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row and row[0] in MESSAGES:
            return row[0]
    except Exception as e:
        logging.error(f"Foydalanuvchi tilini olishda xato: {e}")
    return "uz"


def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")

    # Quizzes jadvali
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

    # Users jadvali
    cursor.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        created_at INTEGER,
        language TEXT DEFAULT 'uz',
        status TEXT DEFAULT 'Oddiy foydalanuvchi',
        free_used INTEGER DEFAULT 0,
        premium_until INTEGER DEFAULT 0)""")

    cursor.execute("PRAGMA table_info(users);")
    columns = [col[1] for col in cursor.fetchall()]

    if "status" not in columns:
        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'Oddiy"
                " foydalanuvchi';"
            )
        except Exception:
            pass
    if "free_used" not in columns:
        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN free_used INTEGER DEFAULT 0;"
            )
        except Exception:
            pass
    if "premium_until" not in columns:
        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN premium_until INTEGER DEFAULT 0;"
            )
        except Exception:
            pass

    # Flashcards jadvali
    cursor.execute("""CREATE TABLE IF NOT EXISTS flashcards (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        front TEXT,
        back TEXT,
        created_at INTEGER)""")

    # Payments jadvali
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
    options: List[str] = Field(
        description="Jami 4 ta variant ro'yxati (Variant harflarisiz)"
    )
    correct_index: int = Field(
        description="To'g'ri javob indeks (0 dan 3 gacha)"
    )
    explanation: str = Field(
        description="Ushbu javob nega to'g'riligini tushuntiruvchi qisqa izoh"
    )


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
    tariff_name: str
    tariff_price: str


def add_user_to_db(user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, created_at, language,"
            " status, free_used, premium_until) VALUES (?, ?, 'uz', 'Oddiy"
            " foydalanuvchi', 0, 0)",
            (user_id, int(time.time())),
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
        cursor.execute("SELECT COUNT(DISTINCT user_id) FROM users")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logging.error(f"Foydalanuvchilar sonini olishda xato: {e}")
        return 0


def trigger_payment_flow(user_id, tariff_name, tariff_price):
    try:
        tx_id = f"TX{uuid.uuid4().hex[:6].upper()}"

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, 'pending', ?)",
            (tx_id, user_id, tariff_name, tariff_price, int(time.time())),
        )
        conn.commit()
        conn.close()

        user_lang = get_user_lang(user_id)
        msg_text = MESSAGES[user_lang]["payment_prompt"].format(
            tariff_name=tariff_name, tariff_price=tariff_price, tx_id=tx_id
        )

        prompt_msg = bot.send_message(user_id, msg_text, parse_mode="Markdown")
        bot.register_next_step_handler(
            prompt_msg, process_receipt, tx_id, tariff_name, tariff_price
        )
    except Exception as e:
        logging.error(f"To'lov jarayonini ishga tushirishda xato: {e}")


@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_id = message.from_user.id
    add_user_to_db(user_id)
    user_lang = get_user_lang(user_id)

    welcome_text = MESSAGES[user_lang]["welcome"].format(
        name=message.from_user.first_name
    )

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_start = telebot.types.KeyboardButton(text="/start")

    mini_app_url = os.getenv(
        "MINI_APP_URL", "https://your-railway-url.up.railway.app"
    )
    btn_app = telebot.types.KeyboardButton(
        text=MESSAGES[user_lang]["btn_app"],
        web_app=telebot.types.WebAppInfo(url=mini_app_url),
    )

    markup.row(btn_start, btn_app)
    bot.send_message(
        message.chat.id,
        welcome_text,
        parse_mode="Markdown",
        reply_markup=markup,
    )


@bot.message_handler(content_types=["web_app_data"])
def handle_webapp_data(message):
    try:
        logging.info(
            f"WebApp dan kelgan xom ma'lumot: {message.web_app_data.data}"
        )
        data = json.loads(message.web_app_data.data)

        if data.get("action") == "payment_intent":
            user_id = data.get("user_id")
            tariff_name = data.get("tariff_name", "Noma'lum Tarif")
            tariff_price = data.get("tariff_price", "0 UZS")
            trigger_payment_flow(user_id, tariff_name, tariff_price)
    except Exception as e:
        logging.error(f"WebApp ma'lumotlarini o'qishda jiddiy xato: {e}")


def process_receipt(message, tx_id, tariff_name, tariff_price):
    user_id = message.from_user.id
    user_lang = get_user_lang(user_id)

    if not message.photo:
        err_msg = bot.send_message(
            message.chat.id, MESSAGES[user_lang]["receipt_err"]
        )
        bot.register_next_step_handler(
            err_msg, process_receipt, tx_id, tariff_name, tariff_price
        )
        return

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "Mavjud emas"
    )
    first_name = message.from_user.first_name
    file_id = message.photo[-1].file_id

    admin_markup = telebot.types.InlineKeyboardMarkup()
    btn_approve = telebot.types.InlineKeyboardButton(
        "✅ Tasdiqlash", callback_data=f"p_app_{tx_id}_{user_id}"
    )
    btn_reject = telebot.types.InlineKeyboardButton(
        "❌ Rad etish", callback_data=f"p_rej_{tx_id}_{user_id}"
    )
    admin_markup.row(btn_approve, btn_reject)

    admin_text = (
        f"💰 YANGI TO'LOV SO'ROVI!\n\n"
        f"👤 Foydalanuvchi: {first_name} ({username})\n"
        f"🆔 Telegram ID: {user_id}\n"
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
        bot.send_message(message.chat.id, MESSAGES[user_lang]["receipt_sent"])
    except Exception as e:
        logging.error(f"Admin ga rasm yuborishda xatolik yuz berdi: {e}")
        bot.send_message(
            message.chat.id, MESSAGES[user_lang]["receipt_admin_err"]
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("p_"))
def handle_admin_decision(call):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "Siz administrator emassiz!", show_alert=True
        )
        return

    parts = call.data.split("_")
    action = parts[1]
    tx_id = parts[2]
    user_id = int(parts[3])

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(
        "SELECT status, tariff_name FROM payments WHERE tx_id = ?", (tx_id,)
    )
    pay_row = cursor.fetchone()

    if not pay_row or pay_row[0] != "pending":
        bot.answer_callback_query(
            call.id, "Bu so'rov allaqachon ko'rib chiqilgan!", show_alert=True
        )
        conn.close()
        return

    tariff_name = pay_row[1]
    user_lang = get_user_lang(user_id)

    if action == "app":
        current_time = int(time.time())

        cursor.execute(
            "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
        )
        u_row = cursor.fetchone()
        user_current_until = u_row[0] if u_row and u_row[0] else 0

        base_time = (
            user_current_until
            if user_current_until > current_time
            else current_time
        )

        # MUDDATLARNI ANIQLASH LOGIKASI (TARTIBI TO'G'RILANDI)
        duration = 24 * 3600  # Standart 1 kun
        t_name_lower = tariff_name.lower()

        if "umrbod" in t_name_lower or "unlimited" in t_name_lower:
            duration = 365 * 10 * 24 * 3600  # 10 yil
        elif (
            "oyl" in t_name_lower
            or "30" in t_name_lower
            or "o'qituvchi" in t_name_lower
        ):
            duration = 30 * 24 * 3600  # 30 kun
        elif "hafta" in t_name_lower or "7" in t_name_lower:
            duration = 7 * 24 * 3600  # 7 kun
        elif "kun" in t_name_lower or "24" in t_name_lower:
            duration = 24 * 3600  # 1 kun (24 soat)

        premium_until_timestamp = base_time + duration
        cursor.execute(
            "UPDATE payments SET status = 'approved' WHERE tx_id = ?", (tx_id,)
        )
        cursor.execute(
            "UPDATE users SET status = ?, premium_until = ? WHERE user_id = ?",
            (f"PRO ✨ ({tariff_name})", premium_until_timestamp, user_id),
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
            bot.send_message(
                user_id,
                MESSAGES[user_lang]["pay_approved"].format(
                    tariff_name=tariff_name
                ),
            )
        except Exception:
            pass

    elif action == "rej":
        cursor.execute(
            "UPDATE payments SET status = 'rejected' WHERE tx_id = ?", (tx_id,)
        )
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
            bot.send_message(user_id, MESSAGES[user_lang]["pay_rejected"])
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
    response = templates.TemplateResponse("index.html", {"request": request})
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.post("/api/payment-intent")
def api_payment_intent(req: PaymentIntentRequest):
    if req.action == "payment_intent":
        threading.Thread(
            target=trigger_payment_flow,
            args=(req.user_id, req.tariff_name, req.tariff_price),
            daemon=True,
        ).start()
        return {
            "status": "ok",
            "message": "To'lov so'rovi muvaffaqiyatli qabul qilindi",
        }
    raise HTTPException(status_code=400, detail="Noto'g'ri amal")


@app.get("/api/premium-status")
def get_premium_status(user_id: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(
        "SELECT status, free_used, premium_until, created_at FROM users WHERE"
        " user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()

    if row:
        user_status = row["status"]
        premium_until = row["premium_until"]
        free_used = row["free_used"]
        created_at = row["created_at"] or int(time.time())
        current_now = int(time.time())

        # 30 kunda bepul limitni 0 ga reset qilish (har oyda 2 ta bepul beriladi)
        thirty_days_sec = 30 * 24 * 3600
        if current_now - created_at >= thirty_days_sec and free_used > 0:
            cursor.execute(
                "UPDATE users SET free_used = 0, created_at = ? WHERE user_id"
                " = ?",
                (current_now, user_id),
            )
            conn.commit()
            free_used = 0

        if (
            "PRO" in user_status
            and premium_until > 0
            and current_now > premium_until
        ):
            cursor.execute(
                "UPDATE users SET status = 'Oddiy foydalanuvchi',"
                " premium_until = 0 WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
            user_status = "Oddiy foydalanuvchi"
            premium_until = 0

        conn.close()

        if "PRO" in user_status and premium_until > 0:
            # SERVER UTC XATOSI NAMOYISH QILINMASLIGI UCHUN UTC+5 BO'YICHA FORMATLANADI
            readable_date = format_uzb_time(premium_until)
            user_status = f"{user_status} (Gacha: {readable_date})"
        return {
            "status": "ok",
            "user_status": user_status,
            "free_used": free_used,
        }

    conn.close()
    return {"status": "ok", "user_status": "Oddiy foydalanuvchi", "free_used": 0}


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
        "SELECT status, premium_until, free_used, created_at FROM users WHERE"
        " user_id = ?",
        (user_id,),
    )
    user_row = cursor_check.fetchone()

    if user_row:
        current_status = user_row["status"]
        premium_until = user_row["premium_until"]
        free_used = user_row["free_used"]
        created_at = user_row["created_at"] or int(time.time())
        current_now = int(time.time())

        # 30 kunda bepul limitni 0 ga reset qilish
        thirty_days_sec = 30 * 24 * 3600
        if current_now - created_at >= thirty_days_sec and free_used > 0:
            cursor_check.execute(
                "UPDATE users SET free_used = 0, created_at = ? WHERE user_id"
                " = ?",
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
                "UPDATE users SET status = 'Oddiy foydalanuvchi',"
                " premium_until = 0 WHERE user_id = ?",
                (user_id,),
            )
            conn_check.commit()
            current_status = "Oddiy foydalanuvchi"

        # Bepul limit 2 taga o'zgartirildi va ko'p tillilik qo'shildi:
        if "PRO" not in current_status and free_used >= 2:
            conn_check.close()
            return {
                "status": "error",
                "message": MESSAGES[user_lang]["limit_exceeded"],
            }
    conn_check.close()

    raw_text = ""
    auto_title = MESSAGES[user_lang]["default_title"]

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
                    raw_text = "".join([
                        p.extract_text() + "\n"
                        for p in reader.pages
                        if p.extract_text()
                    ])
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
        return {"status": "error", "message": MESSAGES[user_lang]["read_error"]}

    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    if not quiz_json_raw:
        return {"status": "error", "message": MESSAGES[user_lang]["ai_error"]}

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        if not items:
            return {
                "status": "error",
                "message": MESSAGES[user_lang]["ai_empty"],
            }

        quiz_id = f"q_{int(time.time())}"
        final_title = (
            quiz_title.strip()
            if (quiz_title and quiz_title.strip())
            else auto_title
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
        cursor.execute(
            "UPDATE users SET free_used = free_used + 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        conn.close()

        try:
            bot.send_message(
                user_id,
                MESSAGES[user_lang]["quiz_ready"].format(
                    title=final_title[:30], count=len(items)
                ),
            )
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

    with key_lock:
        start_index = current_key_index
        current_key_index = (current_key_index + 1) % total_keys

    for i in range(total_keys):
        key_idx = (start_index + i) % total_keys
        api_key = GOOGLE_API_KEYS[key_idx].strip()

        if not api_key:
            continue

        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-3.6-flash",
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
                    "Muvaffaqiyatli AI so'rovi! Ishlatilgan kalit indeksi:"
                    f" [{key_idx}]"
                )
                return response.text

        except Exception as e:
            logging.warning(
                f"API kalit [{key_idx}] ishlamadi yoki limit tugadi. Xatolik:"
                f" {e}. Keyingi kalitga o'tilmoqda..."
            )

    logging.error(
        "Barcha API kalitlar bo'yicha so'rovlar muvaffaqiyatsiz bo'ldi."
    )
    return None


@app.get("/api/quizzes")
def get_user_quizzes(user_id: int):
    add_user_to_db(user_id)
    total_users = get_users_count()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(
        "SELECT id, title, total, answered, created_at, last_score,"
        " last_percent, is_public FROM quizzes WHERE user_id = ? ORDER BY"
        " created_at DESC",
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
def get_public_quizzes():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(
        "SELECT id, title, total, created_at FROM quizzes WHERE is_public = 1"
        " ORDER BY created_at DESC LIMIT 50"
    )
    rows = cursor.fetchall()
    conn.close()
    quizzes = [{
        "id": r["id"],
        "title": r["title"],
        "total": r["total"],
        "created_at": r["created_at"],
    } for r in rows]
    return {"status": "ok", "quizzes": quizzes}


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


@app.get("/api/flashcards")
def get_flashcards(user_id: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(
        "SELECT id, front, back, created_at FROM flashcards WHERE user_id = ?"
        " ORDER BY created_at DESC",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    cards = [{
        "id": r["id"],
        "front": r["front"],
        "back": r["back"],
        "created_at": r["created_at"],
    } for r in rows]
    return {"status": "ok", "flashcards": cards}
