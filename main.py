# -*- coding: utf-8 -*-
import logging
import json
import os
import hashlib
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873670048:AAHT1j9JOTcBp8hmu5SP1JDwlEHAUySeIJs")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

GOOGLE_API_KEYS = ["AQ.Ab8RN6KzCuEHHBw1uDXcLR82sYNdoukSexyeImZpkftNys7Lwg"]
current_key_index = 0

# SQLITE O'RNIGA HAQIQIY SMART XOTIRA (DICTIONARY) TIZIMI O'RNATILDI
USER_TEXTS = {}       # Foydalanuvchilarning arxiv matnlari
ACTIVE_SESSIONS = {}  # Foydalanuvchilarning faol test sahifalari (offset)

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="To'g'ri javob va 3 ta noto'g'ri variantdan iborat jami 4 ta variant ro'yxati")
    correct_index: int = Field(description="To'g'ri javob joylashtirilgan indeks raqami (0 dan 3 gacha)")
    explanation: str = Field(description="Ushbu javob nega to'g'riligini tushuntiruvchi qisqa qoida (maksimal 200 ta belgi)")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.row(types.KeyboardButton('/start'), types.KeyboardButton('🗂️ Mening testlarim'))
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

    system_instruction = (
        "Siz berilgan darslik matni segmenti asosida faqat o'zbek tilida 5 ta interaktiv test yaratuvchi botsiz. "
        "Matndagi ma'lumotlarga tayanib savol bering, to'g'ri javobni aniqlang va unga mos 3 ta noto'g'ri variant to'qing. "
        "Jami 4 ta variant bo'lsin va har bir variant boshiga qat'iy ravishda ketma-ketlikda "
        "'A) ', 'B) ', 'C) ', 'D) ' harflarini qo'shib yozing. "
        "Har bir savol uchun explanation maydoniga ushbu javob nega to'g'riligini isbotlovchi qisqa ilmiy qoidani yozing. "
        "DIQQAT: Savol, variantlar va explanation (tushuntirish) matni foydalanuvchi yuborgan matnning asl tili bilan aynan bir xil tilda bo'lishi shart! "
        "Agar matn ingliz tilida bo'lsa, test ham, explanation ham faqat ingliz tilida bo'lsin. Tarjima qilmang. "
        "Explanation matni qat'iy ravishda 200 ta belgidan oshmasligi kerak. Berilgan sxemaga amal qiling."
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
                contents=extracted_text,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=QuizResponse,
                    temperature=0.5
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
        "1️⃣ Menga matn yoki savollarni yuboring.\n"
        "2️⃣ Savollar/darsliklar yozilgan **PDF** yoki **Word (.docx)** fayllarni yuboring.\n"
        "3️⃣ Men ularni **5 tadan bo'lib** test qilaman va tarixingizga saqlab qo'yaman!\n\n"
        "🗂️ Oldingi testlaringizni qayta ishlash uchun pastdagi **'🗂️ Mening testlarim'** tugmasini bosing.",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda message: message.text == '🗂️ Mening testlarim')
