# -*- coding: utf-8 -*-
import logging
import json
import os
import threading
import time
from pypdf import PdfReader
import docx
import telebot
from telebot import types
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List
from flask import Flask, jsonify, request, render_template

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN topilmadi!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
flask_app = Flask(__name__)

PORT = int(os.getenv("PORT", 5000))
RAILWAY_PUBLIC_URL = os.getenv("RAILWAY_PUBLIC_URL", "")

raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
current_key_index = 0

DOWNLOADS_DIR = 'downloads'
STORE_FILE = 'quiz_store.json'
global_quiz_data = {}

if os.path.exists(STORE_FILE):
    try:
        with open(STORE_FILE, 'r', encoding='utf-8') as f:
            global_quiz_data = json.load(f)
        logging.info("Eski testlar fayldan muvaffaqiyatli yuklandi.")
    except Exception as e:
        logging.error(f"Fayldan o'qishda xatolik: {e}")
        global_quiz_data = {}

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="To'g'ri javob va 3 ta noto'g'ri variantdan iborat jami 4 ta variant ro'yxati")
    correct_index: int = Field(description="To'g'ri javob joylashtirilgan indeks raqami (0 dan 3 gacha)")
    explanation: str = Field(description="Ushbu javob nega to'g'riligini tushuntiruvchi qisqa qoida (maksimal 200 ta belgi)")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.add(types.KeyboardButton('/start'))
    return markup

def read_pdf(file_path):
    try:
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            if page.extract_text():
                text += page.extract_text() + "\n"
        return text
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
    if not GOOGLE_API_KEYS:
        logging.error("Google API kalitlari ro'yxati bo'sh!")
        return None

    system_instruction = (
        "Siz berilgan savollar yoki matnlar asosida interaktiv testlar yaratuvchi botsiz. "
        "Foydalanuvchi bergan savolning to'g'ri javobini toping va unga mos 3 ta noto'g'ri variant to'qing. "
        "Jami 4 ta variant bo'lsin va har bir variant boshiga qat'iy 'A) ', 'B) ', 'C) ', 'D) ' harflarini qo'shing. "
        "Explanation maydoniga ushbu javob nega to'g'riligini isbotlovchi qisqa ilmiy qoidani yozing. "
        "DIQQAT: Savol, variantlar va explanation foydalanuvchi yuborgan matnning asl tili bilan bir xil tilda bo'lishi shart! "
        "Explanation matni qat'iy ravishda 200 ta belgidan oshmasligi kerak."
    )

    attempts = 0
    while attempts < len(GOOGLE_API_KEYS):
        api_key = GOOGLE_API_KEYS[current_key_index].strip()
        if not api_key:
            current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
            attempts += 1
            continue
        try:
            logging.info(f"Ishlatilayotgan API Key indeksi: {current_key_index}")
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=extracted_text[:15000],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=QuizResponse,
                    temperature=0.7
                )
            )
            if response and response.text:
                return response.text
        except Exception as e:
            logging.error(f"Gemini API xatosi (Indeks: {current_key_index}): {e}")
        
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
        attempts += 1
        
    return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_name = message.from_user.first_name
    bot.send_message(
        message.chat.id,
        f"👋 Assalomu alaykum, {user_name}!\n\n🚀 Men **Quiz Pilot Bot** — darsliklardan chiroyli mobil ilova ko'rinishidagi testlar yaratuvchi yordamchingizman.\n\nMenga matn yoki **PDF/DOCX** fayl yuboring!",
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
            bot.send_message(message.chat.id, "❌ Fayl bo'sh.", reply_markup=get_main_keyboard())
            return

        threading.Thread(target=process_quiz_logic, args=(message, raw_text), daemon=True).start()
    except Exception as e:
        logging.error(f"Fayl xatosi: {e}")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    if message.text == '/start' or message.text.startswith('/'):
        send_welcome(message)
        return
    threading.Thread(target=process_quiz_logic, args=(message, message.text), daemon=True).start()

def process_quiz_logic(message, raw_text):
    status_msg = bot.send_message(message.chat.id, "⏳ Sun'iy intellekt darslik asosida chiroyli dastur interfeysini tayyorlamoqda...", reply_markup=get_main_keyboard())
    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    if not quiz_json_raw:
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        except Exception:
            pass
        bot.send_message(message.chat.id, "❌ Afsuski, barcha API kalitlar limitga tushgan yoki xato. Kalitlarni tekshiring.", reply_markup=get_main_keyboard())
        return

    try:
        quiz_data = json.loads(quiz_json_raw)
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        except Exception:
            pass
            
        items = quiz_data.get("quizzes", [])
        if not items:
            bot.send_message(message.chat.id, "❌ Matndan test yaratib bo'lmadi.", reply_markup=get_main_keyboard())
            return

        user_id = str(message.from_user.id)
        global_quiz_data[user_id] = items

        try:
            with open(STORE_FILE, 'w', encoding='utf-8') as f:
                json.dump(global_quiz_data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logging.error(f"Faylga yozishda xatolik: {e}")

        markup = types.InlineKeyboardMarkup()
        app_url = f"{RAILWAY_PUBLIC_URL}/quiz?user_id={user_id}"
        markup.add(types.InlineKeyboardButton(text="📱 Testni Ilovada Boshlash", web_app=types.WebAppInfo(url=app_url)))

        bot.send_message(
            message.chat.id, 
            "📚 **Test savollari tayyor!**\n\n🎯 Jami savollar yuklandi.\n\nPastdagi tugmani bosing va maxsus qora fondagi interfeysda testni yeching 👇", 
            reply_markup=markup
        )
    except Exception as e:
        logging.error(f"Xatolik: {e}")

# ----------------- TELEGRAM MINI APP -----------------

@flask_app.route('/quiz_data', methods=['GET'])
def get_quiz_data_api():
    # TO'G'RILANDI: JSON kalitlari string ko'rinishida bo'lgani uchun barcha so'rovlar qat'iy string shakliga o'tkazilib qidiriladi
    user_id_raw = request.args.get('user_id', '')
    user_id_str = str(user_id_raw).strip()
    
    data = global_quiz_data.get(user_id_str, [])
    return jsonify(data)

@flask_app.route('/quiz')
def quiz_page():
    user_id = request.args.get('user_id', '')
    return render_template('quiz.html', user_id=user_id)

def run_flask():
    logging.info(f"Flask veb-server {PORT} portida ishga tushmoqda...")
    flask_app.run(host='0.0.0.0', port=PORT, use_reloader=False)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    logging.info("Sinxron Telegram Bot infinity_polling rejimida ishga tushdi...")
    bot.infinity_polling()
