# -*- coding: utf-8 -*-
import docx
import asyncio
import re
from docx import Document
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from google import genai
from google.genai import types as genai_types
import json
import logging
import os
from pydantic import BaseModel, Field
from pypdf import PdfReader
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
import psycopg2
from psycopg2.extras import RealDictCursor
import telebot
import threading
import time
from typing import List, Optional
import uvicorn
import uuid
import random
import secrets
from copy import deepcopy
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)
templates = Jinja2Templates(directory="templates")

raw_admin_id = os.getenv("ADMIN_ID")
try:
    ADMIN_ID = int(raw_admin_id.strip()) if raw_admin_id else None
except Exception as e:
    logging.error(f"ADMIN_ID ni int ga o'tkazishda xato: {e}")
    ADMIN_ID = None

# --- ADMIN SUPPORT ---
support_waiting_users = set()
support_reply_targets = {}

raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = (
    [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
)
current_key_index = 0
key_lock = threading.Lock()

# --- PROFESSIONAL AI QUEUE + RETRY + TIMEOUT + CONCURRENCY PROTECTION ---
AI_MAX_CONCURRENT = max(1, int(os.getenv("AI_MAX_CONCURRENT", str(min(7, max(1, len(GOOGLE_API_KEYS)))))))
AI_MAX_QUEUE = max(AI_MAX_CONCURRENT, int(os.getenv("AI_MAX_QUEUE", "150")))
AI_REQUEST_TIMEOUT = max(60, int(os.getenv("AI_REQUEST_TIMEOUT", "600")))
AI_TOTAL_TIMEOUT = max(AI_REQUEST_TIMEOUT, int(os.getenv("AI_TOTAL_TIMEOUT", "1800")))
AI_RETRY_PER_KEY = max(1, min(3, int(os.getenv("AI_RETRY_PER_KEY", "2"))))

ai_queue_slots = threading.BoundedSemaphore(AI_MAX_QUEUE)
gemini_semaphore = threading.BoundedSemaphore(AI_MAX_CONCURRENT)
logging.info(
    "AI protection initialized | concurrent=%s | queue=%s | request_timeout=%ss | total_timeout=%ss",
    AI_MAX_CONCURRENT, AI_MAX_QUEUE, AI_REQUEST_TIMEOUT, AI_TOTAL_TIMEOUT
)

# --- PROFESSIONAL FILE PROTECTION LAYER ---
MAX_UPLOAD_FILE_MB = max(1, int(os.getenv("MAX_UPLOAD_FILE_MB", "20")))
MAX_UPLOAD_FILE_BYTES = MAX_UPLOAD_FILE_MB * 1024 * 1024
MAX_PDF_PAGES = max(1, int(os.getenv("MAX_PDF_PAGES", "150")))
MAX_EXTRACTED_TEXT_CHARS = max(10000, int(os.getenv("MAX_EXTRACTED_TEXT_CHARS", "300000")))
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".docx"}

FILE_PROTECTION_MESSAGES = {
    "uz": {
        "unsupported": "Faqat PDF yoki DOCX fayl yuklash mumkin.",
        "too_large": f"Fayl hajmi {MAX_UPLOAD_FILE_MB} MB limitdan oshdi. Iltimos, faylni kichikroq qismlarga bo'lib yuboring.",
        "too_many_pages": f"PDF sahifalari soni {MAX_PDF_PAGES} ta limitdan oshdi. Iltimos, PDF faylni qismlarga bo'ling.",
        "too_much_text": "Fayldagi matn hajmi juda katta. Iltimos, faylni kichikroq qismlarga bo'lib yuboring.",
        "unreadable": "Faylni o'qib bo'lmadi. Matnli PDF yoki to'g'ri DOCX fayl yuboring.",
    },
    "ru": {
        "unsupported": "Можно загрузить только файл PDF или DOCX.",
        "too_large": f"Размер файла превышает лимит {MAX_UPLOAD_FILE_MB} МБ. Пожалуйста, разделите файл на меньшие части.",
        "too_many_pages": f"Количество страниц PDF превышает лимит {MAX_PDF_PAGES}. Пожалуйста, разделите PDF на части.",
        "too_much_text": "Объём текста в файле слишком большой. Пожалуйста, разделите файл на меньшие части.",
        "unreadable": "Не удалось прочитать файл. Отправьте текстовый PDF или корректный DOCX-файл.",
    },
    "en": {
        "unsupported": "Only PDF or DOCX files can be uploaded.",
        "too_large": f"The file exceeds the {MAX_UPLOAD_FILE_MB} MB limit. Please split it into smaller parts.",
        "too_many_pages": f"The PDF exceeds the {MAX_PDF_PAGES}-page limit. Please split the PDF into smaller parts.",
        "too_much_text": "The amount of text in the file is too large. Please split the file into smaller parts.",
        "unreadable": "The file could not be read. Please upload a text-based PDF or a valid DOCX file.",
    },
}

def file_protection_message(lang: str, key: str) -> str:
    return FILE_PROTECTION_MESSAGES.get(lang, FILE_PROTECTION_MESSAGES["uz"]).get(key, FILE_PROTECTION_MESSAGES["uz"]["unreadable"])

DOWNLOADS_DIR = "downloads"

TARIFFS = {
    "daily": {
        "price": "10 000 so'm",
        "duration": 24 * 3600,
        "names": {"uz": "Kunlik Cheksiz (24 soat)", "ru": "Суточный Безлимит (24 ч)", "en": "Daily Unlimited (24h)"},
    },
    "weekly": {
        "price": "35 000 so'm",
        "duration": 7 * 24 * 3600,
        "names": {"uz": "Haftalik Cheksiz (7 kun)", "ru": "Недельный Безлимит (7 дн)", "en": "Weekly Unlimited (7d)"},
    },
    "monthly": {
        "price": "65 000 so'm",
        "duration": 30 * 24 * 3600,
        "names": {"uz": "Oylik Cheksiz (30 kun)", "ru": "Месячный Безлимит (30 дн)", "en": "Monthly Unlimited (30d)"},
    },
    "teachers": {
        "price": "95 000 so'm",
        "duration": 30 * 24 * 3600,
        "names": {"uz": "O'qituvchilar Uchun (30 kun)", "ru": "Для Учителей (30 дн)", "en": "For Teachers (30d)"},
    },
}
FREE_QUIZ_LIMIT = 3
FREE_PUBLIC_LIMIT = 3
FREE_FLASHCARD_LIMIT = 3

def is_active_paid_status(status: str, premium_until: int) -> bool:
    return bool(status and "PRO" in status and premium_until and int(time.time()) <= premium_until)

def get_plan_key(status: str, plan_key: str = "") -> str:
    if plan_key in TARIFFS:
        return plan_key
    s = (status or "").lower()
    if "teacher" in s or "o'qit" in s or "учител" in s:
        return "teachers"
    if "weekly" in s or "haftalik" in s or "недель" in s:
        return "weekly"
    if "monthly" in s or "oylik" in s or "месяч" in s:
        return "monthly"
    if "daily" in s or "kunlik" in s or "суточ" in s:
        return "daily"
    return ""

def localized_tariff_name(plan_key: str, lang: str) -> str:
    lang = lang if lang in ("uz", "ru", "en") else "uz"
    return TARIFFS.get(plan_key, {}).get("names", {}).get(lang, plan_key)

