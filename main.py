# -*- coding: utf-8 -*-
import logging
import json
import os
import time
import sqlite3
import threading
from pypdf import PdfReader
import docx
import telebot
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List, Optional

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Loglarni sozlash
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Token va Kalitlarni yuklash
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)
templates = Jinja2Templates(directory="templates")

raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
current_key_index = 0

DOWNLOADS_DIR = 'downloads'
DB_PATH = "/data/quiz_pilot.db" if os.path.exists("/data") else "quiz_pilot.db"

# Ma'lumotlar bazasini tekshirish va yaratish
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute('''CREATE TABLE IF NOT EXISTS quizzes (
                        id TEXT PRIMARY KEY, 
                        user_id INTEGER, 
                        title TEXT, 
                        total INTEGER, 
                        answered INTEGER, 
                        quiz_json TEXT, 
                        created_at INTEGER,
                        last_score INTEGER DEFAULT -1,
                        last_percent INTEGER DEFAULT -1)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, created_at INTEGER)''')
    
    try:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN last_score INTEGER DEFAULT -1")
        cursor.execute("ALTER TABLE quizzes ADD COLUMN last_percent INTEGER DEFAULT -1")
    except:
        pass
        
    conn.commit()
    conn.close()

init_db()

# Pydantic Sxemalari
class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="Jami 4 ta variant ro'yxati (Variant harflarisiz: A), B) qo'shmang)")
    correct_index: int = Field(description="To'g'ri javob joylashtirilgan indeks raqami (0 dan 3 gacha)")
    explanation: str = Field(description="Ushbu javob nega to'g'riligini tushuntiruvchi qisqa izoh")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")

class ProgressUpdateRequest(BaseModel):
    quiz_id: str
    user_id: int
    correct_count: int
    percent: int

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

# Bazadagi jami foydalanuvchilar sonini hisoblash funksiyasi
def get_users_count():
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logging.error(f"Foydalanuvchilar sonini olishda xato: {e}")
        return 0

# Telegram Bot Buyruqlarini Tinglash Qismi
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    add_user_to_db(user_id)
    
    # Real vaqtdagi foydalanuvchilar sonini olish
    total_users = get_users_count()
    
    welcome_text = (
        f"👋 Salom, {message.from_user.first_name}! **Quiz Pilot Super Mini App** tizimiga xush kelibsiz.\n\n"
        f"👥 **Bizning foydalanuvchilar:** {total_users} ta active user\n\n"
        "⚡ **Yangi Yangilanish:**\n"
        "🔥 Endi tizimimiz bitta darslikdan **50 tagacha mukammal va xatosiz test savollarini** qabul qila oladi va tayyorlaydi!\n\n"
        "🚀 Marhamat, pastdagi yonma-yon turgan tugmalardan foydalanib ilovani oching, darsligingizni yuklang va testlarni silliq ishlang!"
    )
    
    # Tugmalarni sozlash
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_start = telebot.types.KeyboardButton(text="/start")
    
    mini_app_url = os.getenv("MINI_APP_URL", "https://your-railway-url.up.railway.app")
    btn_app = telebot.types.KeyboardButton(text="Ilovani ochish 🚀", web_app=telebot.types.WebAppInfo(url=mini_app_url))
    
    # Ikkala tugmani yonma-yon bitta qatorga joylashtirish
    markup.row(btn_start, btn_app)
    
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

