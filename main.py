# -*- coding: utf-8 -*-
import logging
import json
import os
import time
import sqlite3
from threading import Thread
from pypdf import PdfReader
import docx
import telebot
from telebot import types
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List, Optional

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)
templates = Jinja2Templates(directory="templates")

raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
current_key_index = 0

DOWNLOADS_DIR = 'downloads'
DB_PATH = "/data/quiz_pilot.db" if os.path.exists("/data") else "quiz_pilot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute('''CREATE TABLE IF NOT EXISTS quizzes (id TEXT PRIMARY KEY, user_id INTEGER, title TEXT, total INTEGER, answered INTEGER, quiz_json TEXT, created_at INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, created_at INTEGER)''')
    conn.commit()
    conn.close()

init_db()

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="To'g'ri javob va 3 ta noto'g'ri variantdan iborat jami 4 ta variant ro'yxati")
    correct_index: int = Field(description="To'g'ri javob joylashtirilgan indeks raqami")
    explanation: str = Field(description="Ushbu javob nega to'g'riligini tushuntiruvchi qisqa qoida")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")

class ProgressUpdateRequest(BaseModel):
    quiz_id: str
    user_id: int

def add_user_to_db(user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (user_id, int(time.time())))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Foydalanuvchi qo'shishda xato: {e}")

def get_side_by_side_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    url = os.getenv("WEBAPP_URL", "")
    if url:
        url = url if url.startswith("http") else f"https://{url}"
        markup.add(types.KeyboardButton('/start'), types.KeyboardButton(text="Ilovani ochish 🚀", web_app=types.WebAppInfo(url=url)))
    else:
        markup.add(types.KeyboardButton('/start'))
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    add_user_to_db(message.from_user.id)
    if os.getenv("WEBAPP_URL"):
        try:
            url = os.getenv("WEBAPP_URL")
            url = url if url.startswith("http") else f"https://{url}"
            bot.set_chat_menu_button(chat_id=message.chat.id, menu_button=types.MenuButtonWebApp(type="web_app", text="Ilovani ochish 🚀", web_app=types.WebAppInfo(url=url)))
        except Exception as e:
            logging.error(f"Menu xatosi: {e}")
    
    user_name = message.from_user.first_name
    bot.send_message(
        message.chat.id, 
        f"👋 Salom, {user_name}! **Quiz Pilot Super Mini App** tizimiga xush kelibsiz.\n\n⚡ **Yangi Yangilanish:**\n🔥 Endi tizimimiz bitta darslikdan **50 tagacha mukammal va xatosiz test savollarini** qabul qila oladi va tayyorlaydi!\n\n🚀 Marhamat, pastdagi yonma-yon turgan tugmalardan foydalanib ilovani oching, darsligingizni yuklang va testlarni silliq ishlang!",
        reply_markup=get_side_by_side_keyboard()
    )

@bot.message_handler(commands=['admin'])
def send_admin_stats(message):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("SELECT COUNT(*) FROM users")
        u_count = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM quizzes")
        q_count = cursor.fetchone()
        conn.close()
        bot.send_message(message.chat.id, f"📊 **Quiz Pilot loyihasi statistikasi:**\n\n👤 Jami foydalanuvchilar: **{u_count[0]} ta**\n📝 Jami yaratilgan testlar: **{q_count[0]} ta**")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Statistikani olishda xato: {e}")

def generate_quiz_from_gemini(extracted_text):
    global current_key_index
    if not GOOGLE_API_KEYS: return None

    system_instruction = """Siz berilgan darslik matni asosida mukammal testlar yaratuvchi intellektual botsiz.
Vazifangiz: Berilgan matndan kelib chiqib, QAT'IY RAVISHDA JAMI 50 TA UNIQUE (takrorlanmas) savol tuzing.
Har bir savol uchun 1 ta to'g'ri va 3 ta noto'g'ri variant yarating.
Har bir variant boshiga 'A) ', 'B) ', 'C) ', 'D) ' qo'shing.
Explanation maydoniga javobning qisqa ilmiy isbotini yozing. Matn tili darslik bilan bir xil bo'lsin."""

    for _ in range(len(GOOGLE_API_KEYS)):
        api_key = GOOGLE_API_KEYS[current_key_index].strip()
        if not api_key:
            current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
            continue
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=extracted_text[:80000],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=QuizResponse,
                    temperature=0.6
                )
            )
            if response and response.text: return response.text
        except Exception as e:
            logging.error(f"Gemini API xatosi: {e}")
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
    return None

def run_bot_safe():
    while True:
        try:
            bot.delete_webhook(drop_pending_updates=True)
            logging.info("Bot polling boshlanmoqda...")
            bot.polling(none_stop=True, timeout=20, long_polling_timeout=20)
        except Exception as e:
            logging.error(f"Bot Polling qulflanish xatosi (Tizim avtomatik qayta ulanadi): {e}")
            time.sleep(10)

@asynccontextmanager
async def lifespan(app: FastAPI):
    thread = Thread(target=run_bot_safe, daemon=True)
    thread.start()
    logging.info("🚀 Bot parallel xavfsiz tarmoqda ACTIVE holatga o'tdi!")
    yield

app = FastAPI(lifespan=lifespan)

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

@app.post("/api/create-quiz-web")
async def create_quiz_web(user_id: int = Form(...), text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None)):
    add_user_to_db(user_id)
    raw_text = ""
    title = "Matnli Test"
    
    if file:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        file_path = os.path.join(DOWNLOADS_DIR, file.filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())
        
        if file.filename.endswith('.pdf'):
            try:
                reader = PdfReader(file_path)
                raw_text = "".join([p.extract_text() + "\n" for p in reader.pages if p.extract_text()])
            except: pass
            title = file.filename.replace('.pdf', '')
        elif file.filename.endswith('.docx'):
            try:
                doc = docx.Document(file_path)
                raw_text = "\n".join([p.text for p in doc.paragraphs])
            except: pass
            title = file.filename.replace('.docx', '')
    elif text:
        raw_text = text
        title = text[:15] + "..."

    if not raw_text.strip():
        return {"status": "error", "message": "Matn yoki darslikni o'qib bo'lmadi."}

    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    if not quiz_json_raw:
        return {"status": "error", "message": "AI katta hajmli test generatsiya qila olmadi."}

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        quiz_id = f"q_{int(time.time())}"
        
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("INSERT INTO quizzes VALUES (?, ?, ?, ?, ?, ?, ?)", (quiz_id, user_id, title[:22], len(items), 0, quiz_json_raw, int(time.time())))
        conn.commit()
        conn.close()

        try: bot.send_message(user_id, f"🎉 Ajoyib! Katta darsligingiz bo'yicha jami **{len(items)} ta** test savoli xatosiz tayyorlandi!")
        except: pass

        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/quizzes")
def get_user_quizzes(user_id: int):
    add_user_to_db(user_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("SELECT id, title, total, answered, created_at FROM quizzes WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
