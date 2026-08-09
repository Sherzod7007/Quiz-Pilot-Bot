import telebot
from datetime import datetime, timedelta

# Bot tokeningizni kiriting
TOKEN = 'YOUR_BOT_TOKEN_HERE'
bot = telebot.TeleBot(TOKEN)

# Ma'lumotlar bazasi o'rniga vaqtinchalik lug'at (Database)
# Haqiqiy loyihada buni SQLite yoki PostgreSQL bazasiga bog'laysiz
users_db = {
    # Misol uchun foydalanuvchi ma'lumotlari:
    # 12345678: {
    #     "is_vip": True,
    #     "vip_expiry": datetime(2026, 9, 9),
    #     "test_limit": 50,
    #     "created_tests": 0
    # }
}

def get_user_data(user_id):
    """Foydalanuvchi ma'lumotlarini olish yoki yangi foydalanuvchi yaratish"""
    if user_id not in users_db:
        users_db[user_id] = {
            "is_vip": False,
            "vip_expiry": None,
            "test_limit": 0,
            "created_tests": 0
        }
    return users_db[user_id]


@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    user = get_user_data(user_id)
    
    msg = "Xush kelibsiz!\n\n"
    if user["is_vip"]:
        msg += f"Sizning maqomingiz: **VIP (Teachers)**\n"
        msg += f"Qolgan testlar limiti: {user['test_limit'] - user['created_tests']}/{user['test_limit']}\n"
        msg += f"Amal qilish muddati: {user['vip_expiry'].strftime('%Y-%m-%d')}"
    else:
        msg += "Siz oddiy tarifdasiz. VIP tarifga o'tish uchun /buy_vip buyrug'ini yuboring."
    
    bot.reply_to(message, msg, parse_mode="Markdown")


@bot.message_handler(commands=['buy_vip'])
def buy_vip_command(message):
    user_id = message.from_user.id
    user = get_user_data(user_id)
    
    # Bu yerda to'lov muvaffaqiyatli amalga oshirildi deb hisoblaymiz
    # Foydalanuvchiga 30 kunlik VIP va 50 ta test limiti beriladi
    user["is_vip"] = True
    user["vip_expiry"] = datetime.now() + timedelta(days=30)
    user["test_limit"] = 50
    user["created_tests"] = 0  # Yangi oy uchun hisoblagichni nolga tushiramiz
    
    bot.reply_to(
        message, 
        "Tabriklaymiz! Siz **Teachers (VIP)** tarifini muvaffaqiyatli faollashtirdingiz.\n"
        "Sizga 30 kun davomida 50 ta test yaratish limiti berildi.",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=['create_test'])
def create_test_command(message):
    user_id = message.from_user.id
    user = get_user_data(user_id)
    
    # 1. VIP muddatini tekshirish
    if user["is_vip"] and user["vip_expiry"]:
        if datetime.now() > user["vip_expiry"]:
            user["is_vip"] = False
            bot.reply_to(message, "VIP tarifingiz muddati tugagan. Iltimos, tarifni yangilang.")
            return

    # 2. VIP maqomi va limitni tekshirish
    if not user["is_vip"]:
        bot.reply_to(message, "Test yaratish uchun sizda Teachers (VIP) tarifi faol bo'lishi kerak. /buy_vip")
        return

    qolgan_limit = user["test_limit"] - user["created_tests"]
    if qolgan_limit <= 0:
        bot.reply_to(message, "Oylik test yaratish limitikingiz (50 ta) tugadi. Keyingi oyda qayta urinib ko'ring.")
        return

    # 3. Test yaratish jarayoni
    user["created_tests"] += 1
    yangi_qoldiq = user["test_limit"] - user["created_tests"]
    
    bot.reply_to(
        message, 
        f"Test muvaffaqiyatli yaratildi!\n"
        f"Ushbu oyda qolgan testlar limitingiz: {yangi_qoldiq} ta."
    )


# Botni ishga tushirish
if __name__ == '__main__':
    print("Bot ishga tushdi...")
    bot.infinity_polling()
