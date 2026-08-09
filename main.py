# -*- coding: utf-8 -*-
import docx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
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
import re

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

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
GOOGLE_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
current_key_index = 0
key_lock = threading.Lock()

DOWNLOADS_DIR = "downloads"
DB_PATH = "/data/quiz_pilot_v2.db" if os.path.exists("/data") else "quiz_pilot_v2.db"

MESSAGES = {
    "uz": {
        "welcome": ("👋 Salom, {name}! Quiz Pilot Super Mini App tizimiga xush kelibsiz.\n\n"
                     "🚀 Yangi Yangilanish:\n🔒 Bizning aqlli to'lov tizimimiz ishga tushdi. "
                     "Premium rejalarni faollashtirib, cheksiz testlar yarating!\n\n"
                     "👇 Marhamat, pastdagi tugmani bosib ilovani oching!"),
        "open_app": "Ilovani ochish 📱",
        "payment_prompt": ("🧾 Siz {tariff_name} ({tariff_price}) tarifini tanladingiz.\n\n"
                           "Iltimos, plastik kartaga to'lov qilganingiz haqidagi To'lov Chekini "
                           "(Rasm/Skrinshot ko'rinishida) shu yerga yuboring.\n"
                           "Sizning buyurtma raqamingiz: {tx_id}"),
        "receipt_received": "✅ Rahmat! To'lov chekingiz administratorga yuborildi. Tez orada tekshirilib, tarifingiz faollashtiriladi.",
        "receipt_error": "⚠️ To'lov chekingiz qabul qilindi, biroq adminga bildirishnoma yuborishda muammo bo'ldi. Admin paneldan tekshiriladi.",
        "payment_approved": "🎉 Tabriklaymiz! Sizning {tariff_name} tarifi uchun qilgan to'lovingiz tasdiqlandi. Ilovada PRO status faollashdi! 👑",
        "payment_rejected": "❌ Siz yuborgan to'lov cheki qabul qilinmadi yoki rad etildi. Agar xatolik bo'lgan deb o'ylasangiz, administratorga murojaat qiling.",
        "quiz_limit_reached": "Sizning 30 kun ichida bepul 2 ta test yaratish limitingiz tugadi. Iltimos, Premium tarifga o'ting! 👑",
        "quiz_ready": "📝 {title} darsligi bo'yicha jami {count} ta test savoli muvaffaqiyatli tayyorlandi!",
    },
    "ru": {
        "welcome": ("👋 Привет, {name}! Добро пожаловать в Quiz Pilot Super Mini App.\n\n"
                     "🚀 Новое обновление:\n🔒 Запущена наша умная система оплаты. "
                     "Активируйте Premium тарифы и создавайте неограниченное количество тестов!\n\n"
                     "👇 Нажмите кнопку ниже, чтобы открыть приложение!"),
        "open_app": "Открыть приложение 📱",
        "payment_prompt": ("🧾 Вы выбрали тариф {tariff_name} ({tariff_price}).\n\n"
                           "Пожалуйста, отправьте чек об оплате (в виде фото/скриншота) сюда.\n"
                           "Ваш номер заказа: {tx_id}"),
        "receipt_received": "✅ Спасибо! Ваш чек отправлен администратору. В ближайшее время он будет проверен, и ваш тариф активируется.",
        "receipt_error": "⚠️ Ваш чек принят, но возникла проблема с отправкой уведомления администратору. Он будет проверен через админ-панель.",
        "payment_approved": "🎉 Поздравляем! Ваш платеж по тарифу {tariff_name} подтвержден. В приложении активирован PRO статус! 👑",
        "payment_rejected": "❌ Ваш чек об оплате был отклонен. Если вы считаете, что произошла ошибка, свяжитесь с администратором.",
        "quiz_limit_reached": "Ваш лимит на создание 2 бесплатных тестов в течение 30 дней исчерпан. Пожалуйста, перейдите на Premium тариф! 👑",
        "quiz_ready": "📝 Успешно подготовлено {count} тестовых вопросов по материалу {title}!",
    },
    "en": {
        "welcome": ("👋 Hello, {name}! Welcome to Quiz Pilot Super Mini App.\n\n"
                     "🚀 New Update:\n🔒 Our smart payment system is now live. "
                     "Activate Premium plans to generate unlimited quizzes!\n\n"
                     "👇 Tap the button below to open the app!"),
        "open_app": "Open App 📱",
        "payment_prompt": ("🧾 You have selected the {tariff_name} ({tariff_price}) plan.\n\n"
                           "Please send your payment receipt (as a Photo/Screenshot) here.\n"
                           "Your Order ID is: {tx_id}"),
        "receipt_received": "✅ Thank you! Your payment receipt has been sent to the administrator. It will be verified shortly, and your plan will be activated.",
        "receipt_error": "⚠️ Your receipt was received, but there was an issue notifying the admin. It will be reviewed via the admin panel.",
        "payment_approved": "🎉 Congratulations! Your payment for the {tariff_name} plan has been confirmed. PRO status is now active! 👑",
        "payment_rejected": "❌ Your payment receipt was rejected. If you believe this is an error, please contact support.",
        "quiz_limit_reached": "You have reached your free limit of 2 quizzes within 30 days. Please upgrade to a Premium plan! 👑",
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
        id TEXT PRIMARY KEY, user_id INTEGER, title TEXT, total INTEGER, answered INTEGER,
        quiz_json TEXT, created_at INTEGER, last_score INTEGER DEFAULT -1,
        last_percent INTEGER DEFAULT -1, is_public INTEGER DEFAULT 0)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, created_at INTEGER, language TEXT DEFAULT 'uz',
        status TEXT DEFAULT 'Oddiy foydalanuvchi', free_used INTEGER DEFAULT 0,
        premium_until INTEGER DEFAULT 0)""")
    cursor.execute("PRAGMA table_info(users);")
    columns = [col[1] for col in cursor.fetchall()]
    if "status" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'Oddiy foydalanuvchi';")
    if "free_used" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN free_used INTEGER DEFAULT 0;")
    if "premium_until" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN premium_until INTEGER DEFAULT 0;")
    cursor.execute("UPDATE users SET free_used = 0 WHERE free_used IS NULL;")
    cursor.execute("UPDATE users SET status = 'Oddiy foydalanuvchi' WHERE status IS NULL;")
    cursor.execute("UPDATE users SET premium_until = 0 WHERE premium_until IS NULL;")
    cursor.execute("""CREATE TABLE IF NOT EXISTS flashcards (
        id TEXT PRIMARY KEY, user_id INTEGER, front TEXT, back TEXT, created_at INTEGER)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS payments (
        tx_id TEXT PRIMARY KEY, user_id INTEGER, tariff_name TEXT, tariff_price TEXT,
        status TEXT DEFAULT 'pending', created_at INTEGER)""")

    # Teacher group-mode tables. Existing tables/data are untouched.
    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_sessions (
        id TEXT PRIMARY KEY,
        quiz_id TEXT NOT NULL,
        teacher_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        token TEXT UNIQUE NOT NULL,
        duration_minutes INTEGER DEFAULT 60,
        status TEXT DEFAULT 'active',
        created_at INTEGER NOT NULL,
        started_at INTEGER NOT NULL,
        expires_at INTEGER DEFAULT 0,
        closed_at INTEGER DEFAULT 0)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_results (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        quiz_id TEXT NOT NULL,
        teacher_id INTEGER NOT NULL,
        participant_id INTEGER NOT NULL,
        participant_name TEXT,
        username TEXT,
        correct_count INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        percent INTEGER DEFAULT 0,
        started_at INTEGER DEFAULT 0,
        finished_at INTEGER DEFAULT 0,
        UNIQUE(session_id, participant_id))""")
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
    tariff_name: str
    tariff_price: str