MESSAGES = {
    "uz": {
        "welcome": (
            "👋 *Salom, {name}!*\n"
            "🎓 *Quiz AI* — AI yordamida bilimni tez va qulay tekshirish uchun zamonaviy App.\n\n"
            "✨ *Ilova imkoniyatlari:*\n"
            "🤖 AI yordamida matn, PDF yoki DOCX dan test yaratish\n"
            "📚 Testlarni Library bo'limida saqlash va ishlash\n"
            "🌐 Ommaviy testlardan foydalanish\n"
            "🧠 Flash Kartochkalar orqali takrorlash\n"
            "👥 Guruhlarga qo'shilish va guruh testlarida qatnashish\n"
            "👨‍🏫 O'qituvchilar uchun Professional vositalar\n"
            "📊 Natijalarni qulay ko'rish va tahlil qilish\n"
            "🔊 Ovoz va vibratsiya sozlamalari\n"
            "🌍 O'zbek, Русский va English tillari\n\n"
            "🆓 *Bepul:* har 30 kunda 3 ta AI test, 3 ta ommaviy test va 3 ta Flash Kartochka.\n"
            "👑 *Premium:* limitlarsiz foydalanish imkoniyati.\n\n"
            "📌 *Eslatma:* «Tariflarni faollashtirish» tugmasi bosilganda yangi oyna ochiladi. Shu oynadagi «Chekni yuborish» tugmasini bosing — bu sizni botga qaytaradi. Soʻng toʻlov chekini rasm yoki skrinshot shaklida yuboring.\n\n"
            "💬 *Bizning rasmiy guruhimiz:* [Quiz AI Rasmiy Chat](https://t.me/Quiz_AI_Chat)\n\n"
            "🚀 Boshlash uchun quyidagi tugmani bosing va Quiz Pilot Bot imkoniyatlaridan foydalaning!"
        ),
        "open_app": "Ilovani ochish 📱",
        "payment_prompt": (
            "🧾 Siz {tariff_name} ({tariff_price}) tarifini tanladingiz.\n\n"
            "Iltimos, plastik kartaga to'lov qilganingiz haqidagi To'lov Chekini "
            "(Rasm/Skrinshot ko'rinishida) shu yerga yuboring.\n"
            "Sizning buyurtma raqamingiz: {tx_id}"
        ),
        "receipt_received": "✅ Rahmat! To'lov chekingiz administratorga yuborildi. Tez orada tekshirilib, tarifingiz faollashtiriladi.",
        "receipt_error": "⚠️ To'lov chekingiz qabul qilindi, biroq adminga bildirishnoma yuborishda muammo bo'ldi. Admin paneldan tekshiriladi.",
        "payment_approved": "🎉 Tabriklaymiz! Sizning {tariff_name} tarifi uchun qilgan to'lovingiz tasdiqlandi. Ilovada PRO status faollashdi! 👑",
        "payment_rejected": "❌ Siz yuborgan to'lov cheki qabul qilinmadi yoki rad etildi. Agar xatolik bo'lgan deb o'ylasangiz, administratorga murojaat qiling.",
        "quiz_limit_reached": "🔒 Bepul limit tugadi. Test yaratishni davom ettirish uchun Premium tarifga o'ting. 👑",
        "public_limit_reached": "🔒 Bepul limit tugadi. Ommaviy testlarni davom ettirish uchun Premium tarifga o'ting. 👑",
        "flashcard_limit_reached": "🔒 Bepul limit tugadi. Flash Kartochka yaratishni davom ettirish uchun Premium tarifga o'ting. 👑",
        "support_prompt": "💬 Admin bilan bog'lanish. Savolingiz yoki muammoingizni shu yerga yozing. Xabaringiz administratorga yuboriladi.",
        "support_sent": "✅ Murojaatingiz administratorga yuborildi. Javob kelishini kuting.",
        "support_continue_btn": "💬 Admin bilan bog'lanish",
        "support_admin_title": "📩 Yangi murojaat",
        "support_reply_btn": "✉️ Javob berish",
        "support_reply_prompt": "✍️ Javobingizni yozing. U foydalanuvchiga yuboriladi.",
        "support_reply_sent": "✅ Javob foydalanuvchiga yuborildi.",
        "support_admin_reply_title": "Admin javobi",
        "support_config_error": "⚠️ Admin bilan bog'lanish hozircha sozlanmagan. Iltimos, keyinroq urinib ko'ring.",
        "quiz_ready": "📝 {title} darsligi bo'yicha jami {count} ta test savoli muvaffaqiyatli tayyorlandi!",
        "free_quiz_limit_notice": "🔒 *Bepul AI test limiti tugadi!*\n\nSizga ajratilgan 3 ta bepul AI testdan foydalanib bo'ldingiz. Yangi testlar yaratishda davom etish uchun 👑 *Premium tarif*ni tavsiya qilamiz.\n\n💎 Kunlik — 10 000 so'm\n💎 Haftalik — 35 000 so'm\n💎 Oylik — 65 000 so'm\n👨‍🏫 O'qituvchilar — 95 000 so'm\n\n🚀 Premium bo'limidan o'zingizga mos tarifni tanlashingiz mumkin.",
        "free_public_limit_notice": "🔒 *Bepul ommaviy test limiti tugadi!*\n\nSiz 30 kunlik bepul 3 ta ommaviy test limitidan foydalanib bo'ldingiz. Davom etish uchun 👑 *Premium tarif*ni tavsiya qilamiz.\n\n🚀 Premium bo'limidan tarifni tanlang.",
        "free_flashcard_limit_notice": "🔒 *Bepul Flash Kartochka limiti tugadi!*\n\nSiz 30 kunlik bepul 3 ta Flash Kartochka limitidan foydalanib bo'ldingiz. Davom etish uchun 👑 *Premium tarif*ni tavsiya qilamiz.\n\n🚀 Premium bo'limidan tarifni tanlang.",
        "paid_limit_notice": "⏳ *Premium limitingiz tugadi!*\n\nSizning {tariff_name} tarifingiz muddati yakunlandi. 👑 Premium imkoniyatlardan yana foydalanish uchun tarifni yangilashni tavsiya qilamiz.\n\n💎 Kunlik — 10 000 so'm\n💎 Haftalik — 35 000 so'm\n💎 Oylik — 65 000 so'm\n👨‍🏫 O'qituvchilar — 95 000 so'm\n\n🚀 Premium bo'limidan yangi tarifni tanlang.",
        "free_limits_restored_notice": "🎉 *Bepul limitlaringiz qaytdi!*\n\nSizning 30 kunlik bepul limit davringiz yangilandi. Endi yana 3 ta AI test, 3 ta ommaviy test va 3 ta Flash Kartochkadan bepul foydalanishingiz mumkin.\n\n🚀 Quiz Pilot Bot’dan foydalanishda davom eting!",
    },
    "ru": {
        "welcome": (
            "👋 *Привет, {name}!*\n"
            "🎓 *Quiz AI* — современный App для быстрой и удобной проверки знаний с помощью ИИ.\n\n"
            "✨ *Возможности приложения:*\n"
            "🤖 Создание тестов с помощью ИИ из текста, PDF или DOCX\n"
            "📚 Сохранение и прохождение тестов в разделе «Библиотека»\n"
            "🌐 Публичные тесты\n"
            "🧠 Флеш-карточки для повторения материала\n"
            "👥 Вступление в группы и участие в групповых тестах\n"
            "👨‍🏫 Профессиональные инструменты для учителей\n"
            "📊 Удобный просмотр и анализ результатов\n"
            "🔊 Настройки звука и вибрации\n"
            "🌍 Узбекский, русский и английский языки\n\n"
            "🆓 *Бесплатно:* 3 AI-теста, 3 публичных теста и 3 флеш-карточки каждые 30 дней.\n"
            "👑 *Premium:* использование без лимитов.\n\n"
            "📌 *Примечание:* При нажатии на кнопку «Активировать тарифы» откроется новое окно. Нажмите в этом окне кнопку «Отправить чек» — это вернёт вас в бот. Затем отправьте чек об оплате в виде фото или скриншота.\n\n"
            "💬 *Наша официальная группа:* [Quiz AI Официальный Чат](https://t.me/Quiz_AI_Chat)\n\n"
            "🚀 Нажмите кнопку ниже и начните пользоваться возможностями Quiz Pilot Bot!"
        ),
        "open_app": "Открыть приложение 📱",
        "payment_prompt": (
            "🧾 Вы выбрали тариф {tariff_name} ({tariff_price}).\n\n"
            "Пожалуйста, отправьте чек об оплате (в виде фото/скриншота) сюда.\n"
            "Ваш номер заказа: {tx_id}"
        ),
        "receipt_received": "✅ Спасибо! Ваш чек отправлен администратору. В ближайшее время он будет проверен, и ваш тариф активируется.",
        "receipt_error": "⚠️ Ваш чек принят, но возникла проблема с отправкой уведомления администратору. Он будет проверен через админ-панель.",
        "payment_approved": "🎉 Поздравляем! Ваш платеж по тарифу {tariff_name} подтвержден. В приложении активирован PRO статус! 👑",
        "payment_rejected": "❌ Ваш чек об оплате был отклонен. Если вы считаете, что произошла ошибка, свяжитесь с администратором.",
        "quiz_limit_reached": "🔒 Бесплатный лимит исчерпан. Чтобы продолжить создавать тесты, перейдите на Premium тариф. 👑",
        "public_limit_reached": "🔒 Бесплатный лимит исчерпан. Чтобы продолжить проходить публичные тесты, перейдите на Premium тариф. 👑",
        "flashcard_limit_reached": "🔒 Бесплатный лимит исчерпан. Чтобы продолжить создавать флеш-карточки, перейдите на Premium тариф. 👑",
        "support_prompt": "💬 Связаться с администратором. Напишите ваш вопрос или проблему здесь. Сообщение будет отправлено администратору.",
        "support_sent": "✅ Ваше обращение отправлено администратору. Ожидайте ответа.",
        "support_continue_btn": "💬 Связаться с администратором",
        "support_admin_title": "📩 Новое обращение",
        "support_reply_btn": "✉️ Ответить",
        "support_reply_prompt": "✍️ Напишите ответ. Он будет отправлен пользователю.",
        "support_reply_sent": "✅ Ответ отправлен пользователю.",
        "support_admin_reply_title": "Ответ администратора",
        "support_config_error": "⚠️ Связь с администратором пока не настроена. Пожалуйста, попробуйте позже.",
        "quiz_ready": "📝 Успешно подготовлено {count} тестовых вопросов по материалу {title}!",
        "free_quiz_limit_notice": "🔒 *Бесплатный лимит AI-тестов исчерпан!*\n\nВы использовали все 3 бесплатных AI-теста. Для продолжения рекомендуем 👑 *Premium тариф*.\n\n💎 Суточный — 10 000 so'm\n💎 Недельный — 35 000 so'm\n💎 Месячный — 65 000 so'm\n👨‍🏫 Для учителей — 95 000 so'm\n\n🚀 Выберите подходящий тариф в разделе Premium.",
        "free_public_limit_notice": "🔒 *Бесплатный лимит публичных тестов исчерпан!*\n\nВы использовали 3 бесплатных публичных теста за 30 дней. Для продолжения рекомендуем 👑 *Premium тариф*.\n\n🚀 Выберите тариф в разделе Premium.",
        "free_flashcard_limit_notice": "🔒 *Бесплатный лимит флеш-карточек исчерпан!*\n\nВы использовали 3 бесплатные флеш-карточки за 30 дней. Для продолжения рекомендуем 👑 *Premium тариф*.\n\n🚀 Выберите тариф в разделе Premium.",
        "paid_limit_notice": "⏳ *Срок Premium тарифа истёк!*\n\nВаш тариф {tariff_name} завершён. 👑 Рекомендуем продлить Premium, чтобы снова пользоваться всеми возможностями без ограничений.\n\n💎 Суточный — 10 000 so'm\n💎 Недельный — 35 000 so'm\n💎 Месячный — 65 000 so'm\n👨‍🏫 Для учителей — 95 000 so'm\n\n🚀 Выберите новый тариф в разделе Premium.",
        "free_limits_restored_notice": "🎉 *Ваши бесплатные лимиты восстановлены!*\n\nВаш 30-дневный бесплатный период обновлён. Теперь вы снова можете бесплатно использовать 3 AI-теста, 3 публичных теста и 3 флеш-карточки.\n\n🚀 Продолжайте пользоваться Quiz Pilot Bot!",
    },
    "en": {
        "welcome": (
            "👋 *Hello, {name}!*\n"
            "🎓 *Quiz AI* — a modern App for fast and convenient knowledge testing with AI.\n\n"
            "✨ *App features:*\n"
            "🤖 Create quizzes with AI from text, PDF or DOCX\n"
            "📚 Save and take quizzes in the Library\n"
            "🌐 Public quizzes\n"
            "🧠 Flashcards for revision\n"
            "👥 Join groups and participate in group quizzes\n"
            "👨‍🏫 Professional tools for teachers\n"
            "📊 Easy results viewing and analysis\n"
            "🔊 Sound and vibration settings\n"
            "🌍 Uzbek, Russian and English languages\n\n"
            "🆓 *Free:* 3 AI quizzes, 3 public quizzes and 3 flashcards every 30 days.\n"
            "👑 *Premium:* unlimited usage.\n\n"
            "📌 *Note:* Clicking the «Activate plans» button will open a new window. Press the «Send receipt» button in that window — this will return you to the bot. Then, send the payment receipt as a photo or screenshot.\n\n"
            "💬 *Our official group:* [Quiz AI Official Chat](https://t.me/Quiz_AI_Chat)\n\n"
            "🚀 Tap the button below and start using Quiz Pilot Bot!"
        ),
        "open_app": "Open App 📱",
        "payment_prompt": (
            "🧾 You have selected the {tariff_name} ({tariff_price}) plan.\n\n"
            "Please send your payment receipt (as a Photo/Screenshot) here.\n"
            "Your Order ID is: {tx_id}"
        ),
        "receipt_received": "✅ Thank you! Your payment receipt has been sent to the administrator. It will be verified shortly, and your plan will be activated.",
        "receipt_error": "⚠️ Your receipt was received, but there was an issue notifying the admin. It will be reviewed via the admin panel.",
        "payment_approved": "🎉 Congratulations! Your payment for the {tariff_name} plan has been confirmed. PRO status is now active! 👑",
        "payment_rejected": "❌ Your payment receipt was rejected. If you believe this is an error, please contact support.",
        "quiz_limit_reached": "🔒 Your free limit has been reached. Upgrade to Premium to continue creating quizzes. 👑",
        "public_limit_reached": "🔒 Your free limit has been reached. Upgrade to Premium to continue taking public quizzes. 👑",
        "flashcard_limit_reached": "🔒 Your free limit has been reached. Upgrade to Premium to continue creating flashcards. 👑",
        "support_prompt": "💬 Contact Admin. Write your question or problem here. Your message will be sent to the administrator.",
        "support_sent": "✅ Your message has been sent to the administrator. Please wait for a reply.",
        "support_continue_btn": "💬 Contact Admin",
        "support_admin_title": "📩 New support request",
        "support_reply_btn": "✉️ Reply",
        "support_reply_prompt": "✍️ Write your reply. It will be sent to the user.",
        "support_reply_sent": "✅ Reply sent to the user.",
        "support_admin_reply_title": "Admin reply",
        "support_config_error": "⚠️ Contact with the administrator is not configured yet. Please try again later.",
        "quiz_ready": "📝 A total of {count} quiz questions for {title} have been successfully generated!",
        "free_quiz_limit_notice": "🔒 *Your free AI quiz limit has ended!*\n\nYou have used all 3 free AI quizzes. To keep creating quizzes, we recommend 👑 *Premium*.\n\n💎 Daily — 10 000 so'm\n💎 Weekly — 35 000 so'm\n💎 Monthly — 65 000 so'm\n👨‍🏫 Teachers — 95 000 so'm\n\n🚀 Choose a plan in the Premium section.",
        "free_public_limit_notice": "🔒 *Your free public quiz limit has ended!*\n\nYou have used your 3 free public quizzes for the 30-day period. To continue, we recommend 👑 *Premium*.\n\n🚀 Choose a plan in the Premium section.",
        "free_flashcard_limit_notice": "🔒 *Your free flashcard limit has ended!*\n\nYou have used your 3 free flashcards for the 30-day period. To continue, we recommend 👑 *Premium*.\n\n🚀 Choose a plan in the Premium section.",
        "paid_limit_notice": "⏳ *Your Premium plan has expired!*\n\nYour {tariff_name} plan has ended. 👑 We recommend renewing Premium to continue using all features without limits.\n\n💎 Daily — 10 000 so'm\n💎 Weekly — 35 000 so'm\n💎 Monthly — 65 000 so'm\n👨‍🏫 Teachers — 95 000 so'm\n\n🚀 Choose a new plan in the Premium section.",
        "free_limits_restored_notice": "🎉 *Your free limits have been restored!*\n\nYour 30-day free period has been renewed. You can now use 3 AI quizzes, 3 public quizzes, and 3 flashcards for free again.\n\n🚀 Keep enjoying Quiz Pilot Bot!",
    }
}

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    db_url = DATABASE_URL
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url)

