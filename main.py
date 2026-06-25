# -*- coding: utf-8 -*-
import logging
import sqlite3
import json
import os
from datetime import datetime, timedelta
from pypdf import PdfReader
import docx
import telebot
from telebot import types
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873670048:AAGwfHZUV5Jc_JUFu0uw08UB0IS4cFZ1ceQ")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

GOOGLE_API_KEYS = os.getenv("GOOGLE_API_KEYS", "").split(",")
current_key_index = 0

DB_PATH = 'quiz_bot.db'
DOWNLOADS_DIR = 'downloads'

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="4 ta variant ro'yxati")
    correct_index: int = Field(description="To'g'ri javob indeksi (0-3)")

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.add(types.KeyboardButton('/start'))
    return markup

def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                tests_today INTEGER DEFAULT 0,
                last_test_time TEXT,
                last_reset_date TEXT
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Baza xatosi: {e}")

def get_user(user_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT tests_today, last_test_time, last_reset_date FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {"tests_today": int(row[0]), "last_test_time": row[1], "last_reset_date": row[2]}
    except Exception as e:
        logging.error(f"Baza o'qishda xato: {e}")
    return {"tests_today": 0, "last_test_time": None, "last_reset_date": str(datetime.today().date())}

def update_user(user_id, tests_today, last_test_time, last_reset_date):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, tests_today, last_test_time, last_reset_date)
            VALUES (?, ?, ?, ?)
        ''', (user_id, tests_today, last_test_time, last_reset_date))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Baza yozishda xato: {e}")

def read_pdf(file_path):
    try:
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            if page.extract_text():
                text += page.extract_text() + "\n"
        return text
    except Exception as e:
        return ""

def read_docx(file_path):
    try:
        doc = docx.Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs])
    except Exception as e:
        return ""

def generate_quiz_from_gemini(extracted_text):
    global current_key_index

    system_instruction = (
        "Siz berilgan savollar asosida faqat o'zbek tilida interaktiv testlar yaratuvchi botsiz. "
        "Foydalanuvchi bergan savolning to'g'ri javobini toping va unga mos 3 ta noto'g'ri variant to'qing. "
        "Jami 4 ta variant bo'lsin. Berilgan sxemaga qat'iy amal qiling."
    )

    for _ in range(len(GOOGLE_API_KEYS)):
        api_key = GOOGLE_API_KEYS[current_key_index].strip()
        if not api_key:
            current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
            continue
            
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-1.5-flash',
                contents=extracted_text[:15000],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=List[QuizItem],
                    temperature=0.7
                )
            )
            if response and response.text:
                return response.text
        except Exception as e:
            logging.error(f"Gemini API xatosi: {e}")
            
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
    return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_name = message.from_user.first_name
    bot.send_message(
        message.chat.id,
        f"👋 Assalomu alaykum, {user_name}!\n\n"
        "🚀 Men **Quiz Pilot Bot** — sizning super yordamchingizman.\n\n"
        "📖 **Men nimalar qila olaman?**\n"
        "1️⃣ Menga istalgan savollarni yuboring (Hatto variantlar va javobi bo'lmasa ham)\n"
        "2️⃣ Savollar yozilgan **PDF** yoki **Word (.docx)** fayllarni yuboring.\n\n"
        "🎯 Men to'g'ri javobni topib, 4 ta variantli interaktiv test qilib beraman!",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(content_types=['document'])
def handle_docs(message):
    try:
        file_name = message.document.file_name
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        file_path = os.path.join(DOWNLOADS_DIR, file_name)
        
        with open(file_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        if file_name.endswith('.pdf'):
            raw_text = read_pdf(file_path)
        elif file_name.endswith('.docx'):
            raw_text = read_docx(file_path)
        else:
            bot.send_message(message.chat.id, "❌ Faqat PDF yoki DOCX fayllarni yuboring.", reply_markup=get_main_keyboard())
            return

        if not raw_text.strip():
            bot.send_message(message.chat.id, "❌ Fayl bo'sh yoki matn o'qilmadi.", reply_markup=get_main_keyboard())
            return

        process_quiz_logic(message, raw_text)
    except Exception as e:
        logging.error(f"Fayl xatosi: {e}")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    if message.text == '/start' or message.text.startswith('/'):
        send_welcome(message)
        return
    process_quiz_logic(message, message.text)

def process_quiz_logic(message, raw_text):
    user_id = message.from_user.id
    today_str = str(datetime.today().date())
    user_data = get_user(user_id)

    if user_data["last_reset_date"] != today_str:
        user_data["tests_today"] = 0
        user_data["last_reset_date"] = today_str

    if user_data["tests_today"] >= 15:
        bot.send_message(message.chat.id, "❌ Kunlik limitingiz (15 ta test) tugadi.", reply_markup=get_main_keyboard())
        return

   ststus_msg = bot.send_message(
       message.chat.id,
       " Sun`iy intellekt javoblarni topib, test tayyorlamoqda...",
       reply_markup=get_main_keyboard()
   )

quiz_json_raw = generate_quiz_from_gemini(raw_text)

if not quiz_json_raw:
    bot.send_message(
        message.chat.id,
        " Test yaratishda hatolik yuz berdi."
    )
    return

    try:
        quiz_data = json.loads(quiz_json_raw)
        bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        
        for q in quiz_data:
            options = q['options'][:4]
            correct_index = int(q['correct_index'])
            if correct_index >= len(options):
                correct_index = 0
                
            bot.send_poll(
                chat_id=message.chat.id,
                question=q['question'],
                options=options,
                correct_option_id=correct_index,
                type='quiz',
                is_anonymous=False
            )
        update_user(user_id, user_data["tests_today"] + 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), today_str)
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Ma'lumotlarni o'qishda xatolik.", reply_markup=get_main_keyboard())

if __name__ == '__main__':
    init_db()
    logging.info("Bot ishga tushmoqda...")
    bot.infinity_polling()