class TeacherSessionCreateRequest(BaseModel):
    teacher_id: int
    quiz_id: str
    duration_minutes: int = 60


class TeacherCloseSessionRequest(BaseModel):
    teacher_id: int
    session_id: str


class TeacherResultRequest(BaseModel):
    session_token: str
    participant_id: int
    participant_name: str = "Ishtirokchi"
    username: str = ""
    correct_count: int
    total: int
    started_at: int = 0


def add_user_to_db(user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("INSERT OR IGNORE INTO users (user_id, created_at, language, status, free_used, premium_until) VALUES (?, ?, 'uz', 'Oddiy foydalanuvchi', 0, 0)", (user_id, int(time.time())))
        conn.commit(); conn.close()
    except Exception as e:
        logging.error(f"Foydalanuvchi qo'shishda xato: {e}")


def get_users_count():
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor(); cursor.execute("SELECT COUNT(DISTINCT user_id) FROM users")
        count = cursor.fetchone()[0]; conn.close(); return count
    except Exception as e:
        logging.error(f"Foydalanuvchilar sonini olishda xato: {e}"); return 0


def get_user_plan(user_id: int):
    add_user_to_db(user_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT status, premium_until, free_used, created_at FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close(); return {"status": "Oddiy foydalanuvchi", "premium_until": 0, "free_used": 0, "is_pro": False, "is_teacher": False}
    status = row["status"] or "Oddiy foydalanuvchi"
    until = int(row["premium_until"] or 0)
    now = int(time.time())
    if "PRO" in status and until > 0 and now > until:
        cur.execute("UPDATE users SET status='Oddiy foydalanuvchi', premium_until=0 WHERE user_id=?", (user_id,)); conn.commit()
        status = "Oddiy foydalanuvchi"; until = 0
    conn.close()
    low = status.lower()
    return {"status": status, "premium_until": until, "free_used": int(row["free_used"] or 0),
            "is_pro": "pro" in low, "is_teacher": ("o'qituvchi" in low or "o‘qituvchi" in low or "teacher" in low)}


def is_teacher_user(user_id: int) -> bool:
    return get_user_plan(user_id)["is_teacher"]


def trigger_payment_flow(user_id, tariff_name, tariff_price):
    try:
        tx_id = f"TX{uuid.uuid4().hex[:6].upper()}"
        conn = sqlite3.connect(DB_PATH, check_same_thread=False); cursor = conn.cursor()
        cursor.execute("UPDATE payments SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'", (user_id,))
        cursor.execute("INSERT INTO payments VALUES (?, ?, ?, ?, 'pending', ?)", (tx_id, user_id, tariff_name, tariff_price, int(time.time())))
        conn.commit(); conn.close()
        user_lang = get_user_lang(user_id)
        bot.send_message(user_id, MESSAGES[user_lang]["payment_prompt"].format(tariff_name=tariff_name, tariff_price=tariff_price, tx_id=tx_id), parse_mode="Markdown")
    except Exception as e: logging.error(f"To'lov jarayonini ishga tushirishda xato: {e}")


@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_id = message.from_user.id; add_user_to_db(user_id); user_lang = get_user_lang(user_id)
    welcome_text = MESSAGES[user_lang]["welcome"].format(name=message.from_user.first_name)
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_start = telebot.types.KeyboardButton(text="/start")
    mini_app_url = os.getenv("MINI_APP_URL", "https://your-railway-url.up.railway.app")
    btn_app = telebot.types.KeyboardButton(text=MESSAGES[user_lang]["open_app"], web_app=telebot.types.WebAppInfo(url=mini_app_url))
    markup.row(btn_start, btn_app); bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)


@bot.message_handler(content_types=["web_app_data"])
def handle_webapp_data(message):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "payment_intent":
            trigger_payment_flow(data.get("user_id"), data.get("tariff_name", "Noma'lum Tarif"), data.get("tariff_price", "0 UZS"))
    except Exception as e: logging.error(f"WebApp ma'lumotlarini o'qishda jiddiy xato: {e}")


@bot.message_handler(content_types=["photo"])
def handle_receipt_photo(message):
    user_id = message.from_user.id; user_lang = get_user_lang(user_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False); cursor = conn.cursor()
    cursor.execute("SELECT tx_id, tariff_name, tariff_price FROM payments WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1", (user_id,))
    pending_pay = cursor.fetchone(); conn.close()
    if not pending_pay: return
    tx_id, tariff_name, tariff_price = pending_pay
    username = f"@{message.from_user.username}" if message.from_user.username else "Mavjud emas"
    file_id = message.photo[-1].file_id
    admin_markup = telebot.types.InlineKeyboardMarkup()
    admin_markup.row(telebot.types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"p_app_{tx_id}_{user_id}"), telebot.types.InlineKeyboardButton("❌ Rad etish", callback_data=f"p_rej_{tx_id}_{user_id}"))
    admin_text = (f"💰 YANGI TO'LOV SO'ROVI!\n\n👤 Foydalanuvchi: {message.from_user.first_name} ({username})\n"
                  f"🆔 Telegram ID: {user_id}\n🌐 Til kodi: {user_lang.upper()}\n📦 Tanlangan Tarif: {tariff_name}\n"
                  f"💵 To'lov Summasi: {tariff_price}\n🧩 Tranzaksiya ID: {tx_id}\n\nChek to'g'riligini tekshiring va pastdagi tugmalardan birini bosing.")
    target_admin = ADMIN_ID if ADMIN_ID else user_id
    try:
        bot.send_photo(target_admin, file_id, caption=admin_text, parse_mode="Markdown", reply_markup=admin_markup)
        bot.send_message(message.chat.id, MESSAGES[user_lang]["receipt_received"])
    except Exception as e:
        logging.error(f"Admin ga rasm yuborishda xatolik yuz berdi: {e}"); bot.send_message(message.chat.id, MESSAGES[user_lang]["receipt_error"])