def get_user_lang(user_id: int) -> str:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT language FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0] in MESSAGES:
            return row[0]
    except Exception as e:
        logging.error(f"Foydalanuvchi tilini olishda xatolik: {e}")
    return "uz"

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""CREATE TABLE IF NOT EXISTS quizzes (
        id TEXT PRIMARY KEY,
        user_id BIGINT,
        title TEXT,
        total INTEGER,
        answered INTEGER,
        quiz_json TEXT,
        created_at BIGINT,
        last_score INTEGER DEFAULT -1,
        last_percent INTEGER DEFAULT -1,
        is_public INTEGER DEFAULT 0
    );""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        created_at BIGINT,
        language TEXT DEFAULT 'uz',
        status TEXT DEFAULT 'Oddiy foydalanuvchi',
        plan_key TEXT DEFAULT '',
        free_used INTEGER DEFAULT 0,
        public_free_used INTEGER DEFAULT 0,
        flashcard_free_used INTEGER DEFAULT 0,
        premium_until BIGINT DEFAULT 0,
        last_active BIGINT DEFAULT 0,
        last_quiz_free_notice_cycle BIGINT DEFAULT 0,
        last_public_free_notice_cycle BIGINT DEFAULT 0,
        last_flashcard_free_notice_cycle BIGINT DEFAULT 0,
        last_free_reset_notice_cycle BIGINT DEFAULT 0,
        paid_limit_notice_until BIGINT DEFAULT 0
    );""")

    cursor.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = 'users';
    """)
    columns = [col[0] for col in cursor.fetchall()]

    if "status" not in columns:
        try: cursor.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'Oddiy foydalanuvchi';")
        except Exception: pass
    if "free_used" not in columns:
        try: cursor.execute("ALTER TABLE users ADD COLUMN free_used INTEGER DEFAULT 0;")
        except Exception: pass
    if "premium_until" not in columns:
        try: cursor.execute("ALTER TABLE users ADD COLUMN premium_until BIGINT DEFAULT 0;")
        except Exception: pass
    if "public_free_used" not in columns:
        try: cursor.execute("ALTER TABLE users ADD COLUMN public_free_used INTEGER DEFAULT 0;")
        except Exception: pass
    if "flashcard_free_used" not in columns:
        try: cursor.execute("ALTER TABLE users ADD COLUMN flashcard_free_used INTEGER DEFAULT 0;")
        except Exception: pass
    if "plan_key" not in columns:
        try: cursor.execute("ALTER TABLE users ADD COLUMN plan_key TEXT DEFAULT '';")
        except Exception: pass
    if "last_active" not in columns:
        try: cursor.execute("ALTER TABLE users ADD COLUMN last_active BIGINT DEFAULT 0;")
        except Exception: pass

    for _col in (
        "last_quiz_free_notice_cycle",
        "last_public_free_notice_cycle",
        "last_flashcard_free_notice_cycle",
        "last_free_reset_notice_cycle",
        "paid_limit_notice_until",
    ):
        if _col not in columns:
            try: cursor.execute(f"ALTER TABLE users ADD COLUMN {_col} BIGINT DEFAULT 0;")
            except Exception: pass

    cursor.execute("UPDATE users SET free_used = 0 WHERE free_used IS NULL;")
    cursor.execute("UPDATE users SET public_free_used = 0 WHERE public_free_used IS NULL;")
    cursor.execute("UPDATE users SET flashcard_free_used = 0 WHERE flashcard_free_used IS NULL;")
    cursor.execute("UPDATE users SET status = 'Oddiy foydalanuvchi' WHERE status IS NULL;")
    cursor.execute("UPDATE users SET premium_until = 0 WHERE premium_until IS NULL;")
    cursor.execute("UPDATE users SET plan_key = '' WHERE plan_key IS NULL;")

    cursor.execute("""CREATE TABLE IF NOT EXISTS flashcards (
        id TEXT PRIMARY KEY,
        user_id BIGINT,
        front TEXT,
        back TEXT,
        created_at BIGINT
    );""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS payments (
        tx_id TEXT PRIMARY KEY,
        user_id BIGINT,
        tariff_name TEXT,
        tariff_price TEXT,
        status TEXT DEFAULT 'pending',
        created_at BIGINT
    );""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_sessions (
        id TEXT PRIMARY KEY,
        owner_id BIGINT,
        quiz_id TEXT,
        code TEXT UNIQUE,
        duration_minutes INTEGER DEFAULT 30,
        created_at BIGINT,
        expires_at BIGINT,
        active INTEGER DEFAULT 1,
        deleted INTEGER DEFAULT 0,
        source_type TEXT DEFAULT 'group_test',
        assignment_id TEXT DEFAULT '',
        group_id TEXT DEFAULT '',
        variant_code TEXT DEFAULT ''
    );""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_participants (
        id SERIAL PRIMARY KEY,
        session_id TEXT,
        user_id BIGINT,
        first_name TEXT DEFAULT '',
        username TEXT DEFAULT '',
        score INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        percent INTEGER DEFAULT 0,
        started_at BIGINT DEFAULT 0,
        finished_at BIGINT DEFAULT 0,
        UNIQUE(session_id, user_id)
    );""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_variants (
        id SERIAL PRIMARY KEY,
        quiz_id TEXT NOT NULL,
        variant_code TEXT NOT NULL,
        variant_json TEXT NOT NULL,
        created_at BIGINT NOT NULL,
        UNIQUE(quiz_id, variant_code)
    );""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_groups (
        id TEXT PRIMARY KEY,
        owner_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        join_code TEXT DEFAULT '',
        created_at BIGINT NOT NULL,
        active INTEGER DEFAULT 1
    );""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_group_members (
        id SERIAL PRIMARY KEY,
        group_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        first_name TEXT DEFAULT '',
        username TEXT DEFAULT '',
        joined_at BIGINT NOT NULL,
        UNIQUE(group_id, user_id)
    );""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS teacher_assignments (
        id TEXT PRIMARY KEY,
        owner_id BIGINT NOT NULL,
        group_id TEXT NOT NULL,
        quiz_id TEXT NOT NULL,
        variant_code TEXT DEFAULT '',
        title TEXT DEFAULT '',
        due_at BIGINT DEFAULT 0,
        duration_minutes INTEGER DEFAULT 30,
        created_at BIGINT NOT NULL,
        active INTEGER DEFAULT 1
    );""")

    teacher_migrations = {
        "teacher_sessions": {
            "group_id": "TEXT DEFAULT ''",
            "variant_code": "TEXT DEFAULT ''",
            "deleted": "INTEGER DEFAULT 0",
            "source_type": "TEXT DEFAULT 'group_test'",
            "assignment_id": "TEXT DEFAULT ''",
        },
        "teacher_participants": {
            "first_name": "TEXT DEFAULT ''",
            "username": "TEXT DEFAULT ''",
            "score": "INTEGER DEFAULT 0",
            "total": "INTEGER DEFAULT 0",
            "percent": "INTEGER DEFAULT 0",
            "started_at": "BIGINT DEFAULT 0",
            "finished_at": "BIGINT DEFAULT 0",
        },
        "teacher_variants": {
            "variant_code": "TEXT DEFAULT ''",
            "variant_json": "TEXT DEFAULT '{}'",
            "created_at": "BIGINT DEFAULT 0",
        },
        "teacher_groups": {
            "owner_id": "BIGINT DEFAULT 0",
            "name": "TEXT DEFAULT ''",
            "description": "TEXT DEFAULT ''",
            "join_code": "TEXT DEFAULT ''",
            "created_at": "BIGINT DEFAULT 0",
            "active": "INTEGER DEFAULT 1",
        },
        "teacher_group_members": {
            "group_id": "TEXT DEFAULT ''",
            "user_id": "BIGINT DEFAULT 0",
            "first_name": "TEXT DEFAULT ''",
            "username": "TEXT DEFAULT ''",
            "joined_at": "BIGINT DEFAULT 0",
        },
        "teacher_assignments": {
            "owner_id": "BIGINT DEFAULT 0",
            "group_id": "TEXT DEFAULT ''",
            "quiz_id": "TEXT DEFAULT ''",
            "variant_code": "TEXT DEFAULT ''",
            "title": "TEXT DEFAULT ''",
            "due_at": "BIGINT DEFAULT 0",
            "duration_minutes": "INTEGER DEFAULT 30",
            "created_at": "BIGINT DEFAULT 0",
            "active": "INTEGER DEFAULT 1",
        },
    }

    for table_name, cols in teacher_migrations.items():
        try:
            cursor.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = %s;
            """, (table_name,))
            existing = {row[0] for row in cursor.fetchall()}
            for column_name, column_def in cols.items():
                if column_name not in existing:
                    try:
                        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
                    except Exception as migration_error:
                        logging.warning("Teacher DB migration %s.%s: %s", table_name, column_name, migration_error)
        except Exception as migration_error:
            logging.warning("Teacher DB schema check %s: %s", table_name, migration_error)

    for table_name, updates in {
        "teacher_groups": [("description", "''"), ("active", "1")],
        "teacher_assignments": [("variant_code", "''"), ("title", "''"), ("due_at", "0"), ("duration_minutes", "30"), ("active", "1")],
        "teacher_sessions": [("group_id", "''"), ("variant_code", "''"), ("deleted", "0"), ("source_type", "'group_test'"), ("assignment_id", "''")],
    }.items():
        for col, value in updates:
            try:
                cursor.execute(f"UPDATE {table_name} SET {col}={value} WHERE {col} IS NULL")
            except Exception:
                pass

    try:
        cursor.execute("SELECT id FROM teacher_groups WHERE COALESCE(join_code,'')='' AND active=1")
        for row in cursor.fetchall():
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
            while True:
                cursor.execute("SELECT 1 FROM teacher_groups WHERE join_code=%s LIMIT 1", (code,))
                if not cursor.fetchone():
                    break
                code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
            cursor.execute("UPDATE teacher_groups SET join_code=%s WHERE id=%s", (code, row[0]))
    except Exception as e:
        logging.warning("Teacher group code migration: %s", e)

    conn.commit()
    conn.close()

init_db()

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="Jami 4 ta variant ro'yxati (Variant harflarisiz)")
    correct_index: int = Field(description="To'g'ri javob indeks (0 dan 3 gacha)")
    explanation: str = Field(description="Ushbu javob nega to'g'riligini tushuntiruvchi qisqa izoh")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")

class ProgressUpdateRequest(BaseModel):
    quiz_id: str
    user_id: int
    correct_count: int
    percent: int

class FlashcardCreateRequest(BaseModel):
    user_id: int
    front: str
    back: str

class PaymentIntentRequest(BaseModel):
    action: str
    user_id: int
    tariff_name: Optional[str] = None
    tariff_price: Optional[str] = None
    tariff_key: Optional[str] = None

def _send_limit_message(user_id: int, message_key: str, **kwargs):
    try:
        lang = get_user_lang(user_id)
        text = MESSAGES.get(lang, MESSAGES["uz"])[message_key].format(**kwargs)
        bot.send_message(user_id, text, parse_mode="Markdown")
        return True
    except Exception as e:
        logging.error(f"Limit Telegram xabari yuborilmadi ({user_id}, {message_key}): {e}")
        return False

def notify_free_limit_reached(user_id: int, kind: str):
    columns = {
        "quiz": ("free_used", "last_quiz_free_notice_cycle", FREE_QUIZ_LIMIT, "free_quiz_limit_notice"),
        "public": ("public_free_used", "last_public_free_notice_cycle", FREE_PUBLIC_LIMIT, "free_public_limit_notice"),
        "flashcard": ("flashcard_free_used", "last_flashcard_free_notice_cycle", FREE_FLASHCARD_LIMIT, "free_flashcard_limit_notice"),
    }
    if kind not in columns:
        return
    used_col, notice_col, limit, message_key = columns[kind]
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT {used_col}, created_at, {notice_col}, status, premium_until FROM users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row or is_active_paid_status(row["status"] or "", row["premium_until"] or 0):
            conn.close()
            return
        used = row[used_col] or 0
        cycle = row["created_at"] or 0
        already = row[notice_col] or 0
        if used < limit or not cycle or already == cycle:
            conn.close()
            return
        cur.execute(f"UPDATE users SET {notice_col} = %s WHERE user_id = %s AND {notice_col} <> %s", (cycle, user_id, cycle))
        changed = cur.rowcount > 0
        conn.commit()
        conn.close()
        if changed:
            _send_limit_message(user_id, message_key)
    except Exception as e:
        logging.error(f"Bepul limit notification xatosi ({user_id}, {kind}): {e}")

def process_expired_free_limits():
    now = int(time.time())
    thirty_days = 30 * 24 * 3600
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT user_id, created_at, free_used, public_free_used, flashcard_free_used, "
            "status, premium_until, last_free_reset_notice_cycle "
            "FROM users WHERE created_at > 0 AND created_at <= %s",
            (now - thirty_days,),
        )
        rows = cur.fetchall()
        for row in rows:
            user_id = row["user_id"]
            old_cycle = row["created_at"] or 0
            is_paid = is_active_paid_status(row["status"] or "", row["premium_until"] or 0)
            if is_paid:
                continue

            already_notified = row["last_free_reset_notice_cycle"] or 0
            had_exhausted_limit = (
                (row["free_used"] or 0) >= FREE_QUIZ_LIMIT
                or (row["public_free_used"] or 0) >= FREE_PUBLIC_LIMIT
                or (row["flashcard_free_used"] or 0) >= FREE_FLASHCARD_LIMIT
            )

            cur.execute(
                "UPDATE users SET free_used=0, public_free_used=0, flashcard_free_used=0, "
                "created_at=%s, last_free_reset_notice_cycle=%s "
                "WHERE user_id=%s AND created_at=%s",
                (now, old_cycle if had_exhausted_limit else already_notified, user_id, old_cycle),
            )
            if cur.rowcount == 1 and had_exhausted_limit and already_notified != old_cycle:
                try:
                    lang = get_user_lang(user_id)
                    text = MESSAGES.get(lang, MESSAGES["uz"])["free_limits_restored_notice"]
                    bot.send_message(user_id, text, parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"Bepul limit qaytgani haqida xabar yuborilmadi ({user_id}): {e}")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Bepul limit reset worker xatosi: {e}")

def process_expired_paid_limits():
    now = int(time.time())
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT user_id, status, plan_key, premium_until, paid_limit_notice_until "
            "FROM users WHERE premium_until > 0 AND premium_until <= %s AND status LIKE '%%PRO%%'",
            (now,),
        )
        rows = cur.fetchall()
        for row in rows:
            user_id = row["user_id"]
            expiry = row["premium_until"] or 0
            if (row["paid_limit_notice_until"] or 0) == expiry:
                continue
            plan_key = row["plan_key"] or get_plan_key(row["status"] or "")
            tariff_name = localized_tariff_name(plan_key, get_user_lang(user_id)) if plan_key else "Premium"
            cur.execute(
                "UPDATE users SET status='Oddiy foydalanuvchi', plan_key='', premium_until=0, paid_limit_notice_until=%s "
                "WHERE user_id=%s AND premium_until=%s",
                (expiry, user_id, expiry),
            )
            if cur.rowcount == 1:
                try:
                    lang = get_user_lang(user_id)
                    text = MESSAGES.get(lang, MESSAGES["uz"])["paid_limit_notice"].format(tariff_name=tariff_name)
                    bot.send_message(user_id, text, parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"Premium tugash xabari yuborilmadi ({user_id}): {e}")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Premium limit worker xatosi: {e}")

def limit_notification_worker():
    while True:
        try:
            process_expired_paid_limits()
            process_expired_free_limits()
        except Exception as e:
            logging.error(f"Limit notification worker xatosi: {e}")
        time.sleep(30)

def add_user_to_db(user_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (user_id, created_at, language, status, plan_key, free_used, public_free_used, flashcard_free_used, premium_until) "
            "VALUES (%s, %s, 'uz', 'Oddiy foydalanuvchi', '', 0, 0, 0, %s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id, int(time.time()), 0),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Foydalanuvchi qo'shishda xato: {e}")

def get_users_count():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        active_since = int(time.time()) - 2 * 60
        cursor.execute(
            "SELECT COUNT(DISTINCT user_id) FROM users WHERE last_active >= %s",
            (active_since,),
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logging.error(f"Faol foydalanuvchilar sonini olishda xato: {e}")
        return 0

def update_user_activity(user_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET last_active = %s WHERE user_id = %s",
            (int(time.time()), user_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Faol foydalanuvchi vaqtini yangilashda xato: {e}")

def trigger_payment_flow(user_id, tariff_name=None, tariff_price=None, tariff_key=None):
    try:
        user_lang = get_user_lang(user_id)
        if tariff_key not in TARIFFS:
            low = (tariff_name or "").lower()
            if "o'qit" in low or "учител" in low or "teacher" in low:
                tariff_key = "teachers"
            elif "haft" in low or "недель" in low or "weekly" in low or "7" in low:
                tariff_key = "weekly"
            elif "oy" in low or "месяч" in low or "monthly" in low or "30" in low:
                tariff_key = "monthly"
            else:
                tariff_key = "daily"
        tariff_name = localized_tariff_name(tariff_key, user_lang)
        tariff_price = TARIFFS[tariff_key]["price"]
        tx_id = f"TX{uuid.uuid4().hex[:6].upper()}"

        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("UPDATE payments SET status = 'cancelled' WHERE user_id = %s AND status = 'pending'", (user_id,))
        cursor.execute(
            "INSERT INTO payments (tx_id, user_id, tariff_name, tariff_price, status, created_at) VALUES (%s, %s, %s, %s, 'pending', %s)",
            (tx_id, user_id, tariff_name, tariff_price, int(time.time())),
        )
        conn.commit()
        conn.close()

        msg_text = MESSAGES[user_lang]["payment_prompt"].format(
            tariff_name=tariff_name,
            tariff_price=tariff_price,
            tx_id=tx_id
        )

        bot.send_message(user_id, msg_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"To'lov jarayonini ishga tushirishda xato: {e}")

@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_id = message.from_user.id
    add_user_to_db(user_id)
    user_lang = get_user_lang(user_id)

    welcome_text = MESSAGES[user_lang]["welcome"].format(name=message.from_user.first_name)

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_start = telebot.types.KeyboardButton(text="/start")

    markup.row(btn_start)
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(content_types=["web_app_data"])
def handle_webapp_data(message):
    try:
        logging.info(f"WebApp dan kelgan xom ma'lumot: {message.web_app_data.data}")
        data = json.loads(message.web_app_data.data)

        if data.get("action") == "contact_admin":
            user_id = int(data.get("user_id") or message.from_user.id)
            user_lang = get_user_lang(user_id)
            if not ADMIN_ID:
                bot.send_message(user_id, MESSAGES[user_lang]["support_config_error"])
                return
            support_waiting_users.add(user_id)
            bot.send_message(user_id, MESSAGES[user_lang]["support_prompt"])
            return

        if data.get("action") == "payment_intent":
            user_id = data.get("user_id")
            tariff_key = data.get("tariff_key")
            tariff_name = data.get("tariff_name")
            tariff_price = data.get("tariff_price")
            trigger_payment_flow(user_id, tariff_name, tariff_price, tariff_key)
    except Exception as e:
        logging.error(f"WebApp ma'lumotlarini o'qishda jiddiy xato: {e}")

def support_continue_markup(lang: str):
    lang = lang if lang in MESSAGES else "uz"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        MESSAGES[lang]["support_continue_btn"],
        callback_data="support_continue"
    ))
    return markup

@bot.message_handler(content_types=["text"], func=lambda message: message.from_user.id in support_waiting_users or message.from_user.id in support_reply_targets)
def handle_support_text(message):
    user_id = message.from_user.id
    if ADMIN_ID and user_id == ADMIN_ID and user_id in support_reply_targets:
        target_user_id = support_reply_targets.pop(user_id)
        try:
            target_lang = get_user_lang(target_user_id)
            target_messages = MESSAGES.get(target_lang, MESSAGES["uz"])
            bot.send_message(
                target_user_id,
                f"💬 {target_messages.get('support_admin_reply_title', 'Admin javobi')}:\n\n{message.text}",
                reply_markup=support_continue_markup(target_lang)
            )
            bot.send_message(user_id, MESSAGES["uz"]["support_reply_sent"])
        except Exception as e:
            logging.error(f"Admin javobini yuborishda xato: {e}")
            bot.send_message(user_id, "❌ Javobni yuborishda xatolik yuz berdi.")
        return
    if user_id in support_waiting_users:
        support_waiting_users.discard(user_id)
        user_lang = get_user_lang(user_id)
        username = f"@{message.from_user.username}" if message.from_user.username else "Mavjud emas"
        first_name = message.from_user.first_name or "Mavjud emas"
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(MESSAGES["uz"]["support_reply_btn"], callback_data=f"support_reply:{user_id}"))
        text = (f"{MESSAGES['uz']['support_admin_title']}\n\n"
                f"👤 {first_name}\n🔗 Username: {username}\n🆔 Telegram ID: {user_id}\n🌐 Til: {user_lang.upper()}\n\n💬 {message.text}")
        try:
            if not ADMIN_ID:
                bot.send_message(user_id, MESSAGES[user_lang]["support_config_error"])
                return
            bot.send_message(ADMIN_ID, text, reply_markup=markup)
            bot.send_message(
                user_id,
                MESSAGES[user_lang]["support_sent"],
                reply_markup=support_continue_markup(user_lang)
            )
        except Exception as e:
            logging.error(f"Admin murojaatini yuborishda xato: {e}")
            bot.send_message(user_id, "❌ Murojaatni yuborishda xatolik yuz berdi.")
        return

@bot.callback_query_handler(func=lambda call: call.data == "support_continue")
def handle_support_continue_callback(call):
    if ADMIN_ID and call.from_user.id == ADMIN_ID:
        bot.answer_callback_query(call.id, "Bu tugma foydalanuvchi uchun.", show_alert=True)
        return
    try:
        user_id = call.from_user.id
        add_user_to_db(user_id)
        user_lang = get_user_lang(user_id)
        if not ADMIN_ID:
            bot.answer_callback_query(call.id)
            bot.send_message(user_id, MESSAGES[user_lang]["support_config_error"])
            return
        support_waiting_users.add(user_id)
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, MESSAGES[user_lang]["support_prompt"])
        logging.info(f"Admin support davom ettirildi: user_id={user_id}, admin_id={ADMIN_ID}")
    except Exception as e:
        logging.error(f"Admin support davom ettirish callback xatosi: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("support_reply:"))
def handle_support_reply_callback(call):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Siz administrator emassiz!", show_alert=True)
        return
    try:
        target_user_id = int(call.data.split(":", 1)[1])
        support_reply_targets[call.from_user.id] = target_user_id
        bot.answer_callback_query(call.id)
        bot.send_message(call.from_user.id, MESSAGES["uz"]["support_reply_prompt"])
    except Exception as e:
        logging.error(f"Admin reply callback xatosi: {e}")
        bot.answer_callback_query(call.id, "Xatolik yuz berdi.", show_alert=True)

@bot.message_handler(content_types=["photo"])
def handle_receipt_photo(message):
    user_id = message.from_user.id
    user_lang = get_user_lang(user_id)

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        "SELECT tx_id, tariff_name, tariff_price FROM payments WHERE user_id = %s AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    pending_pay = cursor.fetchone()
    conn.close()

    if not pending_pay:
        return

    tx_id = pending_pay["tx_id"]
    tariff_name = pending_pay["tariff_name"]
    tariff_price = pending_pay["tariff_price"]

    username = f"@{message.from_user.username}" if message.from_user.username else "Mavjud emas"
    first_name = message.from_user.first_name
    file_id = message.photo[-1].file_id

    admin_markup = telebot.types.InlineKeyboardMarkup()
    btn_approve = telebot.types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"p_app_{tx_id}_{user_id}")
    btn_reject = telebot.types.InlineKeyboardButton("❌ Rad etish", callback_data=f"p_rej_{tx_id}_{user_id}")
    admin_markup.row(btn_approve, btn_reject)

    plan_key = get_plan_key(tariff_name)
    admin_tariff_name = localized_tariff_name(plan_key, "uz") if plan_key else tariff_name

    admin_text = (
        f"💰 YANGI TO'LOV SO'ROVI!\n\n"
        f"👤 Foydalanuvchi: {first_name} ({username})\n"
        f"🆔 Telegram ID: {user_id}\n"
        f"🌐 Til kodi: {user_lang.upper()}\n"
        f"📦 Tanlangan Tarif: {admin_tariff_name}\n"
        f"💵 To'lov Summasi: {tariff_price}\n"
        f"🧩 Tranzaksiya ID: {tx_id}\n\n"
        f"Chek to'g'riligini tekshiring va pastdagi tugmalardan birini bosing."
    )

    target_admin = ADMIN_ID if ADMIN_ID else user_id

    try:
        bot.send_photo(
            target_admin,
            file_id,
            caption=admin_text,
            parse_mode="Markdown",
            reply_markup=admin_markup,
        )
        bot.send_message(message.chat.id, MESSAGES[user_lang]["receipt_received"])
    except Exception as e:
        logging.error(f"Admin ga rasm yuborishda xatolik yuz berdi: {e}")
        bot.send_message(message.chat.id, MESSAGES[user_lang]["receipt_error"])

@bot.callback_query_handler(func=lambda call: call.data.startswith("p_"))
def handle_admin_decision(call):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Siz administrator emassiz!", show_alert=True)
        return

    parts = call.data.split("_")
    action = parts[1]
    tx_id = parts[2]
    user_id = int(parts[3])
    user_lang = get_user_lang(user_id)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status, tariff_name FROM payments WHERE tx_id = %s", (tx_id,))
    pay_row = cursor.fetchone()

    if not pay_row or pay_row[0] != "pending":
        bot.answer_callback_query(call.id, "Bu so'rov allaqachon ko'rib chiqilgan!", show_alert=True)
        conn.close()
        return

    tariff_name = pay_row[1]

    if action == "app":
        current_time = int(time.time())
        plan_key = get_plan_key(tariff_name)
        if not plan_key:
            low = tariff_name.lower()
            if "teacher" in low or "o'qit" in low or "учител" in low:
                plan_key = "teachers"
            elif "weekly" in low or "haft" in low or "недель" in low or "7" in low:
                plan_key = "weekly"
            elif "monthly" in low or "oy" in low or "месяч" in low or "30" in low:
                plan_key = "monthly"
            else:
                plan_key = "daily"
        duration = TARIFFS[plan_key]["duration"]
        premium_until_timestamp = current_time + duration
        cursor.execute("UPDATE payments SET status = 'approved' WHERE tx_id = %s", (tx_id,))
        display_name = localized_tariff_name(plan_key, user_lang)
        cursor.execute(
            "UPDATE users SET status = %s, plan_key = %s, premium_until = %s WHERE user_id = %s",
            (f"PRO ({display_name})", plan_key, premium_until_timestamp, user_id),
        )
        conn.commit()

        bot.answer_callback_query(call.id, "To'lov tasdiqlandi!")
        try:
            bot.edit_message_caption(
                f"✅ {call.message.caption}\n\n🟢 TASDIQLANDI!",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass
        try:
            succ_msg = MESSAGES[user_lang]["payment_approved"].format(tariff_name=display_name)
            bot.send_message(user_id, succ_msg)
        except Exception:
            pass

    elif action == "rej":
        cursor.execute("UPDATE payments SET status = 'rejected' WHERE tx_id = %s", (tx_id,))
        conn.commit()
        bot.answer_callback_query(call.id, "To'lov rad etildi.")
        try:
            bot.edit_message_caption(
                f"❌ {call.message.caption}\n\n🔴 RAD ETILDI!",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass
        try:
            rej_msg = MESSAGES[user_lang]["payment_rejected"]
            bot.send_message(user_id, rej_msg)
        except Exception:
            pass

    conn.close()

# --- FASTAPI ENDPOINTS ---
app = FastAPI()

@app.exception_handler(Exception)
async def teacher_db_error_handler(request: Request, exc: Exception):
    logging.exception("Database/Server Error: %s", exc)
    err_str = str(exc).lower()
    msg = "__TEACHER_DB_BUSY__" if any(x in err_str for x in ("locked", "busy", "readonly", "deadlock")) else "__TEACHER_SERVER_ERROR__"
    return JSONResponse(status_code=500, content={"status": "error", "detail": msg})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    response = templates.TemplateResponse("index.html", {"request": request})
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@app.post("/api/payment-intent")
def api_payment_intent(req: PaymentIntentRequest):
    if req.action == "payment_intent":
        if req.tariff_key not in TARIFFS:
            raise HTTPException(status_code=400, detail="Noto'g'ri tarif")
        threading.Thread(
            target=trigger_payment_flow,
            args=(req.user_id, req.tariff_name, req.tariff_price, req.tariff_key),
            daemon=True,
        ).start()
        return {
            "status": "ok",
            "message": "To'lov so'rovi muvaffaqiyatli qabul qilindi",
        }
    raise HTTPException(status_code=400, detail="Noto'g'ri amal")

@app.get("/api/premium-status")
def get_premium_status(user_id: int):
    add_user_to_db(user_id)
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        "SELECT status, plan_key, free_used, public_free_used, flashcard_free_used, premium_until, created_at FROM users WHERE user_id = %s",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {
            "status": "ok",
            "user_status": "Oddiy foydalanuvchi",
            "free_used": 0,
            "public_free_used": 0,
            "flashcard_free_used": 0,
            "public_remaining": FREE_PUBLIC_LIMIT,
            "flashcard_remaining": FREE_FLASHCARD_LIMIT,
            "quiz_remaining": FREE_QUIZ_LIMIT,
            "plan_key": "",
            "is_paid": False,
            "is_teacher": False,
        }

    user_status = row["status"] or "Oddiy foydalanuvchi"
    plan_key = row["plan_key"] or get_plan_key(user_status)
    premium_until = row["premium_until"] or 0
    free_used = row["free_used"] if row["free_used"] is not None else 0
    public_free_used = row["public_free_used"] if row["public_free_used"] is not None else 0
    flashcard_free_used = row["flashcard_free_used"] if row["flashcard_free_used"] is not None else 0
    created_at = row["created_at"] or int(time.time())
    now = int(time.time())

    if is_active_paid_status(user_status, premium_until):
        pass
    elif "PRO" in user_status and premium_until > 0 and now > premium_until:
        cursor.execute("UPDATE users SET status = 'Oddiy foydalanuvchi', plan_key = '', premium_until = 0 WHERE user_id = %s", (user_id,))
        conn.commit()
        user_status, plan_key, premium_until = "Oddiy foydalanuvchi", "", 0

    if now - created_at >= 30 * 24 * 3600 and not is_active_paid_status(user_status, premium_until):
        cursor.execute(
            "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = %s WHERE user_id = %s",
            (now, user_id),
        )
        conn.commit()
        free_used = 0
        public_free_used = 0
        flashcard_free_used = 0

    is_paid = is_active_paid_status(user_status, premium_until)
    is_teacher = is_paid and plan_key == "teachers"
    lang = get_user_lang(user_id)
    display_status = user_status
    if is_paid:
        display_status = f"PRO ✨ ({localized_tariff_name(plan_key, lang)})"
        uzbek_time = time.gmtime(premium_until + 5 * 3600)
        readable_date = time.strftime("%d.%m.%Y %H:%M", uzbek_time)
        if lang == "ru": display_status += f" (До: {readable_date})"
        elif lang == "en": display_status += f" (Until: {readable_date})"
        else: display_status += f" (Gacha: {readable_date})"
    conn.close()
    return {
        "status": "ok",
        "user_status": display_status,
        "free_used": free_used,
        "public_free_used": public_free_used,
        "flashcard_free_used": flashcard_free_used,
        "public_remaining": max(0, FREE_PUBLIC_LIMIT - public_free_used),
        "flashcard_remaining": max(0, FREE_FLASHCARD_LIMIT - flashcard_free_used),
        "quiz_remaining": max(0, FREE_QUIZ_LIMIT - free_used),
        "plan_key": plan_key,
        "is_paid": is_paid,
        "is_teacher": is_teacher,
    }

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

@app.post("/api/create-quiz-web")
async def create_quiz_web(
    user_id: int = Form(...),
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    quiz_title: Optional[str] = Form(None),
):
    add_user_to_db(user_id)
    user_lang = get_user_lang(user_id)

    if file:
        file_bytes = bytearray()
        chunk_size = 1024 * 1024
        while chunk := await file.read(chunk_size):
            file_bytes.extend(chunk)
            if len(file_bytes) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413, 
                    detail="Fayl hajmi 10 MB limitidan oshib ketdi!"
                )
        await file.seek(0)

    conn_check = get_db_connection()
    cursor_check = conn_check.cursor(cursor_factory=RealDictCursor)
    cursor_check.execute(
        "SELECT status, premium_until, free_used, created_at FROM users WHERE user_id = %s",
        (user_id,),
    )
    user_row = cursor_check.fetchone()

    if user_row:
        current_status = user_row["status"] or "Oddiy foydalanuvchi"
        premium_until = user_row["premium_until"] or 0
        free_used = user_row["free_used"] if user_row["free_used"] is not None else 0
        created_at = user_row["created_at"] or int(time.time())
        current_now = int(time.time())

        thirty_days_sec = 30 * 24 * 3600
        if current_now - created_at >= thirty_days_sec:
            cursor_check.execute(
                "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = %s WHERE user_id = %s",
                (current_now, user_id),
            )
            free_used = 0

        if (
            "PRO" in current_status
            and premium_until > 0
            and current_now > premium_until
        ):
            cursor_check.execute(
                "UPDATE users SET status = 'Oddiy foydalanuvchi', premium_until = 0 WHERE user_id = %s",
                (user_id,),
            )

        if "PRO" not in current_status:
            cursor_check.execute(
                "UPDATE users "
                "SET free_used = COALESCE(free_used, 0) + 1 "
                "WHERE user_id = %s AND COALESCE(free_used, 0) < %s",
                (user_id, FREE_QUIZ_LIMIT),
            )
            if cursor_check.rowcount != 1:
                conn_check.close()
                return {
                    "status": "error",
                    "error_code": "free_limit",
                    "message": MESSAGES[user_lang]["quiz_limit_reached"],
                }
            conn_check.commit()
            free_slot_reserved = True
        else:
            free_slot_reserved = False
    else:
        free_slot_reserved = False

    conn_check.close()

    raw_text = ""
    auto_title = "Matnli Test"

    if file and file.filename and len(file.filename.strip()) > 0:
        original_name = Path(file.filename).name
        extension = Path(original_name).suffix.lower()
        if extension not in ALLOWED_UPLOAD_EXTENSIONS:
            if free_slot_reserved:
                conn_restore = get_db_connection()
                cur_restore = conn_restore.cursor()
                cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s", (user_id,))
                conn_restore.commit()
                conn_restore.close()
            return {"status": "error", "message": file_protection_message(user_lang, "unsupported")}

        try:
            contents = await file.read()
            if len(contents) > MAX_UPLOAD_FILE_BYTES:
                if free_slot_reserved:
                    conn_restore = get_db_connection()
                    cur_restore = conn_restore.cursor()
                    cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s", (user_id,))
                    conn_restore.commit()
                    conn_restore.close()
                return {"status": "error", "message": file_protection_message(user_lang, "too_large")}
                    
            if contents:
                os.makedirs(DOWNLOADS_DIR, exist_ok=True)
                file_path = os.path.join(DOWNLOADS_DIR, f"upload_{uuid.uuid4().hex}{extension}")
                with open(file_path, "wb") as f:
                    f.write(contents)

                if extension == ".pdf":
                    reader = PdfReader(file_path)
                    page_count = len(reader.pages)
                    if page_count > MAX_PDF_PAGES:
                        try: os.remove(file_path)
                        except Exception: pass
                        if free_slot_reserved:
                            conn_restore = get_db_connection()
                            cur_restore = conn_restore.cursor()
                            cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s", (user_id,))
                            conn_restore.commit()
                            conn_restore.close()
                        return {"status": "error", "message": file_protection_message(user_lang, "too_many_pages")}
                    text_parts = []
                    text_len = 0
                    for page in reader.pages:
                        page_text = page.extract_text() or ""
                        if page_text:
                            text_len += len(page_text)
                            if text_len > MAX_EXTRACTED_TEXT_CHARS:
                                try: os.remove(file_path)
                                except Exception: pass
                                if free_slot_reserved:
                                    conn_restore = get_db_connection()
                                    cur_restore = conn_restore.cursor()
                                    cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s", (user_id,))
                                    conn_restore.commit()
                                    conn_restore.close()
                                return {"status": "error", "message": file_protection_message(user_lang, "too_much_text")}
                            text_parts.append(page_text)
                    raw_text = "\n".join(text_parts)
                    auto_title = Path(original_name).stem
                elif extension == ".docx":
                    doc = docx.Document(file_path)
                    text_parts = []
                    text_len = 0
                    for paragraph in doc.paragraphs:
                        part = paragraph.text or ""
                        text_len += len(part)
                        if text_len > MAX_EXTRACTED_TEXT_CHARS:
                            try: os.remove(file_path)
                            except Exception: pass
                            if free_slot_reserved:
                                conn_restore = get_db_connection()
                                cur_restore = conn_restore.cursor()
                                cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s", (user_id,))
                                conn_restore.commit()
                                conn_restore.close()
                            return {"status": "error", "message": file_protection_message(user_lang, "too_much_text")}
                        text_parts.append(part)
                    raw_text = "\n".join(text_parts)
                    auto_title = Path(original_name).stem
        except Exception as e:
            logging.error(f"Professional file protection / parsing error: {e}")
            if free_slot_reserved:
                conn_restore = get_db_connection()
                cur_restore = conn_restore.cursor()
                cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s", (user_id,))
                conn_restore.commit()
                conn_restore.close()
            return {"status": "error", "message": file_protection_message(user_lang, "unreadable")}

    if not raw_text.strip() and text:
        raw_text = text
        auto_text_clean = text.replace("\n", " ").strip()
        auto_title = (
            auto_text_clean[:18] + "..."
            if len(auto_text_clean) > 18
            else auto_text_clean
        )

    if not raw_text.strip():
        if free_slot_reserved:
            try:
                conn_restore = get_db_connection()
                cur_restore = conn_restore.cursor()
                cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s", (user_id,))
                conn_restore.commit()
                conn_restore.close()
            except Exception:
                pass
        return {"status": "error", "message": file_protection_message(user_lang, "unreadable")}

    quiz_json_raw = await asyncio.to_thread(generate_quiz_from_gemini, raw_text)
    if not quiz_json_raw:
        if free_slot_reserved:
            try:
                conn_restore = get_db_connection()
                cur_restore = conn_restore.cursor()
                cur_restore.execute("UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s", (user_id,))
                conn_restore.commit()
                conn_restore.close()
            except Exception as e:
                logging.error(f"Bepul limitni qaytarishda xato: {e}")
        return {"status": "error", "message": "AI test generatsiya qila olmadi."}

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        items = randomize_quiz_answer_positions(items)
        quiz_data["quizzes"] = items
        if not items:
            return {
                "status": "error",
                "message": "AI savollar ro'yxatini bo'sh qaytardi.",
            }

        quiz_id = f"q_{uuid.uuid4().hex}"
        final_title = (
            quiz_title.strip() if (quiz_title and quiz_title.strip()) else auto_title
        )

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO quizzes (id, user_id, title, total, answered, quiz_json, created_at, last_score, last_percent, is_public)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0)""",
            (
                quiz_id,
                user_id,
                final_title[:30],
                len(items),
                0,
                json.dumps(quiz_data),
                int(time.time()),
                -1,
                -1,
            )
        )
        conn.commit()
        conn.close()

        if free_slot_reserved:
            notify_free_limit_reached(user_id, "quiz")

        try:
            q_ready_msg = MESSAGES[user_lang]["quiz_ready"].format(
                title=final_title[:30],
                count=len(items)
            )
            bot.send_message(user_id, q_ready_msg)
        except Exception as e:
            logging.error(f"Telegram xabari yuborilmadi: {e}")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def randomize_quiz_answer_positions(items):
    if not isinstance(items, list) or not items:
        return items

    positions = [i % 4 for i in range(len(items))]
    secrets.SystemRandom().shuffle(positions)

    for item, target_index in zip(items, positions):
        if not isinstance(item, dict):
            continue
        options = list(item.get("options") or [])
        if len(options) < 2:
            continue
        options = options[:4]
        try:
            correct_index = int(item.get("correct_index", 0))
        except (TypeError, ValueError):
            correct_index = 0
        if correct_index < 0 or correct_index >= len(options):
            correct_index = 0

        correct_pair = (correct_index, options[correct_index])
        other_pairs = [(i, value) for i, value in enumerate(options) if i != correct_index]
        secrets.SystemRandom().shuffle(other_pairs)
        new_pairs = other_pairs[:]
        insert_at = min(target_index, len(new_pairs))
        new_pairs.insert(insert_at, correct_pair)

        item["options"] = [value for _, value in new_pairs]
        item["correct_index"] = next(i for i, pair in enumerate(new_pairs) if pair[0] == correct_index)

    return items