# FastAPI Ilovasi
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
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.post("/api/create-quiz-web")
async def create_quiz_web(user_id: int = Form(...), text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None)):
    add_user_to_db(user_id)
    raw_text = ""
    title = "Matnli Test"
    
    if file and file.filename and len(file.filename.strip()) > 0:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        file_path = os.path.join(DOWNLOADS_DIR, file.filename)
        try:
            contents = await file.read()
            if len(contents) > 0:
                with open(file_path, "wb") as f:
                    f.write(contents)
                
                if file.filename.endswith('.pdf'):
                    try:
                        reader = PdfReader(file_path)
                        raw_text = "".join([p.extract_text() + "\n" for p in reader.pages if p.extract_text()])
                    except Exception as e:
                        logging.error(f"PDF o'qishda xato: {e}")
                    title = file.filename.replace('.pdf', '')
                elif file.filename.endswith('.docx'):
                    try:
                        doc = docx.Document(file_path)
                        raw_text = "\n".join([p.text for p in doc.paragraphs])
                    except Exception as e:
                        logging.error(f"DOCX o'qishda xato: {e}")
                    title = file.filename.replace('.docx', '')
        except Exception as e:
            logging.error(f"Faylni yuklashda umumiy xato: {e}")
            
    if not raw_text.strip() and text:
        raw_text = text
        title = text[:15] + "..."

    if not raw_text.strip():
        return {"status": "error", "message": "Matn yoki darslikni o'qib bo'lmadi."}

    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    if not quiz_json_raw:
        return {"status": "error", "message": "AI test generatsiya qila olmadi."}

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        if not items:
            return {"status": "error", "message": "AI savollar ro'yxatini bo'sh qaytardi."}
            
        quiz_id = f"q_{int(time.time())}"
        
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("INSERT INTO quizzes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                       (quiz_id, user_id, title[:22], len(items), 0, quiz_json_raw, int(time.time()), -1, -1))
        conn.commit()
        conn.close()

        try: 
            bot.send_message(user_id, f"🎉 Ajoyib! Katta darsligingiz bo'yicha jami **{len(items)} ta** test savoli xatosiz tayyorlandi!")
        except Exception as e: 
            logging.error(f"Telegram xabari yuborilmadi: {e}")

        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def generate_quiz_from_gemini(extracted_text):
    global current_key_index
    if not GOOGLE_API_KEYS: return None

    system_instruction = """You are an advanced AI quiz generator. 
CRITICAL RULES:
1. LANGUAGE RULE: Detect the language of the provided text. You MUST generate the questions, choices, and explanations in the EXACT SAME language as the input text. If the input text is in English, EVERYTHING must be in English. No Uzbek translations allowed for English text!
2. QUESTION COUNT RULE: Look at the input text. If the user provided a strict list of questions (e.g., 5, 10, or 15 questions), you MUST ONLY extract and format THOSE EXACT questions into the quiz structure. Do NOT generate extra questions, do NOT inflate the count to 50 if the text only contains a few questions. Format ONLY what is given. If it's a huge continuous textbook, you can generate up to 40-50 questions maximum."""

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
                    temperature=0.2
                )
            )
            if response and response.text: return response.text
        except Exception as e:
            logging.error(f"Gemini API xatosi: {e}")
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
    return None

@app.get("/api/quizzes")
def get_user_quizzes(user_id: int):
    add_user_to_db(user_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("SELECT id, title, total, answered, created_at, last_score, last_percent FROM quizzes WHERE user_id = ? ORDER BY CREATED_AT DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    quizzes = [{
        "id": r["id"], 
        "title": r["title"], 
        "total": r["total"], 
        "answered": r["answered"], 
        "created_at": r["created_at"],
        "last_score": r["last_score"],
        "last_percent": r["last_percent"]
    } for r in rows]
    return {"status": "ok", "quizzes": quizzes}

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
    cursor.execute("UPDATE quizzes SET answered = total, last_score = ?, last_percent = ? WHERE id = ? AND user_id = ?", 
                   (data.correct_count, data.percent, data.quiz_id, data.user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# YANGA QO'SHILGAN: Testni o'chirib tashlash yo'nalishi
@app.delete("/api/delete-quiz")
def delete_quiz(quiz_id: str, user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("DELETE FROM quizzes WHERE id = ? AND user_id = ?", (quiz_id, user_id))
        conn.commit()
        conn.close()
        return {"status": "ok", "message": "Test muvaffaqiyatli o'chirildi."}
    except Exception as e:
        logging.error(f"Testni o'chirishda xatolik: {e}")
        raise HTTPException(status_code=500, detail="Testni o'chirib bo'lmadi.")

# Bot uzluksiz tinglashini fonda bajaradigan funksiya
def start_bot_polling():
    logging.info("Telegram Bot parallel oqimda (Thread) tinglashni boshladi...")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except Exception as e:
            logging.error(f"Polling siklida uzilish bo'ldi, 5 soniyadan keyin qayta urinadi: {e}")
            time.sleep(5)

# FastAPI start-up hodisasi (Server yoqilganda botni ham fonda qo'shib ishga tushiradi)
@app.on_event("startup")
async def startup_event():
    polling_thread = threading.Thread(target=start_bot_polling, daemon=True)
    polling_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