@bot.callback_query_handler(func=lambda call: call.data.startswith("p_"))
def handle_admin_decision(call):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Siz administrator emassiz!", show_alert=True); return
    parts = call.data.split("_"); action = parts[1]; tx_id = parts[2]; user_id = int(parts[3]); user_lang = get_user_lang(user_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False); cursor = conn.cursor()
    cursor.execute("SELECT status, tariff_name FROM payments WHERE tx_id = ?", (tx_id,)); pay_row = cursor.fetchone()
    if not pay_row or pay_row[0] != "pending":
        bot.answer_callback_query(call.id, "Bu so'rov allaqachon ko'rib chiqilgan!", show_alert=True); conn.close(); return
    tariff_name = pay_row[1]
    if action == "app":
        current_time = int(time.time()); t_name_lower = tariff_name.lower()
        if "umrbod" in t_name_lower or "unlimited" in t_name_lower: duration = 365 * 10 * 24 * 3600
        elif "oyl" in t_name_lower or "30" in t_name_lower or "o'qituvchi" in t_name_lower or "o‘qituvchi" in t_name_lower: duration = 30 * 24 * 3600
        elif "hafta" in t_name_lower or "7" in t_name_lower: duration = 7 * 24 * 3600
        elif "1 kun" in t_name_lower or "kun" in t_name_lower or "24" in t_name_lower or "day" in t_name_lower: duration = 24 * 3600
        else: duration = 24 * 3600
        premium_until_timestamp = current_time + duration
        cursor.execute("UPDATE payments SET status = 'approved' WHERE tx_id = ?", (tx_id,))
        cursor.execute("UPDATE users SET status = ?, premium_until = ? WHERE user_id = ?", (f"PRO ✨ ({tariff_name})", premium_until_timestamp, user_id))
        conn.commit(); bot.answer_callback_query(call.id, "To'lov tasdiqlandi!")
        try: bot.edit_message_caption(f"✅ {call.message.caption}\n\n🟢 TASDIQLANDI!", call.message.chat.id, call.message.message_id)
        except Exception: pass
        try: bot.send_message(user_id, MESSAGES[user_lang]["payment_approved"].format(tariff_name=tariff_name))
        except Exception: pass
    elif action == "rej":
        cursor.execute("UPDATE payments SET status = 'rejected' WHERE tx_id = ?", (tx_id,)); conn.commit(); bot.answer_callback_query(call.id, "To'lov rad etildi.")
        try: bot.edit_message_caption(f"❌ {call.message.caption}\n\n🔴 RAD ETILDI!", call.message.chat.id, call.message.message_id)
        except Exception: pass
        try: bot.send_message(user_id, MESSAGES[user_lang]["payment_rejected"])
        except Exception: pass
    conn.close()


