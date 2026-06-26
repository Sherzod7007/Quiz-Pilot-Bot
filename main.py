# -*- coding: utf-8 -*-
import logging
import json
import os
import requests  # To'g'ridan-to'g'ri ulanish uchun
from pypdf import PdfReader
import docx
import telebot
from telebot import types

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873670048:AAGwfHZUV5Jc_JUFu0uw08UB0IS4cFZ1ceQ")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

GOOGLE_API_KEYS = os.getenv("GOOGLE_API_KEYS", "").split(",")
current_key_index = 0

DOWNLOADS_DIR = 'downloads'

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

    # Qat'iy va tushunarli toza JSON buyrug'i
    system_instruction = (
        "Siz berilgan savollar asosida faqat o'zbek tilida interaktiv testlar yaratuvchi botsiz. "
        "Foydalanuvchi bergan savolning to'g'ri javobini toping va unga mos 3 ta noto'g'ri variant to'qing. "
        "Jami 4 ta variant bo'lsin. Faqat quyidagi JSON formatida javob bering, hech qanday kirish matnlari yozmang:\n"
        "[\n"
        "  {\n"
        "    \"question\": \"Savol matni?\",\n"
        "    \"options\": [\"1-variant\", \"2-variant\", \"3-variant\", \"4-variant\"],\n"
        "    \"correct_index\": 0\n"
        "  }\n"
        "]"
    )

    payload = {
        "contents": [{"parts": [{"text": extracted_text[:15000]}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.7
        }
    }

    for _ in range(len(GOOGLE_API_KEYS)):
        api_key = GOOGLE_API_KEYS[current_key_index].strip()
        if not api_key:
            current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
            continue
            
        # To'g'ridan-to'g'ri ulanish havolasi (Kutubxonasiz)
        url = f"https://googleapis.com{api_key}"
        
        try:
            response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=25)
            if response.status_code == 200:
                res_json = response.json()
                return res_json['candidates'][0]['content']['parts'][0]['text']
            else:
                logging.error(f"Gemini status xatosi: {response.status_code}")
        except Exception as e:
            logging.error(f"Ulanish xatosi: {e}")
            
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
        "2️⃣ Savollar yozilgan **PDF** yoki **Word (.docx)** formatidagi darsliklarni yuboring.\n\n"
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
    status_msg = bot.send_message(message.chat.id, "⏳ Sun'iy intellekt javoblarni topib, test tayyorlamoqda...", reply_markup=get_main_keyboard())
    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    
    if not quiz_json_raw:
        bot.edit_message_text("❌ Afsuski, test yaratishda xatolik yuz berdi. API kalitlarni yoki ulanishni tekshiring.", chat_id=message.chat.id, message_id=status_msg.message_id)
        return

    try:
        # Markdown o'ramlarini tozalash
        clean_json = quiz_json_raw.replace("```json", "").replace("```", "").strip()
        quiz_data = json.loads(clean_json)
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
    except Exception as e:
        logging.error(f"JSON xatosi: {e}")
        bot.send_message(message.chat.id, "❌ Test ma'lumotlarini o'qishda xatolik yuz berdi.", reply_markup=get_main_keyboard())

if __name__ == '__main__':
    logging.info("Bot ishga tushmoqda...")
    bot.infinity_polling()
