# -*- coding: utf-8 -*-
import logging
import json
import os
import sqlite3
import uuid
import asyncio

# Professional asinxron aiogram kutubxonasi
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Google GenAI yangi versiyasi
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List

# PDF va Word o'quvchilar
from pypdf import PdfReader
import docx

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873670048:AAHT1j9JOTcBp8hmu5SP1JDwlEHAUySeIJs")
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

GOOGLE_API_KEYS = [os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KzCuEHHBw1uDXcLR82sYNdoukSexyeImZpkftNys7Lwg")]
current_key_index = 0

DOWNLOADS_DIR = 'downloads'
DB_NAME = 'quiz_pilot.db'

# ---- DATABASES (SQLite) ----
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS quiz_sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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
    buttons = [[types.KeyboardButton(text='🗂️ Mening testlarim')]]
    return types.ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_inline_pagination(session_id: str, current_offset: int, total_count: int):
    builder = InlineKeyboardBuilder()
    if current_offset + 5 < total_count:
        builder.button(text="➡️ Keyingi 5 ta test", callback_data=f"next:{session_id}:{current_offset + 5}")
    return builder.as_markup()

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
@dp.message(CommandStart())
async def send_welcome(message: types.Message):
    user_name = message.from_user.first_name
    await message.answer(
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

@dp.message(F.text == '🗂️ Mening testlarim')
async def show_archive(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT session_id, title, created_at FROM quiz_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user_id,))
    sessions = cursor.fetchall()
    conn.close()

    if not sessions:
        await message.answer("🗂️ Sizda hali yaratilgan testlar arxivi mavjud emas.")
        return

    text = "📂 **Sizning oxirgi testlaringiz arxivi:**\n\n"
    builder = InlineKeyboardBuilder()
    for idx, (s_id, title, date) in enumerate(sessions, 1):
        text += f"{idx}. 📝 {title[:30]}... ({date[:10]})\n"
        builder.button(text=f"👁️ {idx}-testni ko'rish", callback_data=f"view:{s_id}:0")
    
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.message(F.document)
async def handle_docs(message: types.Message):
    try:
        file_name = message.document.file_name
        
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        file_path = os.path.join(DOWNLOADS_DIR, file_name)
        
        # Aiogram formatida faylni xavfsiz yuklab olish
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, file_path)

        if file_name.endswith('.pdf'):
            raw_text = read_pdf(file_path)
        elif file_name.endswith('.docx'):
            raw_text = read_docx(file_path)
        else:
            await message.answer("❌ Faqat PDF yoki DOCX formatidagi fayllarni yuboring.")
            return

        if not raw_text.strip():
            await message.answer("❌ Fayl ichidan matn o'qib bo'lmadi.")
            return

        await process_quiz_logic(message, raw_text, title=file_name)
    except Exception as e:
        logging.error(f"Fayl yuklashda xatolik: {e}")

@dp.message(F.text)
async def handle_text(message: types.Message):
    await process_quiz_logic(message, message.text, title=message.text[:20])

async def process_quiz_logic(message: types.Message, raw_text: str, title: str):
    status_msg = await message.answer("⏳ Gemini AI matnni tahlil qilib, testlar to'plamini tayyorlamoqda...")
    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    
    if not quiz_json_raw:
        await bot.delete_message(message.chat.id, status_msg.message_id)
        await message.answer("❌ Test yaratishda xatolik yuz berdi. Qaytadan urinib ko'ring.")
        return

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        
        if not items:
            await message.answer("❌ Matnga mos test savollari topilmadi.")
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

        await bot.delete_message(message.chat.id, status_msg.message_id)
        await send_quiz_chunk(message.chat.id, session_id, offset=0)

    except Exception as e:
        logging.error(f"Katta xatolik: {e}")
        await message.answer("❌ Ma'lumotlarni qayta ishlashda xatolik yuz berdi.")

async def send_quiz_chunk(chat_id: int, session_id: str, offset: int = 0):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM quiz_items WHERE session_id = ?", (session_id,))
    total_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT question, options, correct_index, explanation FROM quiz_items WHERE session_id = ? LIMIT 5 OFFSET ?", (session_id, offset))
    quizzes = cursor.fetchall()
    conn.close()

    for q_question, q_options, q_correct_index, q_explanation in quizzes:
        options = json.loads(q_options)[:4]
        correct_index = int(q_correct_index)
        if correct_index >= len(options): correct_index = 0
