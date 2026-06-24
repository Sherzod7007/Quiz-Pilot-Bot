
            if response and response.text:
                return response.text
        except Exception as e:
            logging.error(f"Gemini API xatoligi: {e}")
            
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
    return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_name = message.from_user.first_name
    bot.send_message(
        message.chat.id,
        f"👋 Assalomu alaykum, {user_name}!\n\n"
        "🚀 Men **Quiz Pilot Bot** — sizning intellektual va super yordamchingizman.\n\n"
        "📖 **Men nimalar qila olaman?**\n"
        "1️⃣ Menga istalgan savollarni matn ko'rinishida yuboring (Hatto variantlar va javobi bo'lmasa ham)\n"
        "2️⃣ Menga savollar yozilgan **PDF** yoki **Word (.docx)** fayllarni yuboring.\n\n"
        "🎯 Men o'sha savollarga to'g'ri javobni o'zim topib, 4 ta variantli interaktiv viktorina test qilib beraman!",
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
            bot.send_message(message.chat.id, "❌ Faqat PDF yoki DOCX (Word) fayllarni yuboring.", reply_markup=get_main_keyboard())
            return

        if not raw_text.strip():
            bot.send_message(message.chat.id, "❌ Fayldan matnni o'qib bo'lmadi yoki fayl bo'sh.", reply_markup=get_main_keyboard())
            return

        process_quiz_logic(message, raw_text, is_file=True)
    except Exception as e:
        logging.error(f"Fayl yuklashda xato: {e}")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    if message.text == '/start' or message.text.startswith('/'):
        send_welcome(message)
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
        bot.send_message(message.chat.id, "❌ Kunlik limitingiz (15 ta test) tugadi. Ertaga qayta urinib ko'ring.", reply_markup=get_main_keyboard())
        return

    if user_data["last_test_time"]:
        try:
            last_time = datetime.strptime(user_data["last_test_time"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < last_time + timedelta(minutes=3):
                remaining = (last_time + timedelta(minutes=3)) - datetime.now()
                m, s = int(remaining.total_seconds() // 60), int(remaining.total_seconds() % 60)
                bot.send_message(message.chat.id, f"⏳ Kutish vaqti faol. Keyingi testni {m}m {s}s dan keyin yaratishingiz mumkin.", reply_markup=get_main_keyboard())
                return
        except Exception as e:
            logging.error(f"Vaqt xatosi: {e}")

    status_msg = bot.send_message(message.chat.id, "⏳ Sun'iy intellekt savollarga to'g'ri javoblarni topib, variantlar tayyorlamoqda...", reply_markup=get_main_keyboard())
    quiz_json_raw = generate_quiz_from_gemini(raw_text, is_file=is_file)
    
    if not quiz_json_raw:
        bot.edit_message_text("❌ Afsuski, test yaratishda xatolik yuz berdi.", chat_id=message.chat.id, message_id=status_msg.message_id)
        # Xato bo'lsa ham tugmani qayta tiklab qo'yamiz
        bot.send_message(message.chat.id, "Qayta urinib ko'rishingiz mumkin.", reply_markup=get_main_keyboard())
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
        logging.error(f"JSON xatosi: {e}")
            if response and response.text:
                return response.text
        except Exception as e:
            logging.error(f"Gemini API xatoligi: {e}")
            
        current_key_index = (current_key_index + 1) % len(GOOGLE_API_KEYS)
    return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_name = message.from_user.first_name
    bot.send_message(
        message.chat.id,
        f"👋 Assalomu alaykum, {user_name}!\n\n"
        "🚀 Men **Quiz Pilot Bot** — sizning intellektual va super yordamchingizman.\n\n"
        "📖 **Men nimalar qila olaman?**\n"
        "1️⃣ Menga istalgan savollarni matn ko'rinishida yuboring (Hatto variantlar va javobi bo'lmasa ham)\n"
        "2️⃣ Menga savollar yozilgan **PDF** yoki **Word (.docx)** fayllarni yuboring.\n\n"
        "🎯 Men o'sha savollarga to'g'ri javobni o'zim topib, 4 ta variantli interaktiv viktorina test qilib beraman!",
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
            bot.send_message(message.chat.id, "❌ Faqat PDF yoki DOCX (Word) fayllarni yuboring.", reply_markup=get_main_keyboard())
            return

        if not raw_text.strip():
            bot.send_message(message.chat.id, "❌ Fayldan matnni o'qib bo'lmadi yoki fayl bo'sh.", reply_markup=get_main_keyboard())
            return

        process_quiz_logic(message, raw_text, is_file=True)
    except Exception as e:
        logging.error(f"Fayl yuklashda xato: {e}")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    if message.text == '/start' or message.text.startswith('/'):
        send_welcome(message)
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
        bot.send_message(message.chat.id, "❌ Kunlik limitingiz (15 ta test) tugadi. Ertaga qayta urinib ko'ring.", reply_markup=get_main_keyboard())
        return

    if user_data["last_test_time"]:
        try:
            last_time = datetime.strptime(user_data["last_test_time"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < last_time + timedelta(minutes=3):
                remaining = (last_time + timedelta(minutes=3)) - datetime.now()
                m, s = int(remaining.total_seconds() // 60), int(remaining.total_seconds() % 60)
                bot.send_message(message.chat.id, f"⏳ Kutish vaqti faol. Keyingi testni {m}m {s}s dan keyin yaratishingiz mumkin.", reply_markup=get_main_keyboard())
                return
        except Exception as e:
            logging.error(f"Vaqt xatosi: {e}")

    status_msg = bot.send_message(message.chat.id, "⏳ Sun'iy intellekt savollarga to'g'ri javoblarni topib, variantlar tayyorlamoqda...", reply_markup=get_main_keyboard())
    quiz_json_raw = generate_quiz_from_gemini(raw_text, is_file=is_file)
    
    if not quiz_json_raw:
        bot.edit_message_text("❌ Afsuski, test yaratishda xatolik yuz berdi.", chat_id=message.chat.id, message_id=status_msg.message_id)
        # Xato bo'lsa ham tugmani qayta tiklab qo'yamiz
        bot.send_message(message.chat.id, "Qayta urinib ko'rishingiz mumkin.", reply_markup=get_main_keyboard())
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
        logging.error(f"JSON xatosi: {e}")
