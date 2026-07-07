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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)
templates = Jinja2Templates(directory="templates")

raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
current_key_index = 0

DOWNLOADS_DIR = 'downloads'

# LOYIHA ADMININING TELEGRAM ID RAQAMI (To'lov cheklari shu ID'ga forward qilinadi)
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "123456789")  # O'zingizning Telegram ID'ingizni kiriting

# TUZATILDI: Eski volume muammosini chetlab o'tish uchun baza nomi quiz_pilot_v2.db ga o'zgartirildi
DB_PATH = "/data/quiz_pilot_v2.db" if os.path.exists("/data") else "quiz_pilot_v2.db"

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    
    # Quizzes jadvali (10 ta ustunli toza struktura)
    cursor.execute('''CREATE TABLE IF NOT EXISTS quizzes (
                        id TEXT PRIMARY KEY, 
                        user_id INTEGER, 
                        title TEXT, 
                        total INTEGER, 
                        answered INTEGER, 
                        quiz_json TEXT, 
                        created_at INTEGER,
                        last_score INTEGER DEFAULT -1,
                        last_percent INTEGER DEFAULT -1,
                        is_public INTEGER DEFAULT 0)''')
    
    # Users jadvali (Premium, limit va davrlarni hisoblash ustunlari qo'shildi)
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY, 
                        created_at INTEGER,
                        language TEXT DEFAULT 'uz',
                        premium_status TEXT DEFAULT 'Free',
                        premium_until INTEGER DEFAULT 0,
                        free_quizzes_used INTEGER DEFAULT 0,
                        limit_reset_at INTEGER DEFAULT 0)''')
    
    # Flashcards jadvali
    cursor.execute('''CREATE TABLE IF NOT EXISTS flashcards (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER,
                        front TEXT,
                        back TEXT,
                        created_at INTEGER)''')
                        
    conn.commit()
    conn.close()

init_db()

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="Jami 4 ta variant ro'yxati (Variant harflarisiz)")
    correct_index: int = Field(description="To'g'ri javob indeksi (0 dan 3 gacha)")
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

def add_user_to_db(user_id: int):
    try:
        now = int(time.time())
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        
        # Bepul foydalanuvchilar uchun 30 kunlik davr chegarasi (30 kun = 30 * 24 * 3600 soniya)
        thirty_days_later = now + (30 * 24 * 3600)
        
        cursor.execute("""
            INSERT OR IGNORE INTO users (user_id, created_at, language, premium_status, premium_until, free_quizzes_used, limit_reset_at) 
            VALUES (?, ?, 'uz', 'Free', 0, 0, ?)
        """, (user_id, now, thirty_days_later))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Foydalanuvchi qo'shishda xato: {e}")

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

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    add_user_to_db(user_id)
    
    welcome_text = (
        f"👋 Salom, {message.from_user.first_name}! **Quiz Pilot Super Mini App** tizimiga xush kelibsiz.\n\n"
        "⚡ **Yangi Yangilanish:**\n"
        "🔥 Endi tizimimiz 3 xil tilda (UZ, RU, EN) ishlaydi, Ommaviy testlar va Flesh-kartochkalar to'liq ishga tushdi!\n\n"
        "🚀 Marhamat, pastdagi tugmani bosib ilovani oching!"
    )
    
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_start = telebot.types.KeyboardButton(text="/start")
    
    mini_app_url = os.getenv("MINI_APP_URL", "https://your-railway-url.up.railway.app")
    btn_app = telebot.types.KeyboardButton(text="Ilovani ochish 🚀", web_app=telebot.types.WebAppInfo(url=mini_app_url))
    
    markup.row(btn_start, btn_app)
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

# --- SIZ SO'RAGAN TO'LOV CHEKLARINI TUTISH LOGIKASI ---
@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    caption = message.caption.lower() if message.caption else ""
    user_first = message.from_user.first_name
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "Mavjud emas"
    
    # Foydalanuvchiga javob qaytarish
    bot.reply_to(
        message, 
        "🎉 **To'lov chekingiz qabul qilindi!**\nAdministrator tez orada chekni tekshiradi, to'lovingiz tasdiqlangach premium status faollashadi."
    )
    
    # Loyiha adminiga (Sizga) foydalanuvchi ma'lumotlari bilan chekni yo'naltirish
    try:
        bot.forward_message(ADMIN_CHAT_ID, message.chat.id, message.message_id)
        admin_info_text = (
            f"🔔 **Yangi To'lov Cheki Keldi!**\n\n"
            f"👤 Foydalanuvchi: {user_first}\n"
            f"🆔 Telegram ID: `{user_id}`\n"
            f"🌐 Username: {username}\n\n"
            f"⚠️ Agar to'lov to'g'ri bo'lsa, status berish uchun botga quyidagi buyruqni yuboring:\n"
            f"`/setpremium {user_id} 30`"
        )
        bot.send_message(ADMIN_CHAT_ID, admin_info_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Adminga chek yuborishda xato: {e}")

# --- ADMIN BUYRUG'I: Foydalanuvchiga premium status berish ---
@bot.message_handler(commands=['setpremium'])
def activate_premium_manual(message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.reply_to(message, "Format: `/setpremium TELEGRAM_ID KUN` (Masalan: `/setpremium 123456 30`)")
            return
        
        target_id = int(parts[1])
        days = int(parts[2])
        premium_duration = days * 24 * 3600
        until_timestamp = int(time.time()) + premium_duration
        
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("UPDATE users SET premium_status = 'PRO', premium_until = ? WHERE user_id = ?", (until_timestamp, target_id))
        conn.commit()
        conn.close()
        
        bot.reply_to(message, f"✅ ID: {target_id} bo'lgan foydalanuvchi {days} kunga PRO premium qilindi!")
        try:
            bot.send_message(target_id, f"👑 **To'lovingiz muvaffaqiyatli tasdiqlandi!** Akkauntingiz {days} kunga **Quiz Pilot PRO** tarifiga o'tkazildi. Cheksiz test yaratish imkoniyatidan foydalanishingiz mumkin!")
        except Exception as e:
            logging.error(f"Foydalanuvchini ogohlantirishda xato: {e}")
    except Exception as e:
        bot.reply_to(message, f"Xato yuz berdi: {e}")

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

@app.post("/api/create-quiz-web")
async def create_quiz_web(
    user_id: int = Form(...), 
    text: Optional[str] = Form(None), 
    file: Optional[UploadFile] = File(None),
    quiz_title: Optional[str] = Form(None)
):
    add_user_to_db(user_id)
    
    # --- PREMIUM VA LIMITLAR TEKSHIRUVI ---
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT premium_status, premium_until, free_quizzes_used, limit_reset_at FROM users WHERE user_id = ?", (user_id,))
    u_row = cursor.fetchone()
    
    if u_row:
        p_status = u_row["premium_status"]
        p_until = u_row["premium_until"]
        used_free = u_row["free_quizzes_used"]
        reset_at = u_row["limit_reset_at"]
        
        # Agar Premium muddati tugab qolgan bo'lsa, statusni avtomatik ravishda Free holatiga qaytarish
        if p_status != "Free" and now > p_until:
            cursor.execute("UPDATE users SET premium_status = 'Free', premium_until = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
            p_status = "Free"
            
        # Agar foydalanuvchining 30 kunlik bepul muddat davri o'tib ketgan bo'lsa, limitni 0 ga qaytarish
        if now > reset_at:
            cursor.execute("UPDATE users SET free_quizzes_used = 0, limit_reset_at = ? WHERE user_id = ?", (now + (30 * 24 * 3600), user_id))
            conn.commit()
            used_free = 0
            
        # Agar foydalanuvchi oddiy planda darslik yaratayotgan bo'lsa va 3 tadan ko'p yuklagan bo'lsa, to'xtatish
        if p_status == "Free" and used_free >= 3:
            conn.close()
            return {"status": "error", "message": "Sizning 30 kunlik bepul 3 ta test yaratish limitingiz tugadi. Iltimos, Premium sahifasiga o'tib faollashtiring!"}
            
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
                
                if file.filename.endswith('.pdf'):
                    reader = PdfReader(file_path)
                    raw_text = "".join([p.extract_text() + "\n" for p in reader.pages if p.extract_text()])
                    auto_title = file.filename.replace('.pdf', '')
                elif file.filename.endswith('.docx'):
                    doc = docx.Document(file_path)
                    raw_text = "\n".join([p.text for p in doc.paragraphs])
                    auto_title = file.filename.replace('.docx', '')
        except Exception as e:
            logging.error(f"Foydalanuvchi fayl yuklashda xato: {e}")
            
    if not raw_text.strip() and text:
        raw_text = text
        auto_text_clean = text.replace('\n', ' ').strip()
        auto_title = auto_text_clean[:18] + "..." if len(auto_text_clean) > 18 else auto_text_clean

    if not raw_text.strip():
        conn.close()
        return {"status": "error", "message": "Matn yoki darslikni o'qib bo'lmadi."}

    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    if not quiz_json_raw:
        conn.close()
        return {"status": "error", "message": "AI test generatsiya qila olmadi."}

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        if not items:
            conn.close()
            return {"status": "error", "message": "AI savollar ro'yxatini bo'sh qaytardi."}
            
        quiz_id = f"q_{int(time.time())}"
        final_title = quiz_title.strip() if (quiz_title and quiz_title.strip()) else auto_title
        
        cursor.execute(
            """INSERT INTO quizzes (id, user_id, title, total, answered, quiz_json, created_at, last_score, last_percent, is_public) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""", 
            (quiz_id, user_id, final_title[:30], len(items), 0, quiz_json_raw, int(time.time()), -1, -1)
        )
        
        # Free foydalanuvchining sarflangan bepul limit darsligini 1 taga oshirish
        if u_row and u_row["premium_status"] == "Free":
            cursor.execute("UPDATE users SET free_quizzes_used = free_quizzes_used + 1 WHERE user_id = ?", (user_id,))
            
        conn.commit()
        conn.close()

        try: 
            bot.send_message(user_id, f"🎉 **{final_title[:30]}** darsligi bo'yicha jami **{len(items)} ta** test savoli muvaffaqiyatli tayyorlandi!")
        except Exception as e: 
            logging.error(f"Telegram xabari yuborilmadi: {e}")

        return {"status": "ok"}
    except Exception as e:
        if conn: conn.close()
        return {"status": "error", "message": str(e)}

def generate_quiz_from_gemini(extracted_text):
    global current_key_index
    if not GOOGLE_API_KEYS: return None

    system_instruction = """You are an advanced AI quiz generator. 
CRITICAL RULES:
1. LANGUAGE RULE: Detect the language of the provided text. You MUST generate the questions, choices, and explanations in the EXACT SAME language as the input text.
2. QUESTION COUNT RULE: Look at the input text. If the user provided a strict list of questions, you MUST ONLY extract and format THOSE EXACT questions into the quiz structure. If it's a huge continuous textbook, you can generate up to 40-50 questions maximum."""

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
    total_users = get_users_count()
    
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    
    cursor.execute("SELECT id, title, total, answered, created_at, last_score, last_percent, is_public FROM quizzes WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    personal_rows = cursor.fetchall()
    
    # Premium ma'lumotlarini ham index.html uchun yuborish
    cursor.execute("SELECT language, premium_status, free_quizzes_used FROM users WHERE user_id = ?", (user_id,))
    lang_row = cursor.fetchone()
    
    user_lang = "uz"
    premium_status = "Free"
    free_quizzes_used = 0
    
    if lang_row:
        user_lang = lang_row["language"]
        premium_status = lang_row["premium_status"]
        free_quizzes_used = lang_row["free_quizzes_used"]
    
    conn.close()
    
    quizzes = [{
        "id": r["id"], 
        "title": r["title"], 
        "total": r["total"], 
        "answered": r["answered"], 
        "created_at": r["created_at"],
        "last_score": r["last_score"],
        "last_percent": r["last_percent"],
        "is_public": r["is_public"]
    } for r in personal_rows]
    
    # Kengaytirilgan ma'lumotlar uzatish: index.html yozuvlarini to'g'irlash uchun
    return {
        "status": "ok", 
        "quizzes": quizzes, 
        "total_users": total_users, 
        "user_lang": user_lang,
        "premium_status": premium_status,
        "free_quizzes_used": free_quizzes_used
    }

@app.get("/api/public-quizzes")
def get_public_quizzes():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("SELECT id, title, total, created_at FROM quizzes WHERE is_public = 1 ORDER BY created_at DESC LIMIT 50")
    rows = cursor.fetchall()
    conn.close()
    
    quizzes = [{"id": r["id"], "title": r["title"], "total": r["total"], "created_at": r["created_at"]} for r in rows]
    return {"status": "ok", "quizzes": quizzes}

@app.post("/api/toggle-public")
def toggle_public(quiz_id: str, user_id: int, is_public: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("UPDATE quizzes SET is_public = ? WHERE id = ? AND user_id = ?", (is_public, quiz_id, user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/set-language")
def set_language(user_id: int, lang: str):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/flashcards")
def get_flashcards(user_id: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("SELECT id, front, back FROM flashcards WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    cards = [{"id": r["id"], "front": r["front"], "back": r["back"]} for r in rows]
    return {"status": "ok", "cards": cards}

@app.post("/api/create-flashcard")
def create_flashcard(req: FlashcardCreateRequest):
    card_id = f"c_{int(time.time())}_{os.urandom(2).hex()}"
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("INSERT INTO flashcards VALUES (?, ?, ?, ?, ?)", (card_id, req.user_id, req.front, req.back, int(time.time())))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/delete-flashcard")
def delete_flashcard(card_id: str, user_id: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("DELETE FROM flashcards WHERE id = ? AND user_id = ?", (card_id, user_id))
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
    cursor.execute("UPDATE quizzes SET answered = total, last_score = ?, last_percent = ? WHERE id = ? AND user_id = ?", 
                   (data.correct_count, data.percent, data.quiz_id, data.user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/delete-quiz")
def delete_quiz(quiz_id: str, user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("DELETE FROM quizzes WHERE id = ? AND user_id = ?", (quiz_id, user_id))
        conn.commit()
        conn.close()
        return {"status": "ok", "message": "Test o'chirildi."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Xatolik.")

def start_bot_polling():
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except Exception as e:
            time.sleep(5)

@app.on_event("startup")
async def startup_event():
    polling_thread = threading.Thread(target=start_bot_polling, daemon=True)
    polling_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
