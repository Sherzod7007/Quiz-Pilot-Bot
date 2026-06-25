# -*- coding: utf-8 -*-
import logging
import sqlite3
import json
import os
from datetime import datetime
import docx
from pypdf import PdfReader
import telebot
from telebot import types
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_BOT_TOKEN = "8873670048:AAGwfHZUV5Jc_JUFu0uw08UB0IS4cFZ1ceQ"
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
GOOGLE_API_KEYS = os.getenv("GOOGLE_API_KEYS", "").split(",")
DB_PATH = 'quiz_bot.db'
DOWNLOADS_DIR = 'downloads'

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="To'g'ri javob va 3 ta noto'g'ri variant (jami 4 ta)")
    correct_index: int = Field(description="To'g'ri javob indeksi (0-3)")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, tests_today INTEGER, last_reset_date TEXT)''')
    conn.commit()
    conn.close()

def generate_quiz_from_gemini(extracted_text):
    system_instruction = "Siz o'zbek tilida interaktiv testlar yaratuvchi botsiz. Berilgan matn asosida 4 ta variantli (bitta to'g'ri) testlar tuzing. JSON formatida qaytaring."
    client = genai.Client(api_key=GOOGLE_API_KEYS[0].strip())
    try:
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=extracted_text[:12000],
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=QuizResponse
            )
        )
        return response.text
    except Exception as e:
        logging.error(f"Gemini API xatosi: {e}")
        return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.send_message(message.chat.id, "Salom! Menga mavzu yoki fayl yuboring, men test tuzib beraman.")

@bot.message_handler(content_types=['document'])
def handle_docs(message):
    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOADS_DIR, message.document.file_name)
    
    with open(file_path, 'wb') as f: f.write(downloaded_file)
    
    text = ""
    if file_path.endswith('.pdf'):
        reader = PdfReader(file_path)
        for page in reader.pages: text += page.extract_text() or ""
    elif file_path.endswith('.docx'):
        doc = docx.Document(file_path)
        text = "\n".join([p.text for p in doc.paragraphs])
    
    os.remove(file_path) # Faylni o'chirish
    process_quiz(message, text)

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    process_quiz(message, message.text)

def process_quiz(message, text):
    if not text.strip(): return
    msg = bot.send_message(message.chat.id, "⏳ Tayyorlanmoqda...")
    json_data = generate_quiz_from_gemini(text)
    
    if not json_data:
        bot.edit_message_text("❌ Xatolik yuz berdi.", message.chat.id, msg.message_id)
        return

    try:
        data = json.loads(json_data)
        bot.delete_message(message.chat.id, msg.message_id)
        for q in data.get("quizzes", []):
            # Telegram limitlari uchun qisqartirish
            q_text = (q['question'][:297] + '...') if len(q['question']) > 300 else q['question']
            options = [opt[:100] for opt in q['options'][:4]]
            
            bot.send_poll(message.chat.id, q_text, options, type='quiz', 
                          correct_option_id=min(q['correct_index'], len(options)-1), is_anonymous=False)
    except Exception as e:
        logging.error(f"Render xatosi: {e}")
        bot.send_message(message.chat.id, "❌ Testni formatlashda xatolik.")

if __name__ == '__main__':
    init_db()
    bot.infinity_polling()