def generate_quiz_from_gemini(extracted_text):
    global current_key_index

    if not GOOGLE_API_KEYS:
        logging.error("GOOGLE_API_KEYS topilmadi yoki bo'sh!")
        return None

    system_instruction = """You are an advanced AI quiz generator.
CRITICAL RULES:
1. LANGUAGE RULE: Detect the language of the provided text. You MUST generate the questions, choices, and explanations in the EXACT SAME language as the input text.
2. QUESTION COUNT RULE: Look at the input text. If the user provided a strict list of questions, you MUST ONLY extract and format THOSE EXACT questions into the quiz structure. If it's a huge continuous textbook, you can generate up to 40-50 questions maximum."""

    if not ai_queue_slots.acquire(timeout=AI_TOTAL_TIMEOUT):
        logging.error("AI Queue kutish vaqti tugadi")
        return None

    try:
        total_keys = len(GOOGLE_API_KEYS)
        with key_lock:
            start_index = current_key_index
            current_key_index = (current_key_index + 1) % total_keys

        deadline = time.monotonic() + AI_TOTAL_TIMEOUT
        last_error = None

        for retry_round in range(AI_RETRY_PER_KEY):
            for offset in range(total_keys):
                if time.monotonic() >= deadline:
                    logging.error("AI umumiy timeout (%ss) tugadi", AI_TOTAL_TIMEOUT)
                    return None

                key_idx = (start_index + offset) % total_keys
                api_key = GOOGLE_API_KEYS[key_idx].strip()
                if not api_key:
                    continue

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None

                if not gemini_semaphore.acquire(timeout=remaining):
                    last_error = "Gemini concurrency kutish vaqti tugadi"
                    continue

                started = time.monotonic()
                try:
                    client = genai.Client(api_key=api_key)
                    response = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=extracted_text[:80000],
                        config=genai_types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            response_mime_type="application/json",
                            response_schema=QuizResponse,
                            temperature=0.2,
                        ),
                    )
                    elapsed = time.monotonic() - started
                    if elapsed > AI_REQUEST_TIMEOUT:
                        logging.warning(
                            "Katta AI request uzoq davom etdi (%0.1fs > %ss), ammo muvaffaqiyatli yakunlandi",
                            elapsed, AI_REQUEST_TIMEOUT
                        )

                    if response and response.text:
                        logging.info(
                            "AI muvaffaqiyatli | key=%s | retry=%s | %.1fs",
                            key_idx, retry_round + 1, elapsed
                        )
                        return response.text

                    last_error = "Gemini bo'sh javob qaytardi"

                except Exception as e:
                    last_error = str(e)
                    logging.warning(
                        "API kalit [%s] ishlamadi yoki vaqtincha xato berdi: %s. Keyingi keyga o'tilmoqda...",
                        key_idx, e
                    )
                finally:
                    gemini_semaphore.release()

                if time.monotonic() < deadline:
                    time.sleep(min(5.0, 0.75 * (2 ** retry_round)))

        logging.error("Barcha API key/retry urinishlari muvaffaqiyatsiz. Oxirgi xato: %s", last_error)
        return None
    finally:
        ai_queue_slots.release()