def show_history(message):
    user_id = message.from_user.id
    if user_id not in USER_TEXTS or not USER_TEXTS[user_id]:
        bot.send_message(message.chat.id, "🗂️ Sizda hali saqlangan testlar mavjud emas. Biror fayl yoki matn yuboring.", reply_markup=get_main_keyboard())
        return
    
    markup = types.InlineKeyboardMarkup()
    # Eng oxirgi 10 ta yuklangan testni xotiradan chiqarish
    for idx, item in enumerate(USER_TEXTS[user_id][-10:]):
        markup.add(types.InlineKeyboardButton(text=f"📖 {item['title'][:30]}", callback_data=f"open_{idx}"))
    
    bot.send_message(message.chat.id, "🗂️ Saqlangan testlaringiz ro'yxati. Qayta ishlash uchun birortasini tanlang:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('open_'))
def open_text_callback(call):
    idx = int(call.data.split('_')[1])
    user_id = call.from_user.id
    
    if user_id in USER_TEXTS and idx < len(USER_TEXTS[user_id]):
        target_text = USER_TEXTS[user_id][idx]
        ACTIVE_SESSIONS[user_id] = {
            "title": target_text["title"],
            "content": target_text["content"],
            "offset": 0,
            "idx": idx
        }
        bot.answer_callback_query(call.id, "Test yuklanmoqda...")
        execute_quiz_generation(call.message, user_id)
    else:
        bot.answer_callback_query(call.id, "Xatolik: test topilmadi.")

@bot.callback_query_handler(func=lambda call: call.data == 'next_chunk')
def next_chunk_callback(call):
    bot.answer_callback_query(call.id, "Keyingi qism yuklanmoqda...")
    execute_quiz_generation(call.message, call.from_user.id)

@bot.message_handler(content_types=['document'])
def handle_docs(message):
    try:
        user_id = message.from_user.id
        file_name = message.document.file_name
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        file_path = file_name
        with open(file_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        if file_name.endswith('.pdf'):
            raw_text = read_pdf(file_path)
        elif file_name.endswith('.docx'):
            raw_text = read_docx(file_path)
        else:
            bot.send_message(message.chat.id, "❌ Faqat PDF yoki DOCX fayllarni yuboring.", reply_markup=get_main_keyboard())
            return

        try:
            os.remove(file_path)
        except:
            pass

        if not raw_text.strip():
            bot.send_message(message.chat.id, "❌ Fayl bo'sh yoki matn o'qilmadi.", reply_markup=get_main_keyboard())
            return

        # Matnni xavfsiz ichki xotiraga saqlash
        if user_id not in USER_TEXTS:
            USER_TEXTS[user_id] = []
        
        USER_TEXTS[user_id].append({"title": file_name, "content": raw_text})
        current_idx = len(USER_TEXTS[user_id]) - 1
        
        ACTIVE_SESSIONS[user_id] = {
            "title": file_name,
            "content": raw_text,
            "offset": 0,
            "idx": current_idx
        }
        
        execute_quiz_generation(message, user_id)
    except Exception as e:
        logging.error(f"Fayl xatosi: {e}")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    if message.text.startswith('/'):
        send_welcome(message)
        return
    
    title = message.text[:20] + "..." if len(message.text) > 20 else message.text
    
    if user_id not in USER_TEXTS:
        USER_TEXTS[user_id] = []
        
    USER_TEXTS[user_id].append({"title": title, "content": message.text})
    current_idx = len(USER_TEXTS[user_id]) - 1
    
    ACTIVE_SESSIONS[user_id] = {
        "title": title,
        "content": message.text,
        "offset": 0,
        "idx": current_idx
    }
    execute_quiz_generation(message, user_id)

def execute_quiz_generation(message, user_id):
    if user_id not in ACTIVE_SESSIONS:
        bot.send_message(message.chat.id, "❌ Hech qanday faol test seansi topilmadi.", reply_markup=get_main_keyboard())
        return

    session = ACTIVE_SESSIONS[user_id]
    full_text = session["content"]
    offset = session["offset"]
    
    # Har safar matndan 4000 ta belgini kesib olamiz
    chunk_size = 4000
    text_chunk = full_text[offset:offset+chunk_size].strip()
    
    if not text_chunk or len(text_chunk) < 5:
        bot.send_message(message.chat.id, f"🎉 '{session['title']}' darsligi bo'yicha barcha savollar tugadi!", reply_markup=get_main_keyboard())
        return

    status_msg = bot.send_message(message.chat.id, f"⏳ '{session['title'][:20]}' darsligidan test tayyorlanmoqda, kuting...", reply_markup=get_main_keyboard())
    quiz_json_raw = generate_quiz_from_gemini(text_chunk)
    
    try:
        bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
    except:
        pass

    if not quiz_json_raw:
        bot.send_message(message.chat.id, "❌ Afsuski, ushbu qismdan test yaratishda xatolik yuz berdi.", reply_markup=get_main_keyboard())