app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    response = templates.TemplateResponse("index.html", {"request": request}); response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"; return response


@app.post("/api/payment-intent")
def api_payment_intent(req: PaymentIntentRequest):
    if req.action == "payment_intent":
        threading.Thread(target=trigger_payment_flow, args=(req.user_id, req.tariff_name, req.tariff_price), daemon=True).start()
        return {"status": "ok", "message": "To'lov so'rovi muvaffaqiyatli qabul qilindi"}
    raise HTTPException(status_code=400, detail="Noto'g'ri amal")


@app.get("/api/premium-status")
def get_premium_status(user_id: int):
    plan = get_user_plan(user_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT created_at, free_used FROM users WHERE user_id = ?", (user_id,)); row = cursor.fetchone()
    if row:
        free_used = int(row["free_used"] or 0); created_at = int(row["created_at"] or time.time()); now = int(time.time())
        if now - created_at >= 30 * 24 * 3600 and free_used > 0:
            cursor.execute("UPDATE users SET free_used=0, created_at=? WHERE user_id=?", (now, user_id)); conn.commit(); free_used = 0
    else: free_used = 0
    conn.close()
    status = plan["status"]; until = plan["premium_until"]
    if "PRO" in status and until > 0:
        uzbek_time = time.gmtime(until + 5 * 3600); readable = time.strftime("%d.%m.%Y %H:%M", uzbek_time); status = f"{status} (Gacha: {readable})"
    return {"status": "ok", "user_status": status, "free_used": free_used, "is_teacher": plan["is_teacher"]}


@app.post("/api/create-quiz-web")
async def create_quiz_web(user_id: int = Form(...), text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None), quiz_title: Optional[str] = Form(None)):
    add_user_to_db(user_id); user_lang = get_user_lang(user_id); plan = get_user_plan(user_id)
    conn_check = sqlite3.connect(DB_PATH, check_same_thread=False); conn_check.row_factory = sqlite3.Row; cursor_check = conn_check.cursor()
    cursor_check.execute("SELECT status, premium_until, free_used, created_at FROM users WHERE user_id = ?", (user_id,)); user_row = cursor_check.fetchone()
    if user_row:
        current_status = user_row["status"] or "Oddiy foydalanuvchi"; premium_until = int(user_row["premium_until"] or 0); free_used = int(user_row["free_used"] or 0); created_at = int(user_row["created_at"] or time.time()); now = int(time.time())
        if now - created_at >= 30 * 24 * 3600 and free_used > 0:
            cursor_check.execute("UPDATE users SET free_used=0, created_at=? WHERE user_id=?", (now, user_id)); conn_check.commit(); free_used = 0
        if "PRO" in current_status and premium_until > 0 and now > premium_until:
            cursor_check.execute("UPDATE users SET status='Oddiy foydalanuvchi', premium_until=0 WHERE user_id=?", (user_id,)); conn_check.commit(); current_status = "Oddiy foydalanuvchi"
        # Teacher is PRO, so the existing free limit is also bypassed for teachers.
        if "PRO" not in current_status and free_used >= 2:
            conn_check.close(); return {"status": "error", "message": MESSAGES[user_lang]["quiz_limit_reached"]}
    conn_check.close()
    raw_text = ""; auto_title = "Matnli Test"
    if file and file.filename and len(file.filename.strip()) > 0:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True); safe_name = os.path.basename(file.filename); file_path = os.path.join(DOWNLOADS_DIR, safe_name)
        try:
            contents = await file.read()
            if contents:
                with open(file_path, "wb") as f: f.write(contents)
                lower = safe_name.lower()
                if lower.endswith(".pdf"):
                    reader = PdfReader(file_path); raw_text = "".join([(p.extract_text() or "") + "\n" for p in reader.pages]); auto_title = safe_name[:-4]
                elif lower.endswith(".docx"):
                    doc = docx.Document(file_path); raw_text = "\n".join([p.text for p in doc.paragraphs]); auto_title = safe_name[:-5]
        except Exception as e: logging.error(f"Foydalanuvchi fayl yuklashda xato: {e}")
    if not raw_text.strip() and text:
        raw_text = text; clean = text.replace("\n", " ").strip(); auto_title = clean[:18] + "..." if len(clean) > 18 else clean
    if not raw_text.strip(): return {"status": "error", "message": "Matn yoki darslikni o'qib bo'lmadi."}
    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    if not quiz_json_raw: return {"status": "error", "message": "AI test generatsiya qila olmadi."}
    try:
        quiz_data = json.loads(quiz_json_raw); items = quiz_data.get("quizzes", [])
        if not items: return {"status": "error", "message": "AI savollar ro'yxatini bo'sh qaytardi."}
        quiz_id = f"q_{int(time.time())}_{os.urandom(2).hex()}"; final_title = quiz_title.strip() if quiz_title and quiz_title.strip() else auto_title
        conn = sqlite3.connect(DB_PATH, check_same_thread=False); cursor = conn.cursor()
        cursor.execute("INSERT INTO quizzes (id,user_id,title,total,answered,quiz_json,created_at,last_score,last_percent,is_public) VALUES (?,?,?,?,?,?,?,?,?,0)", (quiz_id,user_id,final_title[:30],len(items),0,quiz_json_raw,int(time.time()),-1,-1))
        cursor.execute("UPDATE users SET free_used=COALESCE(free_used,0)+1 WHERE user_id=?", (user_id,)); conn.commit(); conn.close()
        try: bot.send_message(user_id, MESSAGES[user_lang]["quiz_ready"].format(title=final_title[:30], count=len(items)))
        except Exception as e: logging.error(f"Telegram xabari yuborilmadi: {e}")
        return {"status": "ok", "quiz_id": quiz_id}
    except Exception as e: return {"status": "error", "message": str(e)}