@app.post("/api/contact-admin")
async def api_contact_admin(request: Request):
    try:
        data = await request.json()
        user_id = int(data.get("user_id") or 0)
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id required")
        if not ADMIN_ID:
            user_lang = get_user_lang(user_id)
            return JSONResponse(
                status_code=503,
                content={"status": "error", "message": MESSAGES[user_lang]["support_config_error"]},
            )

        add_user_to_db(user_id)
        user_lang = get_user_lang(user_id)
        support_waiting_users.add(user_id)
        bot.send_message(user_id, MESSAGES[user_lang]["support_prompt"])
        logging.info(f"Admin support boshlandi: user_id={user_id}, admin_id={ADMIN_ID}")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Admin bilan bog'lanish API xatosi: {e}")
        raise HTTPException(status_code=500, detail="contact_admin_failed")

@app.get("/api/heartbeat")
def user_heartbeat(user_id: int):
    add_user_to_db(user_id)
    update_user_activity(user_id)
    return {"status": "ok", "active_users": get_users_count()}

@app.get("/api/quizzes")
def get_user_quizzes(user_id: int):
    add_user_to_db(user_id)
    total_users = get_users_count()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, title, total, answered, created_at, last_score, last_percent, is_public 
           FROM quizzes WHERE user_id = %s ORDER BY created_at DESC""",
        (user_id,)
    )
    personal_rows = cursor.fetchall()
    
    cursor.execute("SELECT language FROM users WHERE user_id = %s", (user_id,))
    lang_row = cursor.fetchone()
    user_lang = lang_row[0] if lang_row and lang_row[0] else "uz"
    conn.close()

    quizzes = [{
        "id": r[0],
        "title": r[1],
        "total": r[2],
        "answered": r[3],
        "created_at": r[4],
        "last_score": r[5],
        "last_percent": r[6],
        "is_public": r[7],
    } for r in personal_rows]
    return {
        "status": "ok",
        "quizzes": quizzes,
        "total_users": total_users,
        "user_lang": user_lang,
    }

@app.get("/api/public-quizzes")
def get_public_quizzes(user_id: int):
    add_user_to_db(user_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, plan_key, premium_until, public_free_used, created_at FROM users WHERE user_id = %s",
        (user_id,),
    )
    u = cursor.fetchone()
    now = int(time.time())
    
    u_created_at = u[4] if u and u[4] else now
    u_status = u[0] if u else ""
    u_premium_until = u[2] if u else 0
    u_public_free_used = u[3] if u else 0

    if u and (now - u_created_at) >= 30 * 24 * 3600 and not is_active_paid_status(u_status, u_premium_until):
        cursor.execute(
            "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = %s WHERE user_id = %s",
            (now, user_id),
        )
        conn.commit()
        public_free_used = 0
    else:
        public_free_used = u_public_free_used if u else 0

    is_paid = bool(u and is_active_paid_status(u_status, u_premium_until))
    public_remaining = max(0, FREE_PUBLIC_LIMIT - public_free_used)
    cursor.execute("SELECT id, title, total, created_at FROM quizzes WHERE is_public = 1 ORDER BY created_at DESC LIMIT 50")
    rows = cursor.fetchall()
    conn.close()

    quizzes = [
        {
            "id": r[0],
            "title": r[1],
            "total": r[2],
            "created_at": r[3],
            "locked": (not is_paid and public_remaining <= 0),
        }
        for r in rows
    ]
    return {
        "status": "ok",
        "quizzes": quizzes,
        "is_paid": is_paid,
        "public_free_used": public_free_used,
        "public_remaining": public_remaining,
        "public_limit": FREE_PUBLIC_LIMIT,
    }

@app.get("/api/public-quiz-detail")
def get_public_quiz_detail(quiz_id: str, user_id: int):
    add_user_to_db(user_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, plan_key, premium_until, public_free_used, created_at FROM users WHERE user_id = %s",
        (user_id,),
    )
    u = cursor.fetchone()
    now = int(time.time())
    
    u_created_at = u[4] if u and u[4] else now
    u_status = u[0] if u else ""
    u_premium_until = u[2] if u else 0
    u_public_free_used = u[3] if u else 0

    if u and (now - u_created_at) >= 30 * 24 * 3600 and not is_active_paid_status(u_status, u_premium_until):
        cursor.execute(
            "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = %s WHERE user_id = %s",
            (now, user_id),
        )
        conn.commit()
        public_free_used = 0
    else:
        public_free_used = u_public_free_used if u else 0

    is_paid = bool(u and is_active_paid_status(u_status, u_premium_until))
    public_remaining = max(0, FREE_PUBLIC_LIMIT - public_free_used)

    if not is_paid and public_remaining <= 0:
        conn.close()
        user_lang = get_user_lang(user_id)
        return {
            "status": "error",
            "error_code": "free_public_limit",
            "message": MESSAGES[user_lang]["public_limit_reached"],
        }

    cursor.execute("SELECT id, title, total, quiz_json FROM quizzes WHERE id = %s AND is_public = 1", (quiz_id,))
    q_row = cursor.fetchone()
    if not q_row:
        conn.close()
        return {"status": "error", "message": "Ommaviy test topilmadi."}

    if not is_paid:
        cursor.execute("UPDATE users SET public_free_used = public_free_used + 1 WHERE user_id = %s", (user_id,))
        conn.commit()
        notify_free_limit_reached(user_id, "public")

    conn.close()

    try:
        data = json.loads(q_row[3])
    except Exception:
        data = {}

    return {
        "status": "ok",
        "quiz": {
            "id": q_row[0],
            "title": q_row[1],
            "total": q_row[2],
            "quizzes": data.get("quizzes", []),
        }
    }
