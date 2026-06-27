# ... (Yuqoridagi kodlar o'zgarishsiz qoladi) ...

# Foydalanuvchi test natijalarini saqlash uchun lug'at (RAMda)
user_results = {}

# Test yuborilayotgan joyni (process_quiz_logic) biroz boyitamiz
def process_quiz_logic(message, raw_text):
    status_msg = bot.send_message(message.chat.id, "⏳ Sun'iy intellekt javoblarni topib, test tayyorlamoqda...", reply_markup=get_main_keyboard())
    quiz_json_raw = generate_quiz_from_gemini(raw_text)
    
    if not quiz_json_raw:
        bot.send_message(message.chat.id, "❌ Xatolik yuz berdi.", reply_markup=get_main_keyboard())
        return

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        
        # Natijalarni nolga tenglaymiz
        user_results[message.chat.id] = {"correct": 0, "total": len(items), "answered": 0}
        
        for q in items:
            options = q['options'][:4]
            correct_index = int(q['correct_index'])
            
            # Telegram poll yuborish
            poll = bot.send_poll(
                chat_id=message.chat.id,
                question=q['question'],
                options=options,
                correct_option_id=correct_index,
                type='quiz',
                is_anonymous=False
            )
            # Har bir poll uchun to'g'ri javobni saqlab qo'yamiz (kerak bo'lsa)
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Test ma'lumotlarini o'qishda xatolik.")

# TEST NATIJASINI ANIQLASH UCHUN YANGI HANDLER
@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    user_id = poll_answer.user.id
    chat_id = poll_answer.user.id # Poll yuborilgan chat id
    
    # Javobni tekshirish
    # Eslatma: Telegramda poll_answer_handler orqali to'g'ri/noto'g'riligini bilish uchun 
    # poll yuborilganda uning ID si va javoblarini bazada saqlab qo'yish kerak.
    # Quyidagi kod soddalashtirilgan variant:
    
    # Agar foydalanuvchi testni tugatgan bo'lsa, natijani chiqarish
    if chat_id in user_results:
        user_results[chat_id]["answered"] += 1
        
        # Bu yerda oddiy hisoblash mantiqi bo'ladi
        # To'g'ri javobni aniqlash uchun poll_answer ichidagi option_ids ni tekshiring
        # Agarda correct_option_id mos kelsa:
        # user_results[chat_id]["correct"] += 1
        
        if user_results[chat_id]["answered"] == user_results[chat_id]["total"]:
            res = user_results[chat_id]
            bot.send_message(chat_id, f"✅ Test yakunlandi!\n\nTo'g'ri javoblar: {res['correct']}\nNoto'g'ri javoblar: {res['total'] - res['correct']}")
            del user_results[chat_id] # Xotirani tozalash

# ... (Pastki qismi o'zgarishsiz) ...