def generate_quiz_from_gemini(extracted_text):
    global current_key_index
    if not GOOGLE_API_KEYS: logging.error("GOOGLE_API_KEYS topilmadi yoki bo'sh!"); return None
    system_instruction = """You are an advanced AI quiz generator.
CRITICAL RULES:
1. LANGUAGE RULE: Detect the language of the provided text. You MUST generate the questions, choices, and explanations in the EXACT SAME language as the input text.
2. QUESTION COUNT RULE: Look at the input text. If the user provided a strict list of questions, you MUST ONLY extract and format THOSE EXACT questions into the quiz structure. If it's a huge continuous textbook, you can generate up to 40-50 questions maximum."""
    total_keys = len(GOOGLE_API_KEYS)
    with key_lock:
        start_index = current_key_index; current_key_index = (current_key_index + 1) % total_keys
    for i in range(total_keys):
        key_idx = (start_index + i) % total_keys; api_key = GOOGLE_API_KEYS[key_idx].strip()
        if not api_key: continue
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(model="gemini-3.6-flash", contents=extracted_text[:80000], config=genai_types.GenerateContentConfig(system_instruction=system_instruction,response_mime_type="application/json",response_schema=QuizResponse,temperature=0.2))
            if response and response.text:
                logging.info(f"Muvaffaqiyatli AI so'rovi! Ishlatilgan kalit indeksi: [{key_idx}]"); return response.text
        except Exception as e: logging.warning(f"API kalit [{key_idx}] ishlamadi yoki limit tugadi. Xatolik: {e}. Keyingi kalitga o'tilmoqda...")
    logging.error("Barcha API kalitlar bo'yicha so'rovlar muvaffaqiyatsiz bo'ldi."); return None


@app.get("/api/quizzes")
def get_user_quizzes(user_id: int):
    add_user_to_db(user_id); total_users = get_users_count(); plan = get_user_plan(user_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT id,title,total,answered,created_at,last_score,last_percent,is_public FROM quizzes WHERE user_id=? ORDER BY created_at DESC", (user_id,)); rows = cursor.fetchall()
    cursor.execute("SELECT language FROM users WHERE user_id=?", (user_id,)); lang_row = cursor.fetchone(); user_lang = lang_row["language"] if lang_row else "uz"; conn.close()
    quizzes = [{"id":r["id"],"title":r["title"],"total":r["total"],"answered":r["answered"],"created_at":r["created_at"],"last_score":r["last_score"],"last_percent":r["last_percent"],"is_public":r["is_public"]} for r in rows]
    return {"status":"ok","quizzes":quizzes,"total_users":total_users,"user_lang":user_lang,"is_teacher":plan["is_teacher"]}


@app.get("/api/public-quizzes")
def get_public_quizzes():
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); conn.row_factory=sqlite3.Row; cursor=conn.cursor(); cursor.execute("SELECT id,title,total,created_at FROM quizzes WHERE is_public=1 ORDER BY created_at DESC LIMIT 50"); rows=cursor.fetchall(); conn.close()
    return {"status":"ok","quizzes":[{"id":r["id"],"title":r["title"],"total":r["total"],"created_at":r["created_at"]} for r in rows]}


