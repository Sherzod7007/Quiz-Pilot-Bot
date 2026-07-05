# -*- coding: utf-8 -*-
import logging
import json
import os
import time
import sqlite3
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
        return {"status": "error", "message": "Matn yoki darslikni o'qib bo'lmadi. Iltimos matn kiriting yoki fayl yuklang."}

    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    if not quiz_json_raw:
        return {"status": "error", "message": "AI test generatsiya qila olmadi. Kalitlarni yoki matnni tekshiring."}

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        if not items:
            return {"status": "error", "message": "AI savollar ro'yxatini bo'sh qaytardi."}
            
        quiz_id = f"q_{int(time.time())}"
        
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("INSERT INTO quizzes VALUES (?, ?, ?, ?, ?, ?, ?)", (quiz_id, user_id, title[:22], len(items), 0, quiz_json_raw, int(time.time())))
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

    # Majburiy til qoidasi va qat'iy tizim buyrug'i
    system_instruction = """You are an advanced AI quiz generator. Your main goal is to create test questions based STRICTLY on the language of the input text.
CRITICAL LANGUAGE RULE: 
- Detect the language of the provided textbook/text.
- You MUST generate the questions, choices, and explanations in the EXACT SAME language as the input text.
- If the input text is in English, the questions, options (A, B, C, D), and explanation MUST BE IN ENGLISH. Do NOT translate into Uzbek or Russian.
- If the input text is in Uzbek, everything must be in Uzbek.

Task: Create 50 unique multiple-choice questions based on the text. Each question must have 1 correct answer and 3 incorrect options. Prefix each option with 'A) ', 'B) ', 'C) ', 'D) '. Write a scientific, brief explanation for the correct choice inside the explanation field (in the same language as the quiz)."""

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
                    temperature=0.3
                )
            )
            if response and response.text: return response.text
        except Exception as e:
            logging.error(f"Gemini API xatosi (Key index {current_key_index}): {e}")
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
    return None

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
    quizzes = [{"id": r["id"], "title": r["title"], "total": r["total"], "answered": r["answered"], "created_at": r["created_at"]} for r in rows]
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
    cursor.execute("UPDATE quizzes SET answered = total WHERE id = ? AND user_id = ?", (data.quiz_id, data.user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/stats")
def get_web_stats():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("SELECT COUNT(*) FROM users")
    u_count = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM quizzes")
    q_count = cursor.fetchone()
    conn.close()
    return {"status": "ok", "total_users": u_count[0] if u_count else 0, "total_quizzes": q_count[0] if q_count else 0}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
