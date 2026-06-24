# -*- coding: utf-8 -*-
import logging
import sqlite3
import json
import re
import requests
import os
from datetime import datetime, timedelta
from pypdf import PdfReader
import docx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Loggingni sozlash
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Dotenv orqali maxfiy kalitlarni yuklash
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Bot obyektini Railway uchun toza va proksisiz yaratish
application = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .build()
)

# Kalitlarni Railway'dan o'qiymiz
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
        "]\n"
        "Eslatma: options ichida maksimal 4 ta variant bo'lsin va correct_index to'g'ri javobning indeks raqami (0 dan 3 gacha) bo'lsin."
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
            else:
                logging.error(f"Gemini error status: {response.status_code}, response: {response.text}")
        except Exception as e:
            logging.error(f"API Xato: {e}")
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Assalomu alaykum, {user_name}!\n\n"
        "🚀 Men **Quiz Pilot Bot** — sizning intellektual yordamchingizman.\n\n"
        "📖 **Men nimalar qila olaman?**\n"
        "1️⃣ Menga istalgan mavzuni matn ko'rinishida yuboring (Masalan: 'Biologiya odam anatomiyasi')\n"
        "2️⃣ Menga **PDF** yoki **Word (.docx)** formatidagi darslik, konspekt yoki maqolalarni yuboring.\n\n"
        "📥 Qani boshladik, menga mavzu matnini yoki hujjat faylini yuboring!"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text
    await process_quiz_logic(update, context, raw_text, is_file=False)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    file_id = document.file_id
    file_name = document.file_name

    file = await context.bot.get_file(file_id)
    file_path = os.path.join(DOWNLOADS_DIR, file_name)

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    await file.download_to_drive(file_path)

    if file_name.endswith('.pdf'):
        raw_text = read_pdf(file_path)
    elif file_name.endswith('.docx'):
        raw_text = read_docx(file_path)
    else:
        await update.message.reply_text("❌ Faqat PDF yoki DOCX (Word) fayllarni yuboring.")
        return

    if not raw_text.strip():
        await update.message.reply_text("❌ Fayldan matnni o'qib bo'lmadi yoki fayl bo'sh.")
        return

    await process_quiz_logic(update, context, raw_text, is_file=True)

async def process_quiz_logic(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text, is_file=False):
    user_id = update.message.from_user.id
    today_str = str(datetime.today().date())
    user_data = get_user(user_id)

    if user_data["last_reset_date"] != today_str:
        user_data["tests_today"] = 0
        user_data["last_reset_date"] = today_str

    if user_data["tests_today"] >= 15:
        await update.message.reply_text("❌ Kunlik limitingiz (15 ta test) tugadi. Ertaga qayta urinib ko'ring.")
        return

    if user_data["last_test_time"]:
        try:
            last_time = datetime.strptime(user_data["last_test_time"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < last_time + timedelta(minutes=3):
                remaining = (last_time + timedelta(minutes=3)) - datetime.now()
                m, s = int(remaining.total_seconds() // 60), int(remaining.total_seconds() % 60)
                await update.message.reply_text(f"⏳ Kutish vaqti faol. Keyingi testni {m}m {s}s dan keyin yaratishingiz mumkin.")
                return
        except Exception as e:
            logging.error(f"Time parsing error: {e}")

    status_message = await update.message.reply_text("⏳ Sun'iy intellekt test savollarini tayyorlamoqda, iltimos kuting...")
    
    quiz_json_raw = generate_quiz_from_gemini(raw_text, is_file=is_file)
    
    if not quiz_json_raw:
        await status_message.edit_text("❌ Afsuski, test yaratishda xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring.")
        return

    try:
        quiz_data = json.loads(quiz_json_raw)
        await status_message.delete()
        
        for q in quiz_data:
            await update.message.reply_poll(
                question=q['question'],
                options=q['options'],
                correct_option_id=q['correct_index'],
                type='quiz',
                is_anonymous=False
            )
        
        # Statistikani yangilash
        update_user(user_id, user_data["tests_today"] + 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), today_str)
        
    except Exception as e:
        logging.error(f"JSON yoki Poll xatosi: {e}")
        await status_message.edit_text("❌ Test ma'lumotlarini o'qishda xatolik yuz berdi.")

def main():
    init_db()
    
    # Handlerlarni qo'shish
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    # Botni ishga tushirish
    logging.info("Bot ishga tushdi...")
    application.run_polling()

if __name__ == '__main__':
    main()