@app.post("/api/toggle-public")
def toggle_public(quiz_id: str, user_id: int, is_public: int):
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); cursor=conn.cursor(); cursor.execute("UPDATE quizzes SET is_public=? WHERE id=? AND user_id=?",(is_public,quiz_id,user_id)); conn.commit(); conn.close(); return {"status":"ok"}


@app.post("/api/set-language")
def set_language(user_id: int, lang: str):
    if lang not in MESSAGES: raise HTTPException(status_code=400, detail="Noto'g'ri til")
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); cursor=conn.cursor(); cursor.execute("UPDATE users SET language=? WHERE user_id=?",(lang,user_id)); conn.commit(); conn.close(); return {"status":"ok"}


@app.get("/api/flashcards")
def get_flashcards(user_id: int):
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); conn.row_factory=sqlite3.Row; cursor=conn.cursor(); cursor.execute("SELECT id,front,back FROM flashcards WHERE user_id=? ORDER BY created_at DESC",(user_id,)); rows=cursor.fetchall(); conn.close(); return {"status":"ok","cards":[{"id":r["id"],"front":r["front"],"back":r["back"]} for r in rows]}


@app.post("/api/create-flashcard")
def create_flashcard(req: FlashcardCreateRequest):
    card_id=f"c_{int(time.time())}_{os.urandom(2).hex()}"; conn=sqlite3.connect(DB_PATH,check_same_thread=False); cursor=conn.cursor(); cursor.execute("INSERT INTO flashcards VALUES (?,?,?,?,?)",(card_id,req.user_id,req.front,req.back,int(time.time()))); conn.commit(); conn.close(); return {"status":"ok"}


@app.delete("/api/delete-flashcard")
def delete_flashcard(card_id: str, user_id: int):
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); cursor=conn.cursor(); cursor.execute("DELETE FROM flashcards WHERE id=? AND user_id=?",(card_id,user_id)); conn.commit(); conn.close(); return {"status":"ok"}


@app.get("/api/quiz-detail")
def get_quiz_detail(quiz_id: str):
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); conn.row_factory=sqlite3.Row; cursor=conn.cursor(); cursor.execute("SELECT quiz_json FROM quizzes WHERE id=?",(quiz_id,)); row=cursor.fetchone(); conn.close()
    if row: return {"status":"ok","quiz_json":json.loads(row["quiz_json"])}
    raise HTTPException(status_code=404,detail="Test topilmadi")


@app.post("/api/update-progress")
def update_progress(data: ProgressUpdateRequest):
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); cursor=conn.cursor(); cursor.execute("UPDATE quizzes SET answered=total,last_score=?,last_percent=? WHERE id=? AND user_id=?",(data.correct_count,data.percent,data.quiz_id,data.user_id)); conn.commit(); conn.close(); return {"status":"ok"}


@app.delete("/api/delete-quiz")
def delete_quiz(quiz_id: str, user_id: int):
    try:
        conn=sqlite3.connect(DB_PATH,check_same_thread=False); cursor=conn.cursor(); cursor.execute("DELETE FROM quizzes WHERE id=? AND user_id=?",(quiz_id,user_id)); conn.commit(); conn.close(); return {"status":"ok","message":"Test o'chirildi."}
    except Exception: raise HTTPException(status_code=500,detail="Xatolik.")


# ========================= TEACHER GROUP MODE =========================
def teacher_guard(user_id: int):
    plan = get_user_plan(user_id)
    if not plan["is_teacher"] or plan["premium_until"] <= int(time.time()):
        raise HTTPException(status_code=403, detail="O'qituvchi tarifi faol emas.")
    return plan


def safe_export_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9А-Яа-яЎўҚқҒғҲҳ ._-]", "_", name)[:70] or "quiz_results"


@app.get("/api/teacher/status")
def teacher_status(user_id: int):
    plan = get_user_plan(user_id)
    return {"status":"ok", "is_teacher":plan["is_teacher"], "active_until":plan["premium_until"]}


@app.post("/api/teacher/sessions")
def create_teacher_session(req: TeacherSessionCreateRequest):
    teacher_guard(req.teacher_id)
    duration = max(0, min(int(req.duration_minutes), 1440))
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); conn.row_factory=sqlite3.Row; cur=conn.cursor()
    cur.execute("SELECT id,title FROM quizzes WHERE id=? AND user_id=?",(req.quiz_id,req.teacher_id)); quiz=cur.fetchone()
    if not quiz: conn.close(); raise HTTPException(status_code=404,detail="Test topilmadi yoki sizga tegishli emas.")
    now=int(time.time()); session_id=f"ts_{uuid.uuid4().hex[:12]}"; token=uuid.uuid4().hex[:12]; expires=now+duration*60 if duration else 0
    cur.execute("INSERT INTO teacher_sessions (id,quiz_id,teacher_id,title,token,duration_minutes,status,created_at,started_at,expires_at,closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,0)", (session_id,quiz["id"],req.teacher_id,quiz["title"],token,duration,"active",now,now,expires)); conn.commit(); conn.close()
    return {"status":"ok","session_id":session_id,"token":token,"title":quiz["title"],"duration_minutes":duration,"expires_at":expires,"link":f"{os.getenv('MINI_APP_URL','').rstrip('/')}/?teacher_test={token}"}


