# -*- coding: utf-8 -*-
import logging
import json
import os
import time
import sqlite3
import telebot
from telebot import types

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN topilmadi!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=True)
DB_PATH = "/data/quiz_pilot.db" if os.path.exists("/data") else "quiz_pilot.db"

def get_side_by_side_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    url = os.getenv("WEBAPP_URL", "")
    if url:
        url = url if url.startswith("http") else f"https://{url}"
        markup.add(
            types.KeyboardButton('/start'),
            types.KeyboardButton(text="Ilovani ochish 🚀", web_app=types.WebAppInfo(url=url))
        )
    else:
        markup.add(types.KeyboardButton('/start'))
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if os.getenv("WEBAPP_URL"):
        try:
            url = os.getenv("WEBAPP_URL")
            url = url if url.startswith("http") else f"https://{url}"
            bot.set_chat_menu_button(
                chat_id=message.chat.id,
                menu_button=types.MenuButtonWebApp(type="web_app", text="Ilovani ochish 🚀", web_app=types.WebAppInfo(url=url))
            )
        except Exception as e:
            logging.error(f"Menu xatosi: {e}")
    
    user_name = message.from_user.first_name
    bot.send_message(
        message.chat.id, 
        f"👋 Salom, {user_name}! **Quiz Pilot Super Mini App** tizimiga xush kelibsiz.\n\n"
        "⚡ **Yangi Yangilanish:**\n"
        "🔥 Endi tizimimiz bitta darslikdan **50 tagacha mukammal va xatosiz test savollarini** qabul qila oladi va tayyorlaydi!\n\n"
        "🚀 Marhamat, pastdagi yonma-yon turgan tugmalardan foydalanib ilovani oching, darsligingizni yuklang va testlarni silliq ishlang!",
        reply_markup=get_side_by_side_keyboard()
    )

if __name__ == "__main__":
    logging.info("Bot Active Polling rejimida ishga tushmoqda...")
    try:
        bot.remove_webhook()
        bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
    except Exception as e:
        logging.error(f"Bot xatosi: {e}")
