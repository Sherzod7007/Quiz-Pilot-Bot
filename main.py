# -*- coding: utf-8 -*-
import logging
import json
import os
import sqlite3
import uuid
import time
import telebot
from telebot import types

# Google GenAI va Pydantic sxemalari
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List

# PDF va Word o'quvchilar
from pypdf import PdfReader
import docx

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873670048:AAHT1j9JOTcBp8hmu5SP1JDwlEHAUySeIJs")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# ADMIN ID va API KALITLAR RO'YXATI
ADMIN_ID = 324575351  
GOOGLE_API_KEYS = [
    "AQ.Ab8RN6KzCuEHHBw1uDXcLR82sYNdoukSexyeImZpkftNys7Lwg",
    "AQ.Ab8RN6JRvaIQvqgs-3W-dP5pJvmYQMco3Xs99cqgah0_ar4U4g",
    "AQ.Ab8RN6JjtMJ_MbVkOB0wh--spHnVz_kLYrEi6rn31nvnS_Oxsg",
    "AQ.Ab8RN6K0S6Pok0j4NFFHKSjyQ_ks-75vhnSg73LlL07Hs4eAXg",
    "AQ.Ab8RN6LvgBcj7NiC_wiquNuGRmoQmQN945yTTPC7W52zQRxmbg",
    "AQ.Ab8RN6LMoKRl2AvvsaMHigzCaFgjSkIh9raQwh_srC-uMNn5-g",
    "AQ.Ab8RN6K6iedLHkBub1TJQjP7YNHaphRhQ8v7pze_9hfKQArhlQ"
]
current_key_index = 0

DOWNLOADS_DIR = 'downloads'
DB_NAME = 'quiz_pilot.db'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            score INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS quiz_sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER,
            title TEXT,
            start_time INTEGER,
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

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="To'g'ri javob va 3 ta variantdan iborat jami 4 ta variant")
    correct_index: int = Field(description="To'g'ri javob indeksi (0 dan 3 gacha)")
    explanation: str = Field(description="Javob to'g'riligini isbotlovchi qoida (maksimal 180 ta belgi)")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.row(types.KeyboardButton('🗂️ Mening testlarim'), types.KeyboardButton('🏆 Top foydalanuvchilar'))
    return markup

def get_inline_pagination(session_id, current_offset, total_count):
    markup = types.InlineKeyboardMarkup()
    if current_offset + 5 < total_count:
        markup.add(types.InlineKeyboardButton("➡️ Keyingi 5 ta test", callback_data=f"next:{session_id}:{current_offset + 5}"))
    else:
        markup.add(types.InlineKeyboardButton("📊 Natijalarni hisoblash", callback_data=f"result:{session_id}"))
    return markup

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

def generate_quiz_from_gemini(extracted_text):
    global current_key_index
    
    system_instruction = (
        "Siz berilgan matnlar asosida interaktiv testlar yaratuvchi botsiz. "
        "Matndan kelib chiqib imkon qadar ko'proq (maksimal 25 tagacha) mukammal test savollari tuzing. "
        "Har bir variant boshiga qat'iy ravishda ketma-ketlikda 'A) ', 'B) ', 'C) ', 'D) ' qo'shing. "
        "Savol, variantlar va explanation matni foydalanuvchi yuborgan til bilan AYNAN BIR XIL bo'lishi shart! "
        "Explanation matni qat'iy ravishda 180 ta belgidan oshmasligi kerak. Berilgan sxemaga rioya qiling."
    )

    for attempt in range(len(GOOGLE_API_KEYS)):
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
            error_message = str(e)
            logging.error(f"Kalit #{current_key_index+1} xatosi: {error_message}")
            
            if "429" in error_message or "ResourceExhausted" in error_message or "quota" in error_message.lower():
                try:
                    bot.send_message(
                        ADMIN_ID,
                        f"⚠️ **DIQQAT ADMIN!**\n\n"
                        f"🔑 Loyihadagi {current_key_index+1}-raqamli API kalitning kunlik limiti (Quota) tugadi!\n"
                        f"🔄 Bot keyingi kalitga avtomatik o'tmoqda.",
                        parse_mode="Markdown"
                    )
                except:
                    pass
            
            current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
            
    return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    username = message.from_user.username
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, first_name, username) VALUES (?, ?, ?)", (user_id, first_name, username))
    conn.commit()
    conn.close()

    bot.send_message(
        message.chat.id,
        f"👋 Assalomu alaykum, {first_name}!\n\n"
        "🚀 Matn yoki fayl yuboring, men ularni interaktiv testlarga aylantiraman va oxirida aniq natijangizni hisoblab beraman!\n\n"
        "🏆 Testlarni ko'proq yechib, eng kuchlilar reytingida peshqadam bo'ling!",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda message: message.text == '🏆 Top foydalanuvchilar')
def show_leaderboard(message):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT first_name, score FROM users ORDER BY score DESC LIMIT 10")
    top_users = cursor.fetchall()
    conn.close()

    if not top_users:
        bot.send_message(message.chat.id, "🏆 Reyting ro'yxati hali bo'sh. Birinchi bo'lib test yeching!")
        return

    text = "🏆 **Eng ko'p ball to'plagan TOP-10 foydalanuvchi:**\n\n"
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    
    for idx, (name, score) in enumerate(top_users):
        medal = medals[idx] if idx < len(medals) else "👤"
        text += f"{medal} {name} — **{score} ball**\n"
        
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM quiz_sessions")
    total_quizzes = cursor.fetchone()
    conn.close()

    admin_text = (
        "📊 **QUIZ PILOT BOT - ADMIN PANEL**\n\n"
        f"👥 Jami foydalanuvchilar: **{total_users[0]} ta**\n"
        f"📝 Jami yaratilgan testlar: **{total_quizzes[0]} ta**\n\n"
        "📢 Hamma foydalanuvchilarga xabar yuborish uchun: `/send_all xabar matni` deb yozing."
    )
    bot.send_message(message.chat.id, admin_text, parse_mode="Markdown")

@bot.message_handler(commands=['send_all'])
def send_all_users(message):
    if message.from_user.id != ADMIN_ID:
        return

    broadcast_text = message.text.replace("/send_all", "").strip()
    if not broadcast_text:
        bot.send_message(message.chat.id, "❌ Xato! Foydalanish: `/send_all xabar matni`")
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    all_users = cursor.fetchall()
    conn.close()

    success = 0
    failed = 0
    bot.send_message(message.chat.id, f"📢 {len(all_users)} ta foydalanuvchiga xabar yuborish boshlandi...")

    for (u_id,) in all_users:
        try:
            bot.send_message(u_id, broadcast_text, parse_mode="Markdown")
            success += 1
            time.sleep(0.05)
        except Exception:
            failed += 1

    bot.send_message(message.chat.id, f"✅ Yuborish yakunlandi!\n👍 Yetkazildi: {success}\n👎 Bloklaganlar: {failed}")

@bot.message_handler(func=lambda message: message.text == '🗂️ Mening testlarim')
def show_archive(message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT session_id, title, created_at FROM quiz_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user_id,))
    sessions = cursor.fetchall()
    conn.close()

    # 🛠️ PROBELLAR TO'LIQ TEKISLANDI: IndentationError xatosi yo'qoldi