@app.get("/api/teacher/sessions")
def get_teacher_sessions(teacher_id: int):
    teacher_guard(teacher_id)
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); conn.row_factory=sqlite3.Row; cur=conn.cursor()
    cur.execute("""SELECT s.*, COUNT(r.id) AS participant_count, COALESCE(MAX(r.percent),0) AS max_percent
                   FROM teacher_sessions s LEFT JOIN teacher_results r ON r.session_id=s.id
                   WHERE s.teacher_id=? GROUP BY s.id ORDER BY s.created_at DESC LIMIT 100""",(teacher_id,)); rows=cur.fetchall(); conn.close()
    now=int(time.time()); result=[]
    for r in rows:
        status=r["status"]
        if status=="active" and r["expires_at"] and now>r["expires_at"]: status="expired"
        result.append({"id":r["id"],"quiz_id":r["quiz_id"],"title":r["title"],"token":r["token"],"duration_minutes":r["duration_minutes"],"status":status,"created_at":r["created_at"],"started_at":r["started_at"],"expires_at":r["expires_at"],"participant_count":r["participant_count"]})
    return {"status":"ok","sessions":result}


@app.post("/api/teacher/sessions/close")
def close_teacher_session(req: TeacherCloseSessionRequest):
    teacher_guard(req.teacher_id)
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); cur=conn.cursor(); cur.execute("UPDATE teacher_sessions SET status='closed',closed_at=? WHERE id=? AND teacher_id=?",(int(time.time()),req.session_id,req.teacher_id)); changed=cur.rowcount; conn.commit(); conn.close()
    if not changed: raise HTTPException(status_code=404,detail="Sessiya topilmadi.")
    return {"status":"ok"}


@app.get("/api/teacher/session/{token}")
def get_teacher_session(token: str):
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); conn.row_factory=sqlite3.Row; cur=conn.cursor(); cur.execute("SELECT s.*,q.quiz_json,q.total FROM teacher_sessions s JOIN quizzes q ON q.id=s.quiz_id WHERE s.token=?",(token,)); row=cur.fetchone(); conn.close()
    if not row: raise HTTPException(status_code=404,detail="Test havolasi topilmadi.")
    now=int(time.time())
    if row["status"] != "active": raise HTTPException(status_code=410,detail="Bu test sessiyasi yopilgan.")
    if row["expires_at"] and now>row["expires_at"]: raise HTTPException(status_code=410,detail="Bu test sessiyasining vaqti tugagan.")
    return {"status":"ok","session_id":row["id"],"title":row["title"],"quiz_json":json.loads(row["quiz_json"]),"total":row["total"],"expires_at":row["expires_at"],"duration_minutes":row["duration_minutes"]}


@app.post("/api/teacher/session-result")
def submit_teacher_result(req: TeacherResultRequest):
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); conn.row_factory=sqlite3.Row; cur=conn.cursor(); cur.execute("SELECT * FROM teacher_sessions WHERE token=?",(req.session_token,)); s=cur.fetchone()
    if not s: conn.close(); raise HTTPException(status_code=404,detail="Sessiya topilmadi.")
    now=int(time.time())
    if s["status"] != "active": conn.close(); raise HTTPException(status_code=410,detail="Sessiya yopilgan.")
    if s["expires_at"] and now>s["expires_at"]: conn.close(); raise HTTPException(status_code=410,detail="Vaqt tugagan.")
    cur.execute("SELECT id FROM teacher_results WHERE session_id=? AND participant_id=?",(s["id"],req.participant_id))
    if cur.fetchone(): conn.close(); return {"status":"ok","already_submitted":True}
    total=max(1,int(req.total)); correct=max(0,min(int(req.correct_count),total)); percent=round(correct/total*100); rid=f"tr_{uuid.uuid4().hex[:12]}"; started=req.started_at or now
    cur.execute("INSERT INTO teacher_results (id,session_id,quiz_id,teacher_id,participant_id,participant_name,username,correct_count,total,percent,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(rid,s["id"],s["quiz_id"],s["teacher_id"],req.participant_id,req.participant_name[:100],req.username[:100],correct,total,percent,started,now)); conn.commit(); conn.close()
    return {"status":"ok","correct_count":correct,"total":total,"percent":percent}


@app.get("/api/teacher/session-results")
def get_teacher_results(teacher_id: int, session_id: str):
    teacher_guard(teacher_id)
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); conn.row_factory=sqlite3.Row; cur=conn.cursor(); cur.execute("SELECT title FROM teacher_sessions WHERE id=? AND teacher_id=?",(session_id,teacher_id)); session=cur.fetchone()
    if not session: conn.close(); raise HTTPException(status_code=404,detail="Sessiya topilmadi.")
    cur.execute("SELECT id,participant_id,participant_name,username,correct_count,total,percent,started_at,finished_at FROM teacher_results WHERE session_id=? ORDER BY percent DESC, correct_count DESC, finished_at ASC",(session_id,)); rows=cur.fetchall(); conn.close()
    return {"status":"ok","title":session["title"],"results":[{"id":r["id"],"participant_id":r["participant_id"],"participant_name":r["participant_name"],"username":r["username"],"correct_count":r["correct_count"],"total":r["total"],"percent":r["percent"],"started_at":r["started_at"],"finished_at":r["finished_at"]} for r in rows]}


