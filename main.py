# -*- coding: utf-8 -*-
import logging
import sqlite3
import json
import requests
import os
from datetime import datetime, timedelta
from pypdf import PdfReader
import docx
import telebot

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873670048:AAGwfHZUV5Jc_JUFu0uw08UB0IS4cFZ1ceQ")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

GOOGLE_API_KEYS = os.getenv("GOOGLE_API_KEYS", "").split(",")
current_key_index = 0

DB_PATH = 'quiz_bot.db'
DOWNLOADS_DIR = 'downloads'

def init_db():
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

def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT tests_today, last_test_time, last_reset_date FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"tests_today": row[0], "last_test_time": row[1], "last_reset_date": row[2]}
    return {"tests_today": 0, "last_test_time": None, "last_reset_date": str(datetime.today().date())}

def update_user(user_id, tests_today, last_test_time, last_reset_date):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, tests_today, last_test_time, last_reset_date)
        VALUES (?, ?, ?, ?)
    ''', (user_id, tests_today, last_test_time, last_reset_date))
    conn.commit()
    conn.close()

def read_pdf(file_path):
    try:
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text
    except Exception as e:
        logging.error(f"PDF o'qishda xato: {e}")
        return ""

def read_docx(file_path):
    try:
        doc = docx.Document(file_path)
        text = [p.text for p in doc.paragraphs]
        return "\n".join(text)
    except Exception as e:
        logging.error(f"DOCX o'qishda xato: {e}")
        return ""

def generate_quiz_from_gemini(extracted_text, is_file=False):
    global current_key_index
    context_instruction = "Berilgan mavzu bo'yicha" if not is_file else "Quyidagi matn/hujjat ichidagi ma'lumotlarga asoslanib"

    system_instruction = (
        "Siz faqat o'zbek tilida interaktiv testlar yaratuvchi Quiz Pilot Bot ekansiz. "
        f"{context_instruction} qat'iy ravishda quyidagi JSON formatda 5 ta eng muhim test savolini qaytaring. "
        "Hech qanday kirish-chiqish matnlari yoki ```json o'ramlarini yozmang, faqat toza JSON bo'lsin.\n"
        "Format namunasi:\n"
        "[\n"
        "  {\n"
        "    \"question\": \"Savol matni?\",\n"
        "    \"options\": [\"A variant\", \"B variant\", \"C variant\", \"D variant\"],\n"
        "    \"correct_index\": 0\n"
        "  }\n"
        "]"
    )

    payload = {
        "contents": [{"parts": [{"text": extracted_text[:15000]}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.5
        }
    }

    for _ in range(len(GOOGLE_API_KEYS)):
        api_key = GOOGLE_API_KEYS[current_key_index].strip()
        if not api_key:
            current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
            continue
            
        url = f"https://googleapis.com{api_key}"
        try:
            response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=20)
            if response.status_code == 200:
                return response.json()['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            logging.error(f"API Xato: {e}")
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
    return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_name = message.from_user.first_name
    bot.reply_to(message, 
        f"👋 Assalomu alaykum, {user_name}!\n\n"
        "🚀 Men **Quiz Pilot Bot** — sizning intellektual yordamchingizman.\n\n"
        "📖 **Men nimalar qila olaman?**\n"
        "1️⃣ Menga istalgan mavzuni matn ko'rinishida yuboring (Masalan: 'Biologiya odam anatomiyasi')\n"
        "2️⃣ Menga **PDF** yoki **Word (.docx)** formatidagi darslik, konspekt yoki maqolalarni yuboring.\n\n"
        "📥 Qani boshladik, menga mavzu matnini yoki hujjat faylini yuboring!"
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
            bot.reply_to(message, "❌ Faqat PDF yoki DOCX (Word) fayllarni yuboring.")
            return

        if not raw_text.strip():
            bot.reply_to(message, "❌ Fayldan matnni o'qib bo'lmadi yoki fayl bo'sh.")
            return

        process_quiz_logic(message, raw_text, is_file=True)
    except Exception as e:
        logging.error(f"Fayl yuklashda xato: {e}")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    process_quiz_logic(message, message.text, is_file=False)

def process_quiz_logic(message, raw_text, is_file=False):
    user_id = message.from_user.id
    today_str = str(datetime.today().date())
    user_data = get_user(user_id)

    if user_data["last_reset_date"] != today_str:
        user_data["tests_today"] = 0
        user_data["last_reset_date"] = today_str

    if user_data["tests_today"] >= 15:
        bot.reply_to(message, "❌ Kunlik limitingiz (15 ta test) tugadi. Ertaga qayta urinib ko'ring.")
        return

    if user_data["last_test_time"]:
        try:
            last_time = datetime.strptime(user_data["last_test_time"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < last_time + timedelta(minutes=3):
                remaining = (last_time + timedelta(minutes=3)) - datetime.now()
                m, s = int(remaining.total_seconds() // 60), int(remaining.total_seconds() % 60)
                bot.reply_to(message, f"⏳ Kutish vaqti faol. Keyingi testni {m}m {s}s dan keyin yaratishingiz mumkin.")
                return
        except Exception as e:
            logging.error(f"Vaqt xatosi: {e}")

    status_msg = bot.reply_to(message, "⏳ Sun'iy intellekt test savollarini tayyorlamoqda, iltimos kuting...")
    quiz_json_raw = generate_quiz_from_gemini(raw_text, is_file=is_file)
    
    if not quiz_json_raw:
        bot.edit_message_text("❌ Afsuski, test yaratishda xatolik yuz berdi.", chat_id=message.chat.id, message_id=status_msg.message_id)
        return

    try:
        quiz_data = json.loads(quiz_json_raw)
        bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        
        for q in quiz_data:
            bot.send_poll(
                chat_id=message.chat.id,
                question=q['question'],
                options=q['options'],
                correct_option_id=q['correct_index'],
                type='quiz',
                is_anonymous=False
            )
        update_user(user_id, user_data["tests_today"] + 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), today_str)
    except Exception as e:
        logging.error(f"JSON xatosi: {e}")
        bot.edit_message_text("❌ Test ma'lumotlarini o'qishda xatolik yuz berdi.", chat_id=message.chat.id, message_id=status_msg.message_id)

if __name__ == '__main__':
    init_db()
    logging.info("Bot ishga tushdi...")
    bot.infinity_polling()
