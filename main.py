# -*- coding: utf-8 -*-
import logging
import json
import os
import sqlite3
import uuid
import asyncio
from pypdf import PdfReader
import docx

# Asinxron Telegram kutubxonasi
from telebot.async_telebot import AsyncTeleBot
from telebot import types

# Google GenAI yangi versiyasi
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List

# Logging sozlamalari
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Ekologik o'zgaruvchilar (Railway yoki .env uchun)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873670048:AAHT1j9JOTcBp8hmu5SP1JDwlEHAUySeIJs")
bot = AsyncTeleBot(TELEGRAM_BOT_TOKEN)

# API kalitlarni ro'yxat qilib olish (.env dan olish tavsiya etiladi)
GOOGLE_API_KEYS = [os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KzCuEHHBw1uDXcLR82sYNdoukSexyeImZpkftNys7Lwg")]
current_key_index = 0

DOWNLOADS_DIR = 'downloads'
DB_NAME = 'quiz_pilot.db'

# ---- DATABASES (SQLite) ----
def init_db():
    """Ma'lumotlar bazasini va jadvallarni yaratish"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Foydalanuvchilar arxivi (Katta mavzular kesimida)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS quiz_sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Har bir testning alohida matnlari
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS quiz_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            question TEXT,
            options TEXT,
            correct_index INTEGER,
            explanation TEXT,
            FOREIGN KEY(session_id) REFERENCES quiz_sessions(session_id)
        )
    ''')
    conn.commit()
    conn.close()

# ---- PYDANTIC SCHEMAS ----
class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="To'g'ri javob va 3 ta variantdan iborat jami 4 ta variant")
    correct_index: int = Field(description="To'g'ri javob indeksi (0 dan 3 gacha)")
    explanation: str = Field(description="Javob to'g'riligini isbotlovchi qoida (maksimal 180 ta belgi)")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")

# ---- KEYBOARDS ----
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.row(types.KeyboardButton('🗂️ Mening testlarim'))
    return markup

def get_inline_pagination(session_id, current_offset, total_count):
    markup = types.InlineKeyboardMarkup()
    if current_offset + 5 < total_count:
        # Callback data chekloviga tushmasligi uchun faqat session_id va offset yuboriladi
        markup.add(types.InlineKeyboardButton("➡️ Keyingi 5 ta test", callback_data=f"next:{session_id}:{current_offset + 5}"))
    return markup

# ---- FILE READERS ----
def read_pdf(file_path):
    try:
        reader = PdfReader(file_path)
        return "".join([page.extract_text() + "\n" for page in reader.pages if page.extract_text()])
    except Exception:
        return ""

def read_docx(file_path):
    try:
        doc = docx.Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs])
    except Exception:
        return ""

# ---- GEMINI GENERATION ----
def generate_quiz_from_gemini(extracted_text):
    global current_key_index
    
    system_instruction = (
        "Siz berilgan matnlar asosida interaktiv testlar yaratuvchi botsiz. "
        "Matndan kelib chiqib imkon qadar ko'proq (maksimal 25 tagacha) mukammal test savollari tuzing. "
        "Har bir variant boshiga qat'iy ravishda ketma-ketlikda 'A) ', 'B) ', 'C) ', 'D) ' qo'shing. "
        "Savol, variantlar va explanation matni foydalanuvchi yuborgan til bilan AYNAN BIR XIL bo'lishi shart! "
        "Explanation matni qat'iy ravishda 180 ta belgidan oshmasligi kerak. Berilgan sxemaga rioya qiling."
    )

    for _ in range(len(GOOGLE_API_KEYS)):
        api_key = GOOGLE_API_KEYS[current_key_index].strip()
        if not api_key:
            current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
            continue
            
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=extracted_text[:35000],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=QuizResponse,
                    temperature=0.6
                )
            )
            if response and response.text:
                return response.text
        except Exception as e:
            logging.error(f"Gemini API xatosi: {e}")
            
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
    return None

# ---- BOT HANDLERS ----
@bot.message_handler(commands=['start'])
async def send_welcome(message):
    user_name = message.from_user.first_name
    await bot.send_message(
        message.chat.id,
        f"👋 Assalomu alaykum, {user_name}!\n\n"
        "🚀 Men **Quiz Pilot Bot (Super Version)** — intellektual yordamchingizman.\n\n"
        "📖 **Imkoniyatlarim:**\n"
        "1️⃣ Matn yoki savollardan testlar yaratish.\n"
        "2️⃣ **PDF** yoki **Word (.docx)** kitoblarni testga aylantirish.\n"
        "3️⃣ Barcha testlarni bazada xavfsiz saqlash va arxivlash.\n\n"
        "📝 Menga matn yuboring yoki quyidagi menyudan foydalaning:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda message: message.text == '🗂️ Mening testlarim')
async def show_archive(message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT session_id, title, created_at FROM quiz_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user_id,))
    sessions = cursor.fetchall()
    conn.close()

    if not sessions:
        await bot.send_message(message.chat.id, "🗂️ Sizda hali yaratilgan testlar arxivi mavjud emas.")
        return

    text = "📂 **Sizning oxirgi testlaringiz arxivi:**\n\n"
    markup = types.InlineKeyboardMarkup()
    for idx, (s_id, title, date) in enumerate(sessions, 1):
        text += f"{idx}. 📝 {title[:30]}... ({date[:10]})\n"
        markup.add(types.InlineKeyboardButton(f"👁️ {idx}-testni ko'rish", callback_data=f"view:{s_id}:0"))
    
    await bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
async def handle_docs(message):
    try:
        file_name = message.document.file_name
        file_info = await bot.get_file(message.document.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        file_path = os.path.join(DOWNLOADS_DIR, file_name)
        
        with open(file_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        if file_name.endswith('.pdf'):
            raw_text = read_pdf(file_path)
        elif file_name.endswith('.docx'):
            raw_text = read_docx(file_path)
        else:
            await bot.send_message(message.chat.id, "❌ Faqat PDF yoki DOCX formatidagi fayllarni yuboring.")
            return

        if not raw_text.strip():
            await bot.send_message(message.chat.id, "❌ Fayl ichidan matn o'qib bo'lmadi.")
            return

        await process_quiz_logic(message, raw_text, title=file_name)
    except Exception as e:
        logging.error(f"Fayl yuklashda xatolik: {e}")

@bot.message_handler(func=lambda message: True)
async def handle_text(message):
    if message.text.startswith('/'):
        return
    await process_quiz_logic(message, message.text, title=message.text[:20])

async def process_quiz_logic(message, raw_text, title):
    status_msg = await bot.send_message(message.chat.id, "⏳ Gemini AI matnni tahlil qilib, testlar to'plamini tayyorlamoqda...")
    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    
    if not quiz_json_raw:
        try: await bot.delete_message(message.chat.id, status_msg.message_id)
        except: pass
        await bot.send_message(message.chat.id, "❌ Test yaratishda xatolik yuz berdi. Qaytadan urinib ko'ring.")
        return

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        
        if not items:
            await bot.send_message(message.chat.id, "❌ Matnga mos test savollari topilmadi.")
            return

        session_id = str(uuid.uuid4())
        user_id = message.from_user.id
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO quiz_sessions (session_id, user_id, title) VALUES (?, ?, ?)", (session_id, user_id, title))
        
        for q in items:
            cursor.execute(
                "INSERT INTO quiz_items (session_id, question, options, correct_index, explanation) VALUES (?, ?, ?, ?, ?)",
                (session_id, q['question'], json.dumps(q['options']), int(q['correct_index']), q.get('explanation', ''))
            )
        conn.commit()
        conn.close()

        try: await bot.delete_message(message.chat.id, status_msg.message_id)
        except: pass

        await send_quiz_chunk(message.chat.id, session_id, offset=0)

    except Exception as e:
        logging.error(f"Katta xatolik: {e}")
        await bot.send_message(message.chat.id, "❌ Ma'lumotlarni qayta ishlashda xatolik yuz berdi.")

async def send_quiz_chunk(chat_id, session_id, offset=0):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