def build_export_rows(teacher_id: int, session_id: str):
    teacher_guard(teacher_id)
    conn=sqlite3.connect(DB_PATH,check_same_thread=False); conn.row_factory=sqlite3.Row; cur=conn.cursor(); cur.execute("SELECT title FROM teacher_sessions WHERE id=? AND teacher_id=?",(session_id,teacher_id)); session=cur.fetchone()
    if not session: conn.close(); raise HTTPException(status_code=404,detail="Sessiya topilmadi.")
    cur.execute("SELECT participant_name,username,correct_count,total,percent,started_at,finished_at FROM teacher_results WHERE session_id=? ORDER BY percent DESC, correct_count DESC, finished_at ASC",(session_id,)); rows=cur.fetchall(); conn.close()
    return session["title"], rows


@app.get("/api/teacher/export")
def export_teacher_results(teacher_id: int, session_id: str, format: str = "xlsx"):
    title, rows = build_export_rows(teacher_id, session_id); fmt=format.lower(); os.makedirs(DOWNLOADS_DIR,exist_ok=True); base=safe_export_name(title)+"_natijalar"
    headers=["№","O'quvchi","Username","To'g'ri","Jami","Foiz","Boshlagan vaqt","Tugatgan vaqt"]
    data=[]
    for i,r in enumerate(rows,1):
        st=time.strftime("%d.%m.%Y %H:%M",time.localtime(r["started_at"])) if r["started_at"] else ""
        ft=time.strftime("%d.%m.%Y %H:%M",time.localtime(r["finished_at"])) if r["finished_at"] else ""
        data.append([i,r["participant_name"] or "",r["username"] or "",r["correct_count"],r["total"],r["percent"],st,ft])
    if fmt in ("xlsx","excel"):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment
        except ImportError: raise HTTPException(status_code=500,detail="Excel eksporti uchun openpyxl o'rnatilmagan.")
        path=os.path.join(DOWNLOADS_DIR,base+".xlsx"); wb=Workbook(); ws=wb.active; ws.title="Natijalar"; ws.append([title]); ws.merge_cells(start_row=1,start_column=1,end_row=1,end_column=len(headers)); ws["A1"].font=Font(bold=True,size=14); ws.append(headers)
        for c in ws[2]: c.font=Font(bold=True); c.alignment=Alignment(horizontal="center")
        for row in data: ws.append(row)
        for col in ws.columns:
            letter=col[0].column_letter; ws.column_dimensions[letter].width=min(max(max(len(str(c.value or "")) for c in col)+2,10),35)
        wb.save(path); return FileResponse(path,filename=os.path.basename(path),media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt=="docx":
        path=os.path.join(DOWNLOADS_DIR,base+".docx"); d=docx.Document(); d.add_heading(title,0); d.add_paragraph("Quiz Pilot — O'qituvchi guruh natijalari")
        table=d.add_table(rows=1,cols=len(headers)); table.style="Table Grid"
        for i,h in enumerate(headers): table.rows[0].cells[i].text=h
        for row in data:
            cells=table.add_row().cells
            for i,v in enumerate(row): cells[i].text=str(v)
        d.save(path); return FileResponse(path,filename=os.path.basename(path),media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if fmt=="pdf":
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import landscape,A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.platypus import SimpleDocTemplate,Table,TableStyle,Paragraph,Spacer
        except ImportError: raise HTTPException(status_code=500,detail="PDF eksporti uchun reportlab o'rnatilmagan.")
        path=os.path.join(DOWNLOADS_DIR,base+".pdf"); doc=SimpleDocTemplate(path,pagesize=landscape(A4),rightMargin=8*mm,leftMargin=8*mm,topMargin=8*mm,bottomMargin=8*mm); styles=getSampleStyleSheet(); elems=[Paragraph(title,styles["Title"]),Spacer(1,5*mm)]
        pdf_data=[headers]+data; table=Table(pdf_data,repeatRows=1,colWidths=[10*mm,45*mm,35*mm,16*mm,16*mm,16*mm,35*mm,35*mm]); table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("GRID",(0,0),(-1,-1),0.5,colors.grey),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),7),("VALIGN",(0,0),(-1,-1),"MIDDLE")])) ; elems.append(table); doc.build(elems); return FileResponse(path,filename=os.path.basename(path),media_type="application/pdf")
    raise HTTPException(status_code=400,detail="Format xlsx, docx yoki pdf bo'lishi kerak.")


def start_bot_polling():
    while True:
        try: bot.infinity_polling(timeout=20,long_polling_timeout=10)
        except Exception: time.sleep(5)


@app.on_event("startup")
async def startup_event():
    threading.Thread(target=start_bot_polling, daemon=True).start()


if __name__ == "__main__":
    port=int(os.environ.get("PORT",8080)); uvicorn.run(app,host="0.0.0.0",port=port)
