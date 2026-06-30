# -*- coding: utf-8 -*-
import logging
import json
import os
import time
from pypdf import PdfReader
import docx
import telebot
from telebot import types
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List

# Logging sozlamalari
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Telegram Bot Token (Railway Variables panelidan avtomat o'qiydi)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN topilmadi! Railway paneliga kiritganingizga ishonch hosil qiling.")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Google API kalitlari ro'yxati (Railway Variables panelidan avtomat o'qiydi)
raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
current_key_index = 0

DOWNLOADS_DIR = 'downloads'
user_quiz_sessions = {}
poll_to_user_map = {}

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
        "Foydalanuvchi bergan savol/matnlar ichidan maksimal darajada ko'p (kamida 25-30 tagacha agar matn yetarli bo'lsa) test savollari yarating. "
        "Foydalanuvchi bergan savolning to'g'ri javobini toping va unga mos 3 ta noto'g'ri variant to'qing. "
        "Jami 4 ta variant bo'lsin va har bir variant boshiga qat'iy ravishda ketma-ketlikda "
        "'A) ', 'B) ', 'C) ', 'D) ' harflarini qo'shib yozing. "
        "Har bir savol uchun explanation maydoniga ushbu javob nega to'g'riligini isbotlovchi qisqa ilmiy qoidani yozing. "
        "DIQQAT: Savol, variantlar va explanation matni foydalanuvchi yuborgan savol/matnning asl tili bilan aynan bir xil tilda bo'lishi shart! "
        "Explanation matni qat'iy ravishda 200 ta belgidan oshmasligi kerak."
    )

    for _ in range(len(GOOGLE_API_KEYS)):
        api_key = GOOGLE_API_KEYS[current_key_index].strip()
        if not api_key:
            current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
            continue
        try:
            logging.info(f"Ishlatilayotgan API Key indeksi: {current_key_index}")
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=extracted_text[:25000],
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
    return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_name = message.from_user.first_name
    bot.send_message(
        message.chat.id,
        f"👋 Assalomu alaykum, {user_name}!\n\n"
        "🚀 Men **Quiz Pilot Bot** — sizning super va intellektual yordamchingizman.\n\n"
        "📖 **Men nimalar qila olaman?**\n"
        "1️⃣ Menga istalgan savollarni yuboring (Hatto variantlar va javobi bo'lmasa ham)\n"
        "2️⃣ Savollar yozilgan **PDF** yoki **Word (.docx)** formatidagi darsliklarni yuboring.\n\n"
        "🎯 Men to'g'ri javobni topib, variantlar tuzaman va xato qilsangiz qoidasini ham tushuntirib beraman!",
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
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        except Exception:
            pass
        bot.send_message(message.chat.id, "❌ Afsuski, test yaratishda xatolik yuz berdi. API kalitlarni tekshiring.", reply_markup=get_main_keyboard())
        return

    try:
        quiz_data = json.loads(quiz_json_raw)
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        except Exception:
            pass
        items = quiz_data.get("quizzes", [])
        user_id = message.from_user.id
        user_quiz_sessions[user_id] = {
            "correct_count": 0,
            "incorrect_count": 0,
            "total_questions": len(items),
            "answered_questions": 0,
            "poll_map": {},
            "chat_id": message.chat.id
        }
        for idx, q in enumerate(items, start=1):
            options = q['options'][:4]
            correct_index = int(q['correct_index'])
            if correct_index >= len(options):
                correct_index = 0
            explanation_text = q.get('explanation', '')[:200]
            numbered_question = f"{idx}. {q['question']}"
            poll_msg = bot.send_poll(
                chat_id=message.chat.id,
                question=numbered_question,
                options=options,
                correct_option_id=correct_index,
                type='quiz',
                explanation=explanation_text,
                is_anonymous=False
            )
            p_id = poll_msg.poll.id
            user_quiz_sessions[user_id]["poll_map"][p_id] = correct_index
            poll_to_user_map[p_id] = user_id
            
            # Telegram spam-blokidan o'tish uchun 0.5 soniya pauza
            time.sleep(0.5)
            
    except Exception as e:
        logging.error(f"JSON yoki Poll xatosi: {e}")
        bot.send_message(message.chat.id, "❌ Ma'lumotlarni qayta ishlashda xatolik yuz berdi.", reply_markup=get_main_keyboard())

# --- FOYDALANUVCHI JAVOBINI TEKSHIRISH VA NATIJANI CHIQARISH ---
@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    poll_id = poll_answer.poll_id
    chosen_options = poll_answer.option_ids

    if poll_id not in poll_to_user_map:
        return

    user_id = poll_to_user_map[poll_id]
    if user_id not in user_quiz_sessions:
        return

    session = user_quiz_sessions[user_id]
    poll_map = session.get("poll_map", {})

    if poll_id in poll_map:
        correct_index = poll_map[poll_id]
        user_chosen = chosen_options if chosen_options else -1

        if user_chosen == correct_index:
            session["correct_count"] += 1
        else:
            session["incorrect_count"] += 1

        session["answered_questions"] += 1

        if session["answered_questions"] >= session["total_questions"]:
            total = session["total_questions"]
            correct = session["correct_count"]
            incorrect = session["incorrect_count"]
            percentage = int((correct / total) * 100) if total > 0 else 0

            result_text = f"📊 Sizning test natijangiz tayyor!\n\n✅ To'g'ri javoblar: {correct} ta\n❌ Noto'g'ri javoblar: {incorrect} ta\n📝 Umumiy savollar: {total} ta\n🎯 Ko'rsatkich: {percentage}%"
