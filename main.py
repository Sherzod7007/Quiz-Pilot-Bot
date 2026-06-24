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

# BU BLOK ENDI TO'G'RI JOYDA — START VA HUJJATLARDAN KEYIN TURIBDI
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    if message.text.startswith('/'):
        return
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
