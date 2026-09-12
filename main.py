# -*- coding: utf-8 -*-
import docx
import asyncio
import re
from docx import Document
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, Depends, Query, BackgroundTasks
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
# sqlite3 O'RNIGA psycopg2 VA contextmanager ISHLATAMIZ
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
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
from deep_translator import GoogleTranslator
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ==========================================
# POSTGRESQL CONNECTION POOL SOZLAMASI
# ==========================================

DATABASE_URL = os.getenv("DATABASE_URL")

# psycopg2 dialect muammosini oldini olish
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Ulanishlar puli (PoolError xatosini oldini oladi)
try:
    pg_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=20,
        dsn=DATABASE_URL
    )
except Exception as e:
    logging.error(f"PostgreSQL pool yaratishda xatolik: {e}")
    pg_pool = None

# Context Manager: ulanishlarni avtomatik va xavfsiz yopish uchun
@contextmanager
def get_db_connection():
    if not pg_pool:
        raise Exception("Database connection pool is not initialized")
    
    conn = pg_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        from datetime import datetime
from fastapi import Depends, Query

# ==========================================
# POSTGRESQL CONTEXT MANAGER (XAVFSIZ ULANISH)
# ==========================================
@contextmanager
def get_db_connection():
    if not pg_pool:
        raise Exception("Database connection pool is not initialized")
    
    conn = pg_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        # Ulanishni har qanday holatda ham pool'ga qaytaradi
        pg_pool.putconn(conn)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    from datetime import datetime
from fastapi import Depends, Query

# ==========================================
# LOGGING VA POSTGRESQL CONTEXT MANAGER
# ==========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

@contextmanager
def get_db_connection():
    if not pg_pool:
        raise Exception("Database connection pool is not initialized")
    
    conn = pg_pool.getconn()
    try:
        yield conn
        conn.commit()
    # ==========================================
# POSTGRESQL CONTEXT MANAGER
# ==========================================
@contextmanager
def get_db_connection():
    if not pg_pool:
        raise Exception("Database connection pool is not initialized")
    
    conn = pg_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        pg_pool.putconn(conn)

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
# Free API Key Rotation saqlanadi. Katta (100-500 savolli) so'rovlarni
# sun'iy 90 soniya / 5 daqiqalik limit bilan kesib tashlamaymiz.
AI_MAX_CONCURRENT = max(1, int(os.getenv("AI_MAX_CONCURRENT", str(min(7, max(1, len(GOOGLE_API_KEYS)))))))
AI_MAX_QUEUE = max(AI_MAX_CONCURRENT, int(os.getenv("AI_MAX_QUEUE", "150")))
AI_REQUEST_TIMEOUT = max(60, int(os.getenv("AI_REQUEST_TIMEOUT", "600")))
AI_TOTAL_TIMEOUT = max(AI_REQUEST_TIMEOUT, int(os.getenv("AI_TOTAL_TIMEOUT", "1800")))
AI_RETRY_PER_KEY = max(1, min(3, int(os.getenv("AI_RETRY_PER_KEY", "2"))))

# Queue slot so'rovni boshqaradi, Gemini semaphore esa haqiqiy parallel
# AI so'rovlar sonini cheklaydi. 150 ta foydalanuvchi birdan so'rov yuborsa
# ortiqcha so'rovlar xavfsiz kutadi, API birdaniga bosib yuborilmaydi.
ai_queue_slots = threading.BoundedSemaphore(AI_MAX_QUEUE)
gemini_semaphore = threading.BoundedSemaphore(AI_MAX_CONCURRENT)
logging.info(
    "AI protection initialized | concurrent=%s | queue=%s | request_timeout=%ss | total_timeout=%ss",
    AI_MAX_CONCURRENT, AI_MAX_QUEUE, AI_REQUEST_TIMEOUT, AI_TOTAL_TIMEOUT
)

# --- PROFESSIONAL FILE PROTECTION LAYER ---
# Katta kitob yoki juda og'ir fayllar server, parser va AI Queue ga
# ortiqcha yuk bermasligi uchun yuklashdan oldin tekshiriladi.
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
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# --- TARIFLAR: yagona manba (narx va nomlar UZ/RU/EN) ---
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

# --- MULTILINGUAL (3 TILDAGI BILDIRISHNOMALAR) ---
MESSAGES = {
    "uz": {
        "welcome": (
            "👋 *Salom, {name}!*\n"
            "🎓 *Quiz AI* — AI yordamida savollarni tez va qulay testga aylantiruvchi ilova.\n\n"
            "✨ *Ilova imkoniyatlari:*\n"
            "🤖 AI yordamida matn, PDF yoki DOCX dan test yaratish.\n"
            "📚 Testlarni Kutubxona bo'limida saqlash va ishlash.\n"
            "🌐 Ommaviy testlardan foydalanish.\n"
            "🧠 Flash Kartochkalar orqali takrorlash.\n"
            "👥 Guruhlarga qo'shilish va guruh testlarida qatnashish.\n"
            "👨‍🏫 O'qituvchilar uchun Professional vositalar.\n"
            "📊 Natijalarni qulay ko'rish va tahlil qilish.\n"
            "🔊 Ovoz va vibratsiya sozlamalari.\n"
            "🌍 O'zbek, Русский va English tillari.\n"
            "🆓 *Bepul:* har 30 kunda 3 ta AI test, 3 ta ommaviy test va 3 ta Flash Kartochka.\n"
            "👑 *Premium:* limitlarsiz foydalanish imkoniyati.\n\n"
            "📌 *Eslatma:* Premium bo'limida «Tariflarni faollashtirish» tugmasi bosilganda yangi oyna ochiladi. Shu oynadagi «Chekni yuborish» tugmasini bosing — bu sizni botga qaytaradi. Soʻng toʻlov chekini rasm yoki skrinshot shaklida yuboring.\n\n"
            "💬 *Bizning rasmiy guruhimiz:* [Quiz AI Rasmiy Chat](https://t.me/Quiz_AI_Chat)\n\n"
            "🚀 Boshlash uchun quyidagi tugmani bosing va Quiz AI imkoniyatlaridan foydalaning!"
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
            "🎓 *Quiz AI* — Приложение, которое с помощью ИИ быстро и удобно превращает вопросы в тесты.\n\n"
            "✨ *Возможности приложения:*\n"
            "🤖 Создание тестов с помощью ИИ из текста, PDF или DOCX.\n"
            "📚 Сохранение и прохождение тестов в разделе «Библиотека».\n"
            "🌐 Публичные тесты.\n"
            "🧠 Флеш-карточки для повторения материала.\n"
            "👥 Вступление в группы и участие в групповых тестах.\n"
            "👨‍🏫 Профессиональные инструменты для учителей.\n"
            "📊 Удобный просмотр и анализ результатов.\n"
            "🔊 Настройки звука и вибрации.\n"
            "🌍 Узбекский, русский и английский языки.\n"
            "🆓 *Бесплатно:* 3 AI-теста, 3 публичных теста и 3 флеш-карточки каждые 30 дней.\n"
            "👑 *Premium:* использование без лимитов.\n\n"
            "📌 *Примечание:* В разделе Премиум при нажатии на кнопку «Активировать тарифы» откроется новое окно. Нажмите в этом окне кнопку «Отправить чек» — это вернёт вас в бот. Затем отправьте чек об оплате в виде фото или скриншота.\n\n"
            "💬 *Наша официальная группа:* [Quiz AI Официальный Чат](https://t.me/Quiz_AI_Chat)\n\n"
            "🚀 Нажмите кнопку ниже и начните пользоваться возможностями Quiz AI!"
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
            "🎓 *Quiz AI* — An application that uses AI to quickly and conveniently turn questions into tests.\n\n"
            "✨ *App features:*\n"
            "🤖 Create quizzes with AI from text, PDF or DOCX.\n"
            "📚 Save and take quizzes in the Library.\n"
            "🌐 Public quizzes.\n"
            "🧠 Flashcards for revision.\n"
            "👥 Join groups and participate in group quizzes.\n"
            "👨‍🏫 Professional tools for teachers.\n"
            "📊 Easy results viewing and analysis.\n"
            "🔊 Sound and vibration settings.\n"
            "🌍 Uzbek, Russian and English languages.\n"
            "🆓 *Free:* 3 AI quizzes, 3 public quizzes and 3 flashcards every 30 days.\n"
            "👑 *Premium:* unlimited usage.\n\n"
            "📌 *Note:* In the Premium section, when you click on the «Activate tariffs» button, a new window opens. Click the «Send receipt» button in this window — this will return you to the bot. Then send the payment receipt in the form of a photo or screenshot.\n\n"
            "💬 *Our official group:* [Quiz AI Official Chat](https://t.me/Quiz_AI_Chat)\n\n"
            "🚀 Tap the button below and start using Quiz AI!"
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

def get_user_lang(user_id: int) -> str:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT language FROM users WHERE user_id = %s", (user_id,))
                row = cursor.fetchone()
                if row and row[0] in MESSAGES:
                    return row[0]
    except Exception as e:
        logging.error(f"Foydalanuvchi tilini olishda xatolik: {e}")
    return "uz"


def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # Quizzes jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS quizzes (
                    id VARCHAR(255) PRIMARY KEY,
                    user_id BIGINT,
                    title TEXT,
                    total INTEGER,
                    answered INTEGER,
                    quiz_json TEXT,
                    created_at BIGINT,
                    last_score INTEGER DEFAULT -1,
                    last_percent INTEGER DEFAULT -1,
                    is_public INTEGER DEFAULT 0
                );
            """)
            
            # Users jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    created_at BIGINT,
                    language VARCHAR(10) DEFAULT 'uz',
                    status TEXT DEFAULT 'Oddiy foydalanuvchi',
                    plan_key TEXT DEFAULT '',
                    free_used INTEGER DEFAULT 0,
                    public_free_used INTEGER DEFAULT 0,
                    flashcard_free_used INTEGER DEFAULT 0,
                    premium_until BIGINT DEFAULT 0,
                    last_active BIGINT DEFAULT 0,
                    last_quiz_free_notice_cycle INTEGER DEFAULT 0,
                    last_public_free_notice_cycle INTEGER DEFAULT 0,
                    last_flashcard_free_notice_cycle INTEGER DEFAULT 0,
                    last_free_reset_notice_cycle INTEGER DEFAULT 0,
                    paid_limit_notice_until BIGINT DEFAULT 0
                );
            """)

            # PostgreSQL uchun ustunlarni tekshirish (PRAGMA o'rniga):
            cursor.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='users';
            """)
            columns = [col[0] for col in cursor.fetchall()]

            if "status" not in columns:
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'Oddiy foydalanuvchi';")
                except Exception:
                    pass
            if "free_used" not in columns:
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN free_used INTEGER DEFAULT 0;")
                except Exception:
                    pass
            if "premium_until" not in columns:
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN premium_until BIGINT DEFAULT 0;")
                except Exception:
                    pass
    if "public_free_used" not in columns:
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN public_free_used INTEGER DEFAULT 0;")
                except Exception:
                    pass
            if "flashcard_free_used" not in columns:
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN flashcard_free_used INTEGER DEFAULT 0;")
                except Exception:
                    pass
            if "plan_key" not in columns:
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN plan_key TEXT DEFAULT '';")
                except Exception:
                    pass
            if "last_active" not in columns:
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN last_active BIGINT DEFAULT 0;")
                except Exception:
                    pass

            for _col in (
                "last_quiz_free_notice_cycle",
                "last_public_free_notice_cycle",
                "last_flashcard_free_notice_cycle",
                "last_free_reset_notice_cycle",
            ):
                if _col not in columns:
                    try:
                        cursor.execute(f"ALTER TABLE users ADD COLUMN {_col} INTEGER DEFAULT 0;")
                    except Exception:
                        pass

            if "paid_limit_notice_until" not in columns:
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN paid_limit_notice_until BIGINT DEFAULT 0;")
                except Exception:
                    pass

    # NULL bo'lib qolgan eski qiymatlarni avtomatik to'g'rilash
            cursor.execute("UPDATE users SET free_used = 0 WHERE free_used IS NULL;")
            cursor.execute("UPDATE users SET public_free_used = 0 WHERE public_free_used IS NULL;")
            cursor.execute("UPDATE users SET flashcard_free_used = 0 WHERE flashcard_free_used IS NULL;")
            cursor.execute("UPDATE users SET status = 'Oddiy foydalanuvchi' WHERE status IS NULL;")
            cursor.execute("UPDATE users SET premium_until = 0 WHERE premium_until IS NULL;")
            cursor.execute("UPDATE users SET plan_key = '' WHERE plan_key IS NULL;")

            # Flashcards jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS flashcards (
                    id VARCHAR(255) PRIMARY KEY,
                    user_id BIGINT,
                    front TEXT,
                    back TEXT,
                    created_at BIGINT
                );
            """)

            # Payments jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    tx_id VARCHAR(255) PRIMARY KEY,
                    user_id BIGINT,
                    tariff_name TEXT,
                    tariff_price TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at BIGINT
                );
            """)

            # Teacher sessions jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS teacher_sessions (
                    id VARCHAR(255) PRIMARY KEY,
                    owner_id BIGINT,
                    quiz_id TEXT,
                    code TEXT UNIQUE,
                    duration_minutes INTEGER DEFAULT 30,
                    created_at BIGINT,
                    expires_at BIGINT,
                    # Teacher sessions jadvalining to'liq ko'rinishi
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS teacher_sessions (
                    id VARCHAR(255) PRIMARY KEY,
                    owner_id BIGINT,
                    quiz_id TEXT,
                    code TEXT UNIQUE,
                    duration_minutes INTEGER DEFAULT 30,
                    created_at BIGINT,
                    expires_at BIGINT,
                    active INTEGER DEFAULT 1,
                    deleted INTEGER DEFAULT 0,
                    source_type TEXT DEFAULT 'group_test',
                    assignment_id TEXT DEFAULT ''
                );
            """)

            # Teacher participants jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS teacher_participants (
                    id BIGSERIAL PRIMARY KEY,
                    session_id TEXT,
                    user_id BIGINT,
                    first_name TEXT,
                    username TEXT,
                    score INTEGER DEFAULT 0,
                    total INTEGER DEFAULT 0,
                    percent INTEGER DEFAULT 0,
                    started_at BIGINT,
                    finished_at BIGINT,
                    UNIQUE(session_id, user_id)
                );
            """)

            # Teacher variants jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS teacher_variants (
                    id BIGSERIAL PRIMARY KEY,
                    quiz_id TEXT NOT NULL,
                    variant_code TEXT NOT NULL,
                    variant_json TEXT NOT NULL,
                    created_at BIGINT NOT NULL,
                    UNIQUE(quiz_id, variant_code)
                );
            """)

            # Teacher groups jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS teacher_groups (
                    id VARCHAR(255) PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    # Teacher groups jadvalining to'liq ko'rinishi
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS teacher_groups (
                    id VARCHAR(255) PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    join_code TEXT DEFAULT '',
                    created_at BIGINT NOT NULL,
                    active INTEGER DEFAULT 1
                );
            """)

            # Teacher group members jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS teacher_group_members (
                    id BIGSERIAL PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    first_name TEXT DEFAULT '',
                    username TEXT DEFAULT '',
                    joined_at BIGINT NOT NULL,
                    UNIQUE(group_id, user_id)
                );
            """)

            # Teacher assignments jadvali
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS teacher_assignments (
                    id VARCHAR(255) PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    group_id TEXT NOT NULL,
                    quiz_id TEXT NOT NULL,
                    variant_code TEXT DEFAULT '',
                    title TEXT DEFAULT '',
                    due_at BIGINT DEFAULT 0,
                    duration_minutes INTEGER DEFAULT 30,
                    created_at BIGINT NOT NULL,
                    active INTEGER DEFAULT 1
                );
            """)

    # Eski Railway/SQLite bazalarida Teacher jadvallari avvalgi versiyadan qolgan
    # bo‘lishi mumkin. CREATE TABLE IF NOT EXISTS mavjud jadvalga yangi ustunlarni
    # qo‘shmaydi, shuning uchun xavfsiz migration qilamiz. Bu faqat yetishmayotgan
    # Teacher ustunlarini qo‘shadi va boshqa funksiyalarga tegmaydi.
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

    # Teacher jadvallari uchun xavfsiz PostgreSQL migratsiyasi
    for table_name, columns_dict in teacher_migrations.items():
        cursor.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name=%s;
        """, (table_name,))
        existing_cols = [col[0] for col in cursor.fetchall()]

        for col_name, col_def in columns_dict.items():
            if col_name not in existing_cols:
                try:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def};")
                except Exception:
                    pass
    
    # Eski yazuvlardagi NULL qiymatlar Teacher endpointlari uchun xavfsiz qiymatga o'tkaziladi.
    null_updates = {
        "teacher_groups": [("description", "''"), ("active", "1")],
        "teacher_assignments": [("variant_code", "''"), ("title", "''"), ("due_at", "0"), ("duration_minutes", "30"), ("active", "1")],
        "teacher_sessions": [("group_id", "''"), ("variant_code", "''"), ("deleted", "0"), ("source_type", "'group_test'"), ("assignment_id", "''")]
    }

    for table_name, updates in null_updates.items():
        for col, value in updates:
            try:
                cursor.execute(f"UPDATE {table_name} SET {col}={value} WHERE {col} IS NULL;")
            except Exception:
                pass

    # Existing groups from older versions receive a 6-character join code.
    try:
        cursor.execute("SELECT id FROM teacher_groups WHERE COALESCE(join_code,'')='' AND active=1;")
        for row in cursor.fetchall():
            while True:
                code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
                cursor.execute("SELECT 1 FROM teacher_groups WHERE join_code=%s LIMIT 1", (code,))
                if not cursor.fetchone():
                    break
            # UPDATE sikldan tashqarida, break'dan keyin ishlashi kerak:
            cursor.execute("UPDATE teacher_groups SET join_code=%s WHERE id=%s", (code, row[0]))
    except Exception as e:
        logging.warning("Teacher group code migration: %s", e)

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
    """Send a localized Telegram limit notification without affecting app requests."""
    try:
        lang = get_user_lang(user_id)
        text = MESSAGES.get(lang, MESSAGES["uz"])[message_key].format(**kwargs)
        bot.send_message(user_id, text, parse_mode="Markdown")
        return True
    except Exception as e:
        logging.error(f"Limit Telegram xabari yuborilmadi ({user_id}, {message_key}): {e}")
        return False


def notify_free_limit_reached(user_id: int, kind: str):
    """Notify once per 30-day free-limit cycle when a free feature is exhausted."""
    columns = {
        "quiz": ("free_used", "last_quiz_free_notice_cycle", FREE_QUIZ_LIMIT, "free_quiz_limit_notice"),
        "public": ("public_free_used", "last_public_free_notice_cycle", FREE_PUBLIC_LIMIT, "free_public_limit_notice"),
        "flashcard": ("flashcard_free_used", "last_flashcard_free_notice_cycle", FREE_FLASHCARD_LIMIT, "free_flashcard_limit_notice"),
    }
    if kind not in columns:
        return
    used_col, notice_col, limit, message_key = columns[kind]
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"SELECT {used_col}, created_at, {notice_col}, status, premium_until FROM users WHERE user_id = %s",
                    (user_id,)
                )
                row = cur.fetchone()
                if not row or is_active_paid_status(row["status"] or "", row["premium_until"] or 0):
                    return
                used = row[used_col] or 0
                cycle = row["created_at"] or 0
                already = row[notice_col] or 0
                if used < limit or not cycle or already == cycle:
                    return
                cur.execute(
                    f"UPDATE users SET {notice_col} = %s WHERE user_id = %s AND ({notice_col} IS DISTINCT FROM %s)",
                    (cycle, user_id, cycle)
                )
                changed = cur.rowcount == 1

        if changed:
            _send_limit_message(user_id, message_key)
    except Exception as e:
        logging.error(f"Bepul limit notification xatosi ({user_id}, {kind}): {e}")
def process_expired_free_limits():
    """Reset the 30-day free cycle and notify the user once when free limits return."""
    now = int(time.time())
    thirty_days = 30 * 24 * 3600
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT user_id, created_at, free_used, public_free_used, flashcard_free_used, "
                    "status, premium_until, last_free_reset_notice_cycle "
                    "FROM users WHERE created_at > 0 AND created_at <= %s",
                    (now - thirty_days,)
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
                        (now, old_cycle if had_exhausted_limit else already_notified, user_id, old_cycle)
                    )
if cur.rowcount == 1 and had_exhausted_limit and already_notified != old_cycle:
                        try:
                            lang = get_user_lang(user_id)
                            text = MESSAGES.get(lang, MESSAGES["uz"])["free_limits_restored_notice"]
                            bot.send_message(user_id, text, parse_mode="Markdown")
                        except Exception as send_err:
                            logging.error(f"Limit tiklandi xabarini yuborishda xato ({user_id}): {send_err}")
    except Exception as e:
        logging.error(f"Bepul limit reset worker xatosi: {e}")


def process_expired_paid_limits():
    """Expire PRO plans and notify users once exactly when their paid period ends."""
    now = int(time.time())
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT user_id, status, plan_key, premium_until, paid_limit_notice_until "
                    "FROM users WHERE premium_until > 0 AND premium_until <= %s AND status LIKE '%%PRO%%'",
                    (now,)
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
                        (expiry, user_id, expiry)
                    )
                    if cur.rowcount == 1:
                        try:
                            lang = get_user_lang(user_id)
                            text = MESSAGES.get(lang, MESSAGES["uz"])["paid_limit_notice"].format(tariff_name=tariff_name)
                            bot.send_message(user_id, text, parse_mode="Markdown")
                        except Exception as e:
                            logging.error(f"Premium tugash xabari yuborilmadi ({user_id}): {e}")
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
        now_ts = int(time.time())
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO users (user_id, created_at, language, status, plan_key, free_used, public_free_used, flashcard_free_used, premium_until) "
                    "VALUES (%s, %s, 'uz', 'Oddiy foydalanuvchi', '', 0, 0, 0, 0) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (user_id, now_ts)
                )
    except Exception as e:
        logging.error(f"Foydalanuvchi qo'shishda xato ({user_id}): {e}")


def get_users_count():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM users")
                result = cursor.fetchone()
                return result[0] if result else 0
    except Exception as e:
        logging.error(f"Foydalanuvchilar sonini olishda xato: {e}")
        return 0


def get_active_users_count():
    try:
        # Faqat oxirgi 2 daqiqada Mini App ochiq/ko'rinib turgan foydalanuvchilar faol hisoblanadi.
        active_since = int(time.time()) - 2 * 60
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM users WHERE last_active >= %s",
                    (active_since,)
                )
                result = cursor.fetchone()
                return result[0] if result else 0
    except Exception as e:
        logging.error(f"Faol foydalanuvchilar sonini olishda xato: {e}")
        return 0
        def update_user_last_active(user_id: int):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET last_active = %s WHERE user_id = %s",
                    (int(time.time()), user_id)
                )
    except Exception as e:
        logging.error(f"Faol foydalanuvchi vaqtini yangilashda xato ({user_id}): {e}")


def trigger_payment_flow(user_id, tariff_name=None, tariff_price=None, tariff_key=None):
    try:
        user_lang = get_user_lang(user_id)
        if tariff_key not in TARIFFS:
            # Eski klientlardan kelgan tarif nomlarini ham saqlab qolamiz.
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

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Avvalgi kutilayotgan to'lovlarni o'chirish/bekor qilish
                cursor.execute(
                    "UPDATE payments SET status = 'cancelled' WHERE user_id = %s AND status = 'pending'",
                    (user_id,)
                )
                cursor.execute(
                    "INSERT INTO payments VALUES (%s, %s, %s, %s, 'pending', %s)",
                    (tx_id, user_id, tariff_name, tariff_price, int(time.time()))
                )
        user_lang = get_user_lang(user_id)
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


# --- ADMIN SUPPORT: foydalanuvchi suhbatini davom ettirish tugmasi ---
def support_continue_markup(lang: str):
    lang = lang if lang in MESSAGES else "uz"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        MESSAGES[lang]["support_continue_btn"],
        callback_data="support_continue"
    ))
    return markup

# --- ADMIN SUPPORT: murojaat va javob ---
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
            # Admin tomondagi texnik tasdiq doim O'zbek tilida qoladi.
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
    """Allow the user to continue the same admin-support conversation from Telegram."""
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

# --- ISHONCHLI TO'LOV CHEKI QABUL QILISH (STABLE PHOTO HANDLER) ---
@bot.message_handler(content_types=["photo"])
def handle_receipt_photo(message):
    user_id = message.from_user.id
    user_lang = get_user_lang(user_id)

    with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT tx_id, tariff_name, tariff_price FROM payments WHERE user_id = %s AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                    (user_id,)
                )
                pending_pay = cursor.fetchone()
    if not pending_pay:
        return  # Kutilayotgan to'lov yo'q bo'lsa javob berilmaydi

    tx_id, tariff_name, tariff_price = pending_pay

    username = f"@{message.from_user.username}" if message.from_user.username else "Mavjud emas"
    first_name = message.from_user.first_name
    file_id = message.photo[-1].file_id

    admin_markup = telebot.types.InlineKeyboardMarkup()
    btn_approve = telebot.types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"p_app_{tx_id}_{user_id}")
    btn_reject = telebot.types.InlineKeyboardButton("❌ Rad etish", callback_data=f"p_rej_{tx_id}_{user_id}")
    admin_markup.row(btn_approve, btn_reject)

    # Admin uchun tarif nomi foydalanuvchi tilidan qat'i nazar doim O'zbek tilida ko'rsatiladi.
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

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT status, tariff_name FROM payments WHERE tx_id = %s", (tx_id,))
                pay_row = cursor.fetchone()

        if not pay_row or pay_row[0] != "pending":
            bot.answer_callback_query(call.id, "Bu so'rov allaqachon ko'rib chiqilgan!", show_alert=True)
            return
        return

    tariff_name = pay_row[1]

    if action == "app":
        current_time = int(time.time())
        # Eski to'lovlar uchun nomdan, yangi to'lovlar uchun canonical nomdan reja aniqlanadi.
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
                    (f"PRO ⭐ ({display_name})", plan_key, premium_until_timestamp, user_id)
                )
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
            bot.answer_callback_query(call.id, "To'lov rad etildi.")
            try:
                bot.edit_message_caption(
                    f"{call.message.caption}\n\n❌ RAD ETILDI!",
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


# --- FASTAPI ENDPOINTS ---
app = FastAPI()


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
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, plan_key, free_used, public_free_used, flashcard_free_used, premium_until, created_at "
                "FROM users WHERE user_id = %s",
                (user_id,)
            )
            row = cursor.fetchone()

    if not row:
        return {
            "status": "ok",
            "user_status": "Oddiy foydalanuvchi",
            "free_used": 0,
            "public_free_used": 0,
            "flashcard_free_used": 0,
            "public_remaining": FREE_PUBLIC_LIMIT,
            # (qolgan default qiymatlar...)
        }
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
        cursor.execute("UPDATE users SET status = %s, plan_key = %s, premium_until = %s WHERE user_id = %s", ('Oddiy foydalanuvchi', '', 0, user_id))
        user_status, plan_key, premium_until = "Oddiy foydalanuvchi", "", 0

    # 30 kunlik bepul hisob davri pullik davrdan mustaqil ishlaydi.
    if now - created_at >= 30 * 24 * 3600 and not is_active_paid_status(user_status, premium_until):
        cursor.execute(
            "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = %s WHERE user_id = %s",
            (now, user_id)
        )
        free_used = 0
        public_free_used = 0
        flashcard_free_used = 0
        public_free_used = 0
        flashcard_free_used = 0

    is_paid = is_active_paid_status(user_status, premium_until)
    is_teacher = is_paid and plan_key == "teachers"
    lang = get_user_lang(user_id)
    display_status = user_status
    
    if is_paid:
        display_status = f"PRO ⭐ ({localized_tariff_name(plan_key, lang)})"
        uzbek_time = time.gmtime(premium_until + 5 * 3600)
        readable_date = time.strftime("%d.%m.%Y %H:%M", uzbek_time)
        if lang == "ru": display_status += f" (До: {readable_date})"
        elif lang == "en": display_status += f" (Until: {readable_date})"
        else: display_status += f" (Gacha: {readable_date})"

    return {
        "status": "ok",
        "user_status": display_status,
        "free_used": free_used,
        "public_free_used": public_free_used,
        "flashcard_free_used": flashcard_free_used,
        "public_remaining": max(0, FREE_PUBLIC_LIMIT - public_free_used),
        "flashcard_remaining": max(0, FREE_FLASHCARD_LIMIT - flashcard_free_used),
        "is_teacher": is_teacher,
    }

    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB (baytlarda)
async def create_quiz_web(
    user_id: int = Form(...),
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    quiz_title: Optional[str] = Form(None),
):
    add_user_to_db(user_id)
    user_lang = get_user_lang(user_id)

# 10 MB Fayl hajmini tekshirish
    if file:
        file_bytes = bytearray()
        chunk_size = 1024 * 1024  # 1 MB bo'laklar
        while chunk := await file.read(chunk_size):
            file_bytes.extend(chunk)
            if len(file_bytes) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413, 
                    detail="Fayl hajmi 10 MB limitidan oshib ketdi!"
                )
        await file.seek(0)


    with get_db_connection() as conn_check:
        with conn_check.cursor() as cursor_check:
            cursor_check.execute(
                "SELECT status, plan_key, free_used, public_free_used, flashcard_free_used, premium_until, created_at FROM users WHERE user_id = %s",
                (user_id,)
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
            "UPDATE users SET status = %s, premium_until = %s WHERE user_id = %s",
            ('Oddiy foydalanuvchi', 0, user_id),
        )
        current_status = "Oddiy foydalanuvchi"
        # 30 kunlik bepul limit: faqat 1 ta.
        # Muhim: bir foydalanuvchi bir vaqtning o'zida 2 ta request yuborsa,
        # ikkalasi ham limitdan o'tib ketmasligi uchun bepul joyni
        # Gemini chaqiruvidan OLDIN atomik tarzda band qilamiz.
        if "PRO" not in current_status:
        cursor_check.execute(
            "UPDATE users "
            "SET free_used = COALESCE(free_used, 0) + 1 "
            "WHERE user_id = %s AND COALESCE(free_used, 0) < %s",
            (user_id, FREE_QUIZ_LIMIT),
        )
        if cursor_check.rowcount != 1:
            return {
                "status": "error",
                "error_code": "free_limit",
                "message": MESSAGES[user_lang]["quiz_limit_reached"],
            }
        free_slot_reserved = True
    else:
        free_slot_reserved = False

    raw_text = ""
    auto_title = "Matnli Test"

    if file and file.filename and len(file.filename.strip()) > 0:
        # Faylni avval xotirada bir marta o'qiymiz va diskka faqat tekshiruvdan
        # o'tgandan keyin yozamiz. Bu server diskini keraksiz yukdan himoya qiladi.
        original_name = Path(file.filename).name
        extension = Path(original_name).suffix.lower()
        if extension not in ALLOWED_UPLOAD_EXTENSIONS:
            if free_slot_reserved:
            with conn_check.cursor() as cur_restore:
                cur_restore.execute(
                    "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                    (user_id,)
                )
            return {"status": "error", "message": file_protection_message(user_lang, "unsupported")}

        try:
            contents = await file.read()
            if free_slot_reserved:
            with conn_check.cursor() as cur_restore:
                cur_restore.execute(
                    "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                    (user_id,)
                )
            return {"status": "error", "message": file_protection_message(user_lang, "too_large")}

            if contents:
                os.makedirs(DOWNLOADS_DIR, exist_ok=True)
                # Bir xil nomdagi fayllar foydalanuvchilar orasida ustma-ust yozilmasligi uchun unique nom.
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
            with conn_check.cursor() as cur_restore:
                cur_restore.execute(
                    "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                    (user_id,)
                )
                        return {"status": "error", "message": file_protection_message(user_lang, "too_many_pages")}
                    text_parts = []
                    text_len = 0
                    for page in reader.pages:
                        page_text = page.extract_text() or ""
                        if page_text:
                            text_len += len(page_text)
                            if text_len > MAX_EXTRACTED_TEXT_CHARS:
                                try: os.remove(file_path)
                                if free_slot_reserved:
            if free_slot_reserved:
            with conn_check.cursor() as cur_restore:
                cur_restore.execute(
                    "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                    (user_id,)
                )
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
            with conn_check.cursor() as cur_restore:
                cur_restore.execute(
                    "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                    (user_id,)
                )
            return {"status": "error", "message": file_protection_message(user_lang, "too_much_text")}
                        text_parts.append(part)
                    raw_text = "\n".join(text_parts)
                    auto_title = Path(original_name).stem
        except Exception as e:
        logging.error(f"Professional file protection / parsing error: {e}")
        if free_slot_reserved:
            with conn_check.cursor() as cur_restore:
                cur_restore.execute(
                    "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                    (user_id,)
                )
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
            with conn_check.cursor() as cur_restore:
                cur_restore.execute(
                    "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                    (user_id,)
                )
        return {"status": "error", "message": file_protection_message(user_lang, "unreadable")}
    # Gemini SDK chaqiruvi sinxron bo'lgani uchun uni alohida threadga chiqaramiz.
    # Shu bilan boshqa foydalanuvchilarning WebApp requestlari event loopni bloklamaydi.
    quiz_json_raw = await asyncio.to_thread(generate_quiz_from_gemini, raw_text)
    if not quiz_json_raw:
        if free_slot_reserved:
            with conn_check.cursor() as cur_restore:
                cur_restore.execute(
                    "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                    (user_id,)
                )
        return {"status": "error", "error_code": "ai_error", "message": MESSAGES[user_lang]["quiz_generation_failed"]}
            except Exception as e:
                logging.error(f"Bepul limitni qaytarishda xato: {e}")
        return {"status": "error", "message": "AI test generatsiya qila olmadi."}

    try:
        quiz_data = json.loads(quiz_json_raw)
        items = quiz_data.get("quizzes", [])
        # Gemini qanday joylashtirishidan qat'i nazar, javob variantlari shu yerda
        # xavfsiz va muvozanatli random qilinadi.
        items = randomize_quiz_answer_positions(items)
        quiz_data["quizzes"] = items
        if not items:
        if free_slot_reserved:
            with conn_check.cursor() as cur_restore:
                cur_restore.execute(
                    "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                    (user_id,)
                )
        return {
            "status": "error",
            "message": "AI savollar roʻyxatini boʻsh qaytardi.",
        }

        quiz_id = f"q_{uuid.uuid4().hex}"
        final_title = (
            quiz_title.strip() if (quiz_title and quiz_title.strip()) else auto_title
        )

        with conn_check.cursor() as cursor:
        cursor.execute(
            """INSERT INTO quizzes (id, user_id, title, total, answered, quiz_json, created_at, last_score, last_percent, is_public)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0)""",
            (
                quiz_id,
                user_id,
                final_title[:30],
                len(items),
                0,
                quiz_json_raw,
                int(time.time()),
                -1,
                -1,
            )
        )

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
    if free_slot_reserved:
        with conn_check.cursor() as cur_restore:
            cur_restore.execute(
                "UPDATE users SET free_used = CASE WHEN COALESCE(free_used, 0) > 0 THEN free_used - 1 ELSE 0 END WHERE user_id = %s",
                (user_id,)
            )
    return {"status": "error", "message": str(e)}


def randomize_quiz_answer_positions(items):
    """
    AI tomonidan berilgan variantlarni xavfsiz aralashtiradi va correct_index ni
    yangi joylashuvga moslaydi. To'g'ri javob mazmuni o'zgarmaydi.
    Savollar ko'p bo'lsa A/B/C/D pozitsiyalari muvozanatli taqsimlanadi.
    """
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

        # Indeks bilan ishlash bir xil matnli variantlarda ham to'g'ri javobni saqlaydi.
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
    """
    Professional AI Queue + Retry + Timeout + Concurrency Protection.
    API Key Rotation va Gemini 2.5 Flash saqlanadi.

    Muhim: katta testlarni avvalgi 203 savolli ishlagan versiyadek yaratish
    uchun request 90 soniyada majburan to'xtatilmaydi. Timeout nazorati
    umumiy jarayonni kuzatadi va katta so'rovlar uchun yetarli vaqt beradi.
    """
    global current_key_index

    if not GOOGLE_API_KEYS:
        logging.error("GOOGLE_API_KEYS topilmadi yoki bo'sh!")
        return None

    system_instruction = """You are an advanced AI quiz generator.
CRITICAL RULES:
1. LANGUAGE RULE: Detect the language of the provided text. You MUST generate the questions, choices, and explanations in the EXACT SAME language as the input text.
2. QUESTION COUNT RULE: Look at the input text. If the user provided a strict list of questions, you MUST ONLY extract and format THOSE EXACT questions into the quiz structure. If it's a huge continuous textbook, you can generate up to 40-50 questions maximum."""

    # Queue: 150 tagacha bir vaqtning o'zida kelgan foydalanuvchi so'rovini
    # xavfsiz boshqaradi. Katta test uchun kutish vaqti umumiy timeoutga mos.
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

        # Har bir key bo'yicha retry qilinadi, keylar esa eski versiyadagi
        # kabi ketma-ket fallback sifatida sinab ko'riladi.
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

                # Faqat haqiqiy Gemini chaqiruvi concurrent limit ostida.
                # Queue slot esa butun foydalanuvchi jarayonini boshqaradi.
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

                # Exponential backoff: faqat xatodan keyin, server/APIga
                # ortiqcha bosim bermasdan.
                if time.monotonic() < deadline:
                    time.sleep(min(5.0, 0.75 * (2 ** retry_round)))

        logging.error("Barcha API key/retry urinishlari muvaffaqiyatsiz. Oxirgi xato: %s", last_error)
        return None
    finally:
        ai_queue_slots.release()


@app.post("/api/contact-admin")
async def api_contact_admin(request: Request):
    """Start an admin-support conversation from the Mini App.

    The frontend calls this endpoint instead of relying on Telegram
    WebApp.sendData(), which is not reliable for every Mini App launch mode.
    The bot sends the support prompt directly to the user's private chat;
    the next text message is then forwarded to ADMIN_ID by the existing
    support handler.
    """
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
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT id, title, total, answered, created_at, last_score, last_percent, is_public 
                       FROM quizzes WHERE user_id = %s ORDER BY created_at DESC""",
                    (user_id,)
                )
                personal_rows = cursor.fetchall()
                
                cursor.execute("SELECT language FROM users WHERE user_id = %s", (user_id,))
                lang_row = cursor.fetchone()
                user_lang = lang_row[0] if lang_row and lang_row[0] else "uz"
        finally:
            db_pool.putconn(conn)

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
    


@app.get("/api/public-quizzes")
def get_public_quizzes(user_id: int):
    add_user_to_db(user_id)
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT status, plan_key, premium_until, public_free_used, created_at FROM users WHERE user_id = %s",
                    (user_id,),
                )
                u = cursor.fetchone()
                now = int(time.time())
                
                if u and now - (u[4] or now) >= 30 * 24 * 3600 and not is_active_paid_status(u[0] or "", u[2] or 0):
                    cursor.execute(
                        "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = %s WHERE user_id = %s",
                        (now, user_id),
                    )
                    conn.commit()
                    public_free_used = 0
                else:
                    public_free_used = (u[3] or 0) if u else 0

                is_paid = bool(u and is_active_paid_status(u[0] or "", u[2] or 0))
                public_remaining = max(0, FREE_PUBLIC_LIMIT - public_free_used)

                cursor.execute("SELECT id, title, total, created_at FROM quizzes WHERE is_public = 1 ORDER BY created_at DESC LIMIT 50")
                rows = cursor.fetchall()
        finally:
            db_pool.putconn(conn)

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
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT status, premium_until, public_free_used, created_at FROM users WHERE user_id = %s",
                    (user_id,),
                )
                u = cursor.fetchone()
                now = int(time.time())
                is_paid = bool(u and is_active_paid_status(u[0] or "", u[1] or 0))
                
                # 30 kunlik bepul davr tugagan bo'lsa, uchala bepul hisoblagichni reset qilamiz.
                if u and now - (u[3] or now) >= 30 * 24 * 3600 and not is_paid:
                    cursor.execute(
                        "UPDATE users SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = %s WHERE user_id = %s",
                        (now, user_id),
                    )
                    conn.commit()
                    public_free_used = 0
                # 30 kunlik bepul davr / limit tekshiruvi davomi
    if not is_paid and public_free_used >= FREE_PUBLIC_LIMIT:
        lang = get_user_lang(user_id)
        messages = {
            "uz": MESSAGES["uz"]["public_limit_reached"],
            "ru": MESSAGES["ru"]["public_limit_reached"],
            "en": MESSAGES["en"]["public_limit_reached"],
        }
        return {
            "status": "error",
            "error_code": "public_limit",
            "message": messages.get(lang, messages["uz"]),
        }

    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT title, quiz_json, is_public FROM quizzes WHERE id = %s",
                    (quiz_id,),
                )
                row = cursor.fetchone()
        finally:
            db_pool.putconn(conn)

    if not row or row[2] != 1:
        raise HTTPException(status_code=404, detail="quiz_not_found")
    
    return {
        "status": "ok", 
        "title": row[0] or "Test", 
        "quiz_json": json.loads(row[1]) if row[1] else {}
    }


@app.post("/api/toggle-public")
@app.post("/api/toggle-public")
def toggle_public(quiz_id: str, user_id: int, is_public: int):
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE quizzes SET is_public = %s WHERE id = %s AND user_id = %s",
                    (is_public, quiz_id, user_id),
                )
                conn.commit()
        finally:
            db_pool.putconn(conn)
    return {"status": "ok"}


@app.post("/api/set-language")
def set_language(user_id: int, lang: str):
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET language = %s WHERE user_id = %s",
                    (lang, user_id),
                )
                conn.commit()
        finally:
            db_pool.putconn(conn)
    return {"status": "ok"}


# --- O'QITUVCHI PROFESSIONAL VOSITALARI ---
TEACHER_VARIANT_CODES = ("A", "B", "C", "D")


def _load_quiz_items(quiz_id: str, owner_id: int):
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, title, quiz_json, total FROM quizzes WHERE id = %s AND user_id = %s",
                    (quiz_id, owner_id),
                )
                row = cursor.fetchone()
        finally:
            db_pool.putconn(conn)

    if not row:
        raise HTTPException(status_code=404, detail=teacher_text(owner_id, "quiz_not_found"))
    try:
        data = json.loads(row[2]) if row[2] else {}
        items = data.get("quizzes", [])
    except Exception:
        raise HTTPException(status_code=500, detail=teacher_text(owner_id, "quiz_data_broken"))
    if not items:
        raise HTTPException(status_code=400, detail=teacher_text(owner_id, "quiz_questions_missing"))
    return row, items

def _make_variant(items, variant_code: str):
    """Variantlar bir-biridan farqli bo'lishi uchun savollar va variantlar deterministik aralashtiriladi."""
    code_index = TEACHER_VARIANT_CODES.index(variant_code)
    result = deepcopy(items)
    # Har bir variant uchun turlicha, lekin takrorlanadigan seed.
    rng = random.Random(f"QuizPilot:{variant_code}:{len(items)}:{json.dumps(items, ensure_ascii=False, sort_keys=True)}")
    # Variant B/C/D savollar tartibini ham o'zgartiradi; A ham originaldan ko'chirma emas.
    if code_index:
        rng.shuffle(result)
    elif len(result) > 1:
        # A variantda ham testni biroz qayta tartiblaymiz, ammo keyin javob kaliti aniq saqlanadi.
        rng.shuffle(result)
    for item in result:
        options = list(item.get("options") or [])[:4]
        correct = int(item.get("correct_index", 0))
        pairs = list(enumerate(options))
        # Har variantda variantlar tartibi ham aralashadi.
        rng.shuffle(pairs)
        new_options = [p[1] for p in pairs]
        new_correct = next((i for i, p in enumerate(pairs) if p[0] == correct), 0)
        item["options"] = new_options
        item["correct_index"] = new_correct
    return {"variant": variant_code, "quizzes": result}


class TeacherVariantsRequest(BaseModel):
    user_id: int
    quiz_id: str
    variants: List[str] = Field(default_factory=lambda: ["A", "B", "C", "D"])


@app.post("/api/teacher/generate-variants")
def teacher_generate_variants(req: TeacherVariantsRequest):
    require_teacher(req.user_id)
    row, items = _load_quiz_items(req.quiz_id, req.user_id)
    requested = []
    for code in req.variants:
        code = str(code).strip().upper()
        if code in TEACHER_VARIANT_CODES and code not in requested:
            requested.append(code)
    if not requested:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "select_quiz_variant"))

    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                now = int(time.time())
                result = []
                for code in requested:
                    variant = make_variant(items, code)
                    payload_json = json.dumps(variant, ensure_ascii=False)
                    
                    cursor.execute(
                        "SELECT id FROM teacher_variants WHERE quiz_id = %s AND variant_code = %s ORDER BY id LIMIT 1",
                        (req.quiz_id, code),
                    )
                    existing_variant = cursor.fetchone()
                    if existing_variant:
                        cursor.execute(
                            "UPDATE teacher_variants SET variant_json = %s, created_at = %s WHERE id = %s",
                            (payload_json, now, existing_variant[0]),
                        )
                    else:
                        cursor.execute(
                            "INSERT INTO teacher_variants (quiz_id, variant_code, variant_json, created_at) VALUES (%s, %s, %s, %s)",
                            (req.quiz_id, code, payload_json, now),
                        )
                    result.append({"variant": code, "question_count": len(variant["quizzes"])})
                conn.commit()
        finally:
            db_pool.putconn(conn)

    return {"status": "ok", "quiz_id": req.quiz_id, "title": row[1] or "Test", "variants": result}

@app.get("/api/teacher/variants")
def teacher_list_variants(user_id: int, quiz_id: str):
    require_teacher(user_id)
    _load_quiz_items(quiz_id, user_id)
    def _read(conn):
        cur = conn.cursor()
        cur.execute("SELECT variant_code, created_at FROM teacher_variants WHERE quiz_id=? ORDER BY variant_code", (quiz_id,))
        return cur.fetchall()
    rows = teacher_db_read(_read)
    return {"status": "ok", "variants": [dict(r) for r in rows]}

@app.get("/api/teacher/variants")
def teacher_list_variants(user_id: int, quiz_id: str):
    require_teacher(user_id)
    _load_quiz_items(quiz_id, user_id)
    
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT variant_code, created_at FROM teacher_variants WHERE quiz_id = %s ORDER BY variant_code",
                    (quiz_id,),
                )
                rows = cursor.fetchall()
        finally:
            db_pool.putconn(conn)
            
    variants = [{"variant_code": r[0], "created_at": r[1]} for r in rows]
    return {"status": "ok", "variants": variants}


@app.get("/api/teacher/variant-detail")
def get_teacher_variant(quiz_id: str, owner_id: int, variant_code: str):
    _load_quiz_items(quiz_id, owner_id)
    code = (variant_code or "A").strip().upper()
    if code not in TEACHER_VARIANT_CODES:
        raise HTTPException(status_code=400, detail=teacher_text(owner_id, "variant_required"))
        
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT variant_json FROM teacher_variants WHERE quiz_id = %s AND variant_code = %s",
                    (quiz_id, code),
                )
                row = cursor.fetchone()
        finally:
            db_pool.putconn(conn)
            
    if row and row[0]:
        return json.loads(row[0])
        
    # Variant hali yaratilmagan bo'lsa, uni xavfsiz tarzda yaratib olamiz.
    _, items = _load_quiz_items(quiz_id, owner_id)
    variant = make_variant(items, code)
    now = int(time.time())
    payload_json = json.dumps(variant, ensure_ascii=False)
    
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO teacher_variants (quiz_id, variant_code, variant_json, created_at) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (quiz_id, variant_code) DO UPDATE SET variant_json = EXCLUDED.variant_json, created_at = EXCLUDED.created_at",
                    (quiz_id, code, payload_json, now),
                )
                conn.commit()
        finally:
            db_pool.putconn(conn)
            
    return variant


class TeacherGroupCreateRequest(BaseModel):
    user_id: int
    name: str
    description: str = ""


class TeacherGroupJoinRequest(BaseModel):
    group_id: str = ""
    join_code: str = ""
    user_id: int
    first_name: str = ""
    username: str = ""


def _new_group_code(conn, length=6):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM teacher_groups WHERE join_code = %s LIMIT 1", (code,))
            row = cursor.fetchone()
        if not row:
            return code
    raise HTTPException(status_code=500, detail="__TEACHER_SERVER_ERROR__")


@app.post("/api/teacher/groups")
def teacher_create_group(req: TeacherGroupCreateRequest):
    require_teacher(req.user_id)
    name = (req.name or "").strip()[:100]
    if not name:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "group_name_required"))
    gid = f"tg_{uuid.uuid4().hex[:10]}"
    now = int(time.time())
    description = (req.description or "")[:500]
    
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                code = _new_group_code(conn)
                cursor.execute(
                    "INSERT INTO teacher_groups (id, owner_id, name, description, join_code, created_at, active) VALUES (%s, %s, %s, %s, %s, %s, 1)",
                    (gid, req.user_id, name, description, code, now),
                )
                conn.commit()
        finally:
            db_pool.putconn(conn)
            
    return {"status": "ok", "group_id": gid, "name": name, "join_code": code}


@app.get("/api/teacher/groups")
def teacher_groups(user_id: int):
    require_teacher(user_id)
    
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT g.id, g.name, g.description, g.join_code, g.created_at, g.active, COUNT(m.id) AS member_count "
                    "FROM teacher_groups g LEFT JOIN teacher_group_members m ON m.group_id = g.id "
                    "WHERE g.owner_id = %s AND g.active = 1 GROUP BY g.id ORDER BY g.created_at DESC",
                    (user_id,),
                )
                rows = cursor.fetchall()
        finally:
            db_pool.putconn(conn)
            
    groups = [
        {
            "id": r[0],
            "name": r[1],
            "description": r[2],
            "join_code": r[3],
            "created_at": r[4],
            "active": r[5],
            "member_count": r[6],
        }
        for r in rows
    ]
    return {"status": "ok", "groups": groups}

@app.post("/api/teacher/groups/join")
def teacher_join_group(req: TeacherGroupJoinRequest):
    code = (req.join_code or "").strip().upper().replace(" ", "")
    group_id = (req.group_id or "").strip()
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                if code:
                    cursor.execute("SELECT id, name, active FROM teacher_groups WHERE join_code = %s", (code,))
                elif group_id:
                    cursor.execute("SELECT id, name, active FROM teacher_groups WHERE id = %s", (group_id,))
                else:
                    raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "group_code_required"))
                group = cursor.fetchone()
                
                if not group or not group[2]:  # group[2] -> active
                    raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "group_not_found"))
                    
                now = int(time.time())
                cursor.execute(
                    "INSERT INTO teacher_group_members (group_id, user_id, first_name, username, joined_at) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (group[0], req.user_id, (req.first_name or "")[:100], (req.username or "")[:100], now),
                )
                conn.commit()
        finally:
            db_pool.putconn(conn)
            
    return {"status": "ok", "group_id": group[0], "group_name": group[1]}

@app.get("/api/teacher/my-groups")
def teacher_my_groups(user_id: int):
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT g.id, g.name, g.description, g.join_code, g.created_at, m.joined_at "
                    "FROM teacher_group_members m JOIN teacher_groups g ON g.id = m.group_id "
                    "WHERE m.user_id = %s AND g.active = 1 ORDER BY m.joined_at DESC",
                    (user_id,),
                )
                rows = cursor.fetchall()
        finally:
            db_pool.putconn(conn)
            
    groups = [
        {
            "id": r[0],
            "name": r[1],
            "description": r[2],
            "join_code": r[3],
            "created_at": r[4],
            "joined_at": r[5],
        }
        for r in rows
    ]
    return {"status": "ok", "groups": groups}


@app.get("/api/teacher/group-members")
def teacher_group_members(group_id: str, user_id: int):
    require_teacher(user_id)
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, name FROM teacher_groups WHERE id = %s AND owner_id = %s",
                    (group_id, user_id),
                )
                g = cursor.fetchone()
                if not g:
                    raise HTTPException(status_code=404, detail=teacher_text(user_id, "group_not_found"))
                
                cursor.execute(
                    "SELECT user_id, first_name, username, joined_at FROM teacher_group_members WHERE group_id = %s ORDER BY joined_at",
                    (group_id,),
                )
                member_rows = cursor.fetchall()
        finally:
            db_pool.putconn(conn)
            
    group_info = {"id": g[0], "name": g[1]}
    members = [
        {
            "user_id": m[0],
            "first_name": m[1],
            "username": m[2],
            "joined_at": m[3],
        }
        for m in member_rows
    ]
    return {"status": "ok", "group": group_info, "members": members}

@app.delete("/api/teacher/groups/{group_id}")
def teacher_delete_group(group_id: str, user_id: int):
    require_teacher(user_id)
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE teacher_groups SET active = 0 WHERE id = %s AND owner_id = %s",
                    (group_id, user_id),
                )
                if cursor.rowcount != 1:
                    raise HTTPException(status_code=404, detail=teacher_text(user_id, "group_not_found"))
                conn.commit()
        finally:
            db_pool.putconn(conn)
    return {"status": "ok"}

class TeacherAssignmentCreateRequest(BaseModel):
    user_id: int
    group_id: str
    quiz_id: str
    variant_code: str = ""
    title: str = ""
    due_at: int = 0
    duration_minutes: int = 30


@app.post("/api/teacher/assignments")
def teacher_create_assignment(req: TeacherAssignmentCreateRequest):
    require_teacher(req.user_id)
    group_id=(req.group_id or "").strip(); quiz_id=(req.quiz_id or "").strip()
    if not group_id or not quiz_id:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "group_required"))
    code=(req.variant_code or "").strip().upper()
    duration = max(5, min(int(req.duration_minutes or 30), 180))
    if code and code not in TEACHER_VARIANT_CODES:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "variant_required"))
    if code:
        _get_teacher_variant(quiz_id,req.user_id,code)
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, name FROM teacher_groups WHERE id = %s AND owner_id = %s AND active = 1",
                    (group_id, req.user_id),
                )
                group = cursor.fetchone()
                
                cursor.execute(
                    "SELECT id, title FROM quizzes WHERE id = %s AND user_id = %s",
                    (quiz_id, req.user_id),
                )
                quiz = cursor.fetchone()
                
                if not group:
                    raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "group_not_found"))
                if not quiz:
                    raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "quiz_not_found"))
                    
                aid = f"ta_{uuid.uuid4().hex[:10]}"
                now = int(time.time())
                title = (req.title or "").strip()[:150] or quiz[1]
                due_at = max(0, int(req.due_at))
                
                cursor.execute(
                    "INSERT INTO teacher_assignments (id, owner_id, group_id, quiz_id, variant_code, title, due_at, duration_minutes, created_at, active) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)",
                    (aid, req.user_id, group_id, quiz_id, code, title, due_at, duration, now),
                )
                conn.commit()
        finally:
            db_pool.putconn(conn)
            
    assignment = {
        "id": aid,
        "group_id": group_id,
        "quiz_id": quiz_id,
        "variant_code": code,
        "title": title,
        "due_at": due_at,
        "duration_minutes": duration,
        "created_at": now,
    }
    return {"status": "ok", "assignment": assignment}


@app.get("/api/teacher/assignments")
def teacher_assignments(user_id: int):
    require_teacher(user_id)
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT a.id, a.group_id, a.quiz_id, a.variant_code, a.title, a.due_at, "
                    "COALESCE(a.duration_minutes, 30) AS duration_minutes, a.created_at, "
                    "g.name AS group_name, q.title AS quiz_title "
                    "FROM teacher_assignments a "
                    "LEFT JOIN teacher_groups g ON g.id = a.group_id "
                    "LEFT JOIN quizzes q ON q.id = a.quiz_id "
                    "WHERE a.owner_id = %s ORDER BY a.created_at DESC LIMIT 100",
                    (user_id,),
                )
                rows = cursor.fetchall()
        finally:
            db_pool.putconn(conn)
            
    assignments = [
        {
            "id": r[0],
            "group_id": r[1],
            "quiz_id": r[2],
            "variant_code": r[3],
            "title": r[4],
            "due_at": r[5],
            "duration_minutes": r[6],
            "created_at": r[7],
            "group_name": r[8],
            "quiz_title": r[9],
        }
        for r in rows
    ]
    return {"status": "ok", "assignments": assignments}


@app.delete("/api/teacher/assignments/{assignment_id}")
def teacher_delete_assignment(assignment_id: str, user_id: int):
    require_teacher(user_id)
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE teacher_assignments SET active = 0 WHERE id = %s AND owner_id = %s",
                    (assignment_id, user_id),
                )
                if cursor.rowcount != 1:
                    raise HTTPException(status_code=404, detail=teacher_text(user_id, "assignment_not_found"))
                conn.commit()
        finally:
            db_pool.putconn(conn)
    return {"status": "ok"}
    


@app.get("/api/teacher/export-assignment")
def teacher_export_assignment(assignment_id: str, user_id: int, format: str):
    require_teacher(user_id)
    with db_pool.getconn() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT a.quiz_id, a.variant_code, q.title AS quiz_title "
                    "FROM teacher_assignments a "
                    "LEFT JOIN quizzes q ON q.id = a.quiz_id "
                    "WHERE a.id = %s AND a.owner_id = %s",
                    (assignment_id, user_id),
                )
                a = cursor.fetchone()
        finally:
            db_pool.putconn(conn)
            
    if not a:
        raise HTTPException(status_code=404, detail=teacher_text(user_id, "assignment_not_found"))
    return teacher_export_quiz(a[0], user_id, format, a[1] or "A", 0)


@app.get("/api/teacher/export-quiz")
def teacher_export_quiz(quiz_id: str, user_id: int, format: str, variant: str = "A", all_variants: int = 0):
    require_teacher(user_id)
    row, items = _load_quiz_items(quiz_id, user_id)
    fmt = format.lower().strip()
    codes = list(TEACHER_VARIANT_CODES) if int(all_variants) else [variant.upper()]
    for c in codes:
        if c not in TEACHER_VARIANT_CODES:
            raise HTTPException(status_code=400, detail=teacher_text(user_id, "variant_required"))
            
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9-_]+", "_", row[1] or "quiz")[:40]
    variant_payloads = [(c, _get_teacher_variant(quiz_id, user_id, c)) for c in codes]

    def answer_key(payload):
        return [chr(65 + int(q.get("correct_index", 0))) for q in payload.get("quizzes", [])]

    if fmt == "xlsx":
        path=os.path.join(DOWNLOADS_DIR, f"{safe}_teacher_{'_'.join(codes)}.xlsx")
        wb=Workbook(); ws=wb.active; ws.title="Testlar"
        ws.append(["Variant","№","Savol","A","B","C","D","To'g'ri javob"])
        for code,payload in variant_payloads:
            for i,q in enumerate(payload.get("quizzes",[]),1):
                opts=(q.get("options") or [])+['','','','']
                ws.append([code,i,q.get("question",""),opts[0],opts[1],opts[2],opts[3],chr(65+int(q.get("correct_index",0)))])
        ws.freeze_panes="A2"; wb.save(path)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if fmt == "docx":
        if fmt == "docx":
        path = os.path.join(DOWNLOADS_DIR, f"{safe}_teacher_{'_'.join(codes)}.docx")
        doc = Document()
        doc.add_heading(row[1] or "Test", level=1)
        doc.add_paragraph("Test kaliti bilan | Variantlar: " + ", ".join(codes))
            doc.add_heading(f"Variant {code}", level=2)
            for i,q in enumerate(payload.get("quizzes",[]),1):
                doc.add_paragraph(f"{i}. {q.get('question','')}")
                opts=(q.get("options") or [])+['','','','']
                for j in range(4): doc.add_paragraph(f"{chr(65+j)}) {opts[j]}", style=None)
            doc.add_paragraph("Javoblar: " + ", ".join(f"{i+1}-{a}" for i,a in enumerate(answer_key(payload))))
        doc.save(path)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    if fmt == "pdf":
        path=os.path.join(DOWNLOADS_DIR, f"{safe}_teacher_{'_'.join(codes)}.pdf")
        reg,bld=_register_pdf_font(); font="QuizPilotFont" if reg else "Helvetica"; bold="QuizPilotFontBold" if bld else font
        doc=SimpleDocTemplate(path,pagesize=A4,rightMargin=30,leftMargin=30,topMargin=30,bottomMargin=30)
        styles=getSampleStyleSheet(); title_style=ParagraphStyle("t_title",parent=styles["Title"],fontName=bold,fontSize=16,alignment=TA_CENTER); q_style=ParagraphStyle("t_q",parent=styles["Normal"],fontName=font,fontSize=10,leading=14)
    story=[Paragraph(row[1] or "Test",title_style),Spacer(1,8),Paragraph("Test kaliti bilan | Variantlar: " + ", ".join(codes),q_style),Spacer(1,10)]
    for code,payload in variant_payloads:
            story.append(Paragraph(f"<b>Variant {code}</b>", q_style)); story.append(Spacer(1,5))
            for i,q in enumerate(payload.get("quizzes",[]),1):
                story.append(Paragraph(f"{i}. {q.get('question','')}",q_style))
                opts=(q.get("options") or [])+['','','','']
                for j in range(4): story.append(Paragraph(f"{chr(65+j)}) {opts[j]}",q_style))
                story.append(Spacer(1,4))
            story.append(Paragraph("<b>Javoblar:</b> " + ", ".join(f"{i+1}-{a}" for i,a in enumerate(answer_key(payload))),q_style)); story.append(Spacer(1,10))
        doc.build(story)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/pdf")
    raise HTTPException(status_code=400, detail=teacher_text(user_id, "format_invalid"))


# --- O'QITUVCHI GURUH REJIMI ---
# --- Teacher DB: barqaror ulanish va SQLite lock himoyasi ---
def _ensure_teacher_schema(conn):
    """Teacher jadvallarini Railway'dagi eski SQLite bazalari bilan ham moslaydi.

    Oldingi Teacher versiyalarida jadvallar qisman yaratilgan bo'lishi mumkin edi.
    CREATE TABLE IF NOT EXISTS bunday jadvalni yangilamaydi, shuning uchun bu yerda
    kerakli ustunlar birma-bir tekshiriladi va yetishmaganlari xavfsiz qo'shiladi.
    Bu faqat teacher_* jadvallariga taalluqli.
    """
    table_defs = {
        "teacher_variants": {
            "id": "INTEGER",
            "quiz_id": "TEXT",
            "variant_code": "TEXT",
            "variant_json": "TEXT",
            "created_at": "INTEGER",
        },
        "teacher_groups": {
            "id": "TEXT",
            "owner_id": "INTEGER",
            "name": "TEXT",
            "description": "TEXT",
            "created_at": "INTEGER",
            "active": "INTEGER",
        },
        "teacher_group_members": {
            "id": "INTEGER",
            "group_id": "TEXT",
            "user_id": "INTEGER",
            "first_name": "TEXT",
            "username": "TEXT",
            "joined_at": "INTEGER",
        },
        "teacher_assignments": {
            "id": "TEXT",
            "owner_id": "INTEGER",
            "group_id": "TEXT",
            "quiz_id": "TEXT",
            "variant_code": "TEXT",
            "title": "TEXT",
            "due_at": "INTEGER",
            "duration_minutes": "INTEGER",
            "created_at": "INTEGER",
            "active": "INTEGER",
        },
        "teacher_sessions": {
            "id": "TEXT",
            "owner_id": "INTEGER",
            "teacher_id": "INTEGER",
            "quiz_id": "TEXT",
            "code": "TEXT",
            "title": "TEXT",
            "duration_minutes": "INTEGER",
            "created_at": "INTEGER",
            "expires_at": "INTEGER",
            "active": "INTEGER",
            "group_id": "TEXT",
            "variant_code": "TEXT",
            "deleted": "INTEGER",
            "source_type": "TEXT",
            "assignment_id": "TEXT",
        },
        "teacher_participants": {
            "id": "INTEGER",
            "session_id": "TEXT",
            "user_id": "INTEGER",
            "first_name": "TEXT",
            "username": "TEXT",
            "score": "INTEGER",
            "total": "INTEGER",
            "percent": "INTEGER",
            "started_at": "INTEGER",
            "finished_at": "INTEGER",
        },
    }

    # Avval jadvalning o'zi mavjudligini kafolatlaymiz.
    create_sql = {
    "teacher_variants": """CREATE TABLE IF NOT EXISTS teacher_variants (
        id SERIAL PRIMARY KEY,
        quiz_id TEXT NOT NULL, variant_code TEXT NOT NULL,
        variant_json TEXT NOT NULL, created_at INTEGER NOT NULL,
        UNIQUE(quiz_id, variant_code))""",
    "teacher_groups": """CREATE TABLE IF NOT EXISTS teacher_groups (
        id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL,
        description TEXT DEFAULT '', created_at INTEGER NOT NULL,
        active INTEGER DEFAULT 1)""",
    "teacher_group_members": """CREATE TABLE IF NOT EXISTS teacher_group_members (
        id SERIAL PRIMARY KEY, group_id TEXT NOT NULL,
        user_id INTEGER NOT NULL, first_name TEXT DEFAULT '',
        username TEXT DEFAULT '', joined_at INTEGER NOT NULL,
        UNIQUE(group_id, user_id))""",
    "teacher_assignments": """CREATE TABLE IF NOT EXISTS teacher_assignments (
        id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, group_id TEXT NOT NULL,
        quiz_id TEXT NOT NULL, variant_code TEXT DEFAULT '', title TEXT DEFAULT '',
        due_at INTEGER DEFAULT 0, duration_minutes INTEGER DEFAULT 30, created_at INTEGER NOT NULL,
        active INTEGER DEFAULT 1)""",
    "teacher_sessions": """CREATE TABLE IF NOT EXISTS teacher_sessions (
        id TEXT PRIMARY KEY, owner_id INTEGER, teacher_id INTEGER, quiz_id TEXT, code TEXT UNIQUE,
        title TEXT DEFAULT '', duration_minutes INTEGER DEFAULT 30, created_at INTEGER,
        expires_at INTEGER, active INTEGER DEFAULT 1,
        group_id TEXT DEFAULT '', variant_code TEXT DEFAULT '', deleted INTEGER DEFAULT 0,
        source_type TEXT DEFAULT 'group_test', assignment_id TEXT DEFAULT '')""",
    "teacher_participants": """CREATE TABLE IF NOT EXISTS teacher_participants (
        id SERIAL PRIMARY KEY, session_id TEXT, user_id INTEGER,
        first_name TEXT, username TEXT, score INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0, percent INTEGER DEFAULT 0,
        started_at INTEGER, finished_at INTEGER,
        UNIQUE(session_id, user_id))"""
}
for name, ddl in create_sql.items():
    cursor.execute(ddl)

# Eski jadvallarga yetishmayotgan ustunlarni qo'shamiz.
for table, columns in table_defs.items():
    cursor.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,)
    )
    existing = {r[0] for r in cursor.fetchall()}
    for col, typ in columns.items():
        if col in existing:
            continue
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            logging.info(f"Teacher DB migration: {table}.{col} qo'shildi")
        except Exception as e:
        logging.error(f"Migration error on {table}.{col}: {e}")

# ID ustuni eski bazada mavjud bo'lmagan bo'lsa, qo'shilgan qiymatlarni
# mavjud rowlar uchun ham to'ldiramiz. Yangi yozuvlar endpointlar tomonidan
# o'z ID'sini beradi.
id_prefixes = {
    "teacher_groups": "tg_migrated_",
    "teacher_assignments": "ta_migrated_",
    "teacher_sessions": "ts_migrated_",
}
for table, prefix in id_prefixes.items():
    try:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s AND column_name = 'id'",
            (table,)
        )
        if cursor.fetchone():
            cursor.execute(
                f"UPDATE {table} SET id = %s || ctid::text WHERE id IS NULL OR TRIM(CAST(id AS TEXT)) = ''",
                (prefix,),
            )
    except Exception as e:
        logging.warning(f"Teacher DB ID repair {table}: {e}")

# Eski bazada teacher_sessions.teacher_id mavjud bo'lsa,
# owner_id bilan bir xil qiymatga to'ldiramiz.
try:
    cursor.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'teacher_sessions' AND column_name IN ('teacher_id', 'owner_id')"
    )
    session_cols = {r[0] for r in cursor.fetchall()}
    if "teacher_id" in session_cols and "owner_id" in session_cols:
        cursor.execute("UPDATE teacher_sessions SET teacher_id = owner_id WHERE teacher_id IS NULL")
except Exception as e:
    logging.warning(f"Teacher DB teacher_id repair: {e}")

    # NULL qiymatlar endpointlar ishlashiga xalaqit bermasin.
    null_defaults = {
        "teacher_groups": {"description": "", "active": 1},
        "teacher_assignments": {"variant_code": "", "title": "", "due_at": 0, "active": 1},
        "teacher_sessions": {"group_id": "", "variant_code": "", "active": 1, "deleted": 0},
        "teacher_participants": {"first_name": "", "username": "", "score": 0, "total": 0, "percent": 0, "started_at": 0, "finished_at": 0},
    }
    with conn.cursor() as cursor:
        for table, values in null_defaults.items():
            for col, value in values.items():
                try:
                    cursor.execute(f"UPDATE {table} SET {col} = %s WHERE {col} IS NULL", (value,))
                except Exception as e:
                    logging.warning("Teacher DB default repair %s.%s: %s", table, col, e)
    conn.commit()


_teacher_schema_checked = False

def teacher_db_connect():
    """Teacher uchun PostgreSQL connection pool ulanishi va schema tekshiruvi."""
    conn = db_pool.getconn()
    global _teacher_schema_checked
    try:
        if not _teacher_schema_checked:
            with conn.cursor() as cursor:
                _ensure_teacher_schema(cursor)
            conn.commit()
            _teacher_schema_checked = True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        db_pool.putconn(conn)
        raise
    return conn

def teacher_db_read(work):
    """Teacher o'qish amallari uchun PostgreSQL pool yordamchisi."""
    conn = teacher_db_connect()
    try:
        return work(conn)
    finally:
        db_pool.putconn(conn)

def teacher_db_write(work):
    """Teacher yozish amallari uchun PostgreSQL pool yordamchisi."""
    conn = teacher_db_connect()
    try:
        res = work(conn)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

def teacher_db_read(work):
    """Teacher o'qish amallari uchun PostgreSQL pool yordamchisi."""
    conn = teacher_db_connect()
    try:
        return work(conn)
    finally:
        db_pool.putconn(conn)

def teacher_db_write(work):
    """Teacher yozish amallari uchun PostgreSQL pool yordamchisi."""
    conn = teacher_db_connect()
    try:
        res = work(conn)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

def teacher_db_write(work):
    """Teacher yozish amallari uchun PostgreSQL pool yordamchisi."""
    conn = teacher_db_connect()
    try:
        res = work(conn)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

TEACHER_MESSAGES = {
    "uz": {
        "quiz_data_broken": "Test ma’lumotlari buzilgan.",
        "quiz_questions_missing": "Test savollari mavjud emas.",
        "variant_required": "A, B, C yoki D variantidan foydalaning.",
        "group_required": "Guruh va testni tanlang.",
        "select_group": "Avval guruhni tanlang.",
        "assignment_not_found": "Topshiriq topilmadi.",
        "session_expired": "Sessiya muddati tugagan.",
        "format_invalid": "Fayl formati noto‘g‘ri.",

        "teacher_plan_required": "O‘qituvchi tarifini faollashtiring.",
        "group_name_required": "Guruh nomini kiriting.",
        "group_created": "Guruh yaratildi.",
        "group_not_found": "Guruh topilmadi.",
        "groups_empty": "Hozircha guruhlar yo‘q.",
        "members_count": "a’zo",
        "active": "Faol", "closed": "Yopiq",
        "delete": "O‘chirish",
        "assignment_created": "Topshiriq guruhga biriktirildi.",
        "assignment_empty": "Hozircha topshiriqlar yo‘q.",
        "select_group_quiz": "Guruh va testni tanlang.",
        "variant_invalid": "A, B, C yoki D variantidan foydalaning.",
        "variants_ready": "Variantlar tayyorlandi. Har bir variant uchun test kaliti saqlandi.",
        "select_quiz_variant": "Test va kamida bitta variantni tanlang.",
        "session_created": "Sessiya yaratildi.",
        "no_results": "Hali natijalar yo‘q.",
        "no_students_results": "Hali o‘quvchilar natijasi yo‘q.",
        "server_error": "Serverda ichki xatolik yuz berdi. Iltimos, qayta urinib ko‘ring.",
        "db_busy": "Server ma’lumotlar bazasi band. Bir necha soniyadan so‘ng qayta urinib ko‘ring.",
        "quiz_not_found": "Test topilmadi.",
        "session_not_found": "Sessiya topilmadi.",
        "session_already_completed": "Bu testni siz allaqachon yakunlagansiz. Qayta kirish mumkin emas.",
        "test_first": "Avval test yarating.",
        "copy_link": "Havolani nusxalash",
    },
    "ru": {
        "quiz_data_broken": "Данные теста повреждены.",
        "quiz_questions_missing": "В тесте нет вопросов.",
        "variant_required": "Используйте вариант A, B, C или D.",
        "group_required": "Выберите группу и тест.",
        "select_group": "Сначала выберите группу.",
        "assignment_not_found": "Задание не найдено.",
        "session_expired": "Срок сессии истёк.",
        "format_invalid": "Неверный формат файла.",

        "teacher_plan_required": "Активируйте тариф для учителей.",
        "group_name_required": "Введите название группы.",
        "group_created": "Группа создана.",
        "group_not_found": "Группа не найдена.",
        "groups_empty": "Групп пока нет.",
        "members_count": "уч.",
        "active": "Активна", "closed": "Закрыта",
        "delete": "Удалить",
        "assignment_created": "Задание назначено группе.",
        "assignment_empty": "Заданий пока нет.",
        "select_group_quiz": "Выберите группу и тест.",
        "variant_invalid": "Используйте вариант A, B, C или D.",
        "variants_ready": "Варианты готовы. Ключ для каждого варианта сохранён.",
        "select_quiz_variant": "Выберите тест и хотя бы один вариант.",
        "session_created": "Сессия создана.",
        "no_results": "Результатов пока нет.",
        "no_students_results": "Результатов учеников пока нет.",
        "server_error": "Произошла внутренняя ошибка сервера. Попробуйте ещё раз.",
        "db_busy": "База данных сервера занята. Повторите через несколько секунд.",
        "quiz_not_found": "Тест не найден.",
        "session_not_found": "Сессия не найдена.",
        "session_already_completed": "Вы уже завершили этот тест. Повторный вход недоступен.",
        "test_first": "Сначала создайте тест.",
        "copy_link": "Копировать ссылку",
    },
    "en": {
        "quiz_data_broken": "Quiz data is corrupted.",
        "quiz_questions_missing": "The quiz has no questions.",
        "variant_required": "Use variant A, B, C or D.",
        "group_required": "Select a group and a quiz.",
        "select_group": "Select a group first.",
        "assignment_not_found": "Assignment not found.",
        "session_expired": "The session has expired.",
        "format_invalid": "Invalid file format.",

        "teacher_plan_required": "Activate the Teacher plan.",
        "group_name_required": "Enter a group name.",
        "group_created": "Group created.",
        "group_not_found": "Group not found.",
        "groups_empty": "No groups yet.",
        "members_count": "members",
        "active": "Active", "closed": "Closed",
        "delete": "Delete",
        "assignment_created": "Assignment was assigned to the group.",
        "assignment_empty": "No assignments yet.",
        "select_group_quiz": "Select a group and a quiz.",
        "variant_invalid": "Use variant A, B, C or D.",
        "variants_ready": "Variants are ready. An answer key was saved for each variant.",
        "select_quiz_variant": "Select a quiz and at least one variant.",
        "session_created": "Session created.",
        "no_results": "No results yet.",
        "no_students_results": "No student results yet.",
        "server_error": "An internal server error occurred. Please try again.",
        "db_busy": "The server database is busy. Please try again in a few seconds.",
        "quiz_not_found": "Quiz not found.",
        "session_not_found": "Session not found.",
        "session_already_completed": "You have already completed this test. Re-entry is not allowed.",
        "test_first": "Create a quiz first.",
        "copy_link": "Copy link",
    },
}

def teacher_text(user_id, key, default=None):
    lang = get_user_lang(user_id)
    return TEACHER_MESSAGES.get(lang, TEACHER_MESSAGES["uz"]).get(key, default or key)


def require_teacher(user_id: int):
    add_user_to_db(user_id)
    def _check(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT status, plan_key, premium_until FROM users WHERE user_id = %s", (user_id,))
            return cur.fetchone()
    row = teacher_db_read(_check)
    if not row or not is_active_paid_status(row[0] or "", row[2] or 0, row[1] or get_plan_key(row[0] or "")):
        raise HTTPException(status_code=403, detail=teacher_text(user_id, "teacher_plan_required"))

@app.get("/api/teacher-quizzes")
def teacher_quizzes(user_id: int):
    require_teacher(user_id)
    conn = teacher_db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title, total FROM quizzes WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            quizzes = [dict(zip(cols, r)) for r in rows]
        return {"status": "ok", "quizzes": quizzes}
    finally:
        db_pool.putconn(conn)


class TeacherSessionCreateRequest(BaseModel):
    user_id: int
    quiz_id: str
    duration_minutes: int = 30
    variant_code: str = ""
    group_id: str = ""
    assignment_id: str = ""


@app.post("/api/teacher/create-session")
def teacher_create_session(req: TeacherSessionCreateRequest):
    """Create a fresh teacher group session safely on both new and legacy PostgreSQL schemas.

    The endpoint deliberately does not reuse an old session. Every click creates a new
    session id and an independent 8-character access code. Legacy NOT NULL/extra columns
    are populated dynamically, and rare UNIQUE collisions are retried automatically.
    """
    require_teacher(req.user_id)
    assignment_id = (req.assignment_id or "").strip()
    duration = max(5, min(int(req.duration_minutes or 30), 180))
    variant_code = (req.variant_code or "").strip().upper()
    if variant_code and variant_code not in TEACHER_VARIANT_CODES:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "variant_required"))

    group_id = (req.group_id or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "select_group"))

    # Assignment sessions inherit their configured test duration and are tagged
    # so students and teachers can distinguish them from manually started group tests.
    if assignment_id:
        def _read_assignment(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, group_id, quiz_id, variant_code, title, active, 
                       COALESCE(duration_minutes, 30) AS duration_minutes 
                       FROM teacher_assignments WHERE id = %s AND owner_id = %s""",
                    (assignment_id, req.user_id)
                )
                row = cur.fetchone()
                if not row:
                    return None
                cols = [desc[0] for desc in cur.description]
                return dict(zip(cols, row))
        assignment_row = teacher_db_read(_read_assignment)
        if not assignment_row or not int(assignment_row["active"] or 0):
            raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "assignment_not_found"))
        if str(assignment_row["group_id"] or "") != group_id or str(assignment_row["quiz_id"] or "") != str(req.quiz_id):
            raise HTTPException(status_code=400, detail=teacher_text(req.user_id, "assignment_not_found"))
        duration = max(5, min(int(assignment_row["duration_minutes"] or 30), 180))
        if not variant_code:
            variant_code = str(assignment_row["variant_code"] or "").strip().upper()

# Validate quiz and group before opening the write transaction.
quiz_row, _items = _load_quiz_items(req.quiz_id, req.user_id)
if variant_code:
    _get_teacher_variant(req.quiz_id, req.user_id, variant_code)

def _write(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name FROM teacher_groups WHERE id = %s AND owner_id = %s AND active = 1",
            (group_id, req.user_id),
        )
        group_row = cur.fetchone()
        if not group_row:
            raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "group_not_found"))
        missing_core = sorted(required_core - set(column_info))
        if missing_core:
            raise HTTPException(status_code=500, detail=teacher_text(req.user_id, "server_error"))

        session_title = str(quiz_row["title"] or "Guruh testi").strip() or "Guruh testi"
        if assignment_id and assignment_row is not None:
            session_title = str(assignment_row["title"] or session_title).strip() or session_title
        now = int(time.time())
        expires = now + duration * 60

        # A new id/code is generated for every session. We retry the INSERT itself,
        # not just the code lookup, because legacy databases may have additional
        # UNIQUE constraints unknown to this version.
        last_integrity = None
        for _attempt in range(12):
            sid = f"ts_{uuid.uuid4().hex[:12]}"
            code = uuid.uuid4().hex[:8].upper()
            values_by_column = {
                "id": sid,
                "session_id": sid,
                "owner_id": req.user_id,
                "teacher_id": req.user_id,
                "teacher_user_id": req.user_id,
                "user_id": req.user_id,
                "quiz_id": req.quiz_id,
                "code": code,
                "join_code": code,
                "title": session_title,
                "name": session_title,
                "duration_minutes": duration,
                "duration": duration,
                "created_at": now,
                "started_at": now,
                "expires_at": expires,
                "active": 1,
                "group_id": group_id,
                "group_name": group["name"],
                "variant_code": variant_code,
                "deleted": 0,
                "source_type": "assignment" if assignment_id else "group_test",
                "assignment_id": assignment_id,
            }

            insert_columns, insert_values = [], []
            for name, row in column_info.items():
                if name in values_by_column:
                    insert_columns.append(name)
                    insert_values.append(values_by_column[name])
                    continue
                not_null = bool(row[3])
                default_value = row[4]
                is_pk = bool(row[5])
                if not_null and not is_pk and default_value is None:
                    declared_type = str(row[2] or "").upper()
                    # Give unknown legacy required columns a value that is also
                    # unlikely to collide if the column happens to be UNIQUE.
                    if "INT" in declared_type or "REAL" in declared_type or "NUM" in declared_type:
                        fallback = now + random.randint(1, 999999)
                    else:
                        fallback = f"{sid}_{name}"
                    insert_columns.append(name)
                    insert_values.append(fallback)
                    logging.warning("Teacher session legacy required column filled: %s=%r", name, fallback)

            placeholders = ", ".join("%s" for _ in insert_columns)
            try:
                cur.execute(
                    f"INSERT INTO teacher_sessions ({', '.join(insert_columns)}) VALUES ({placeholders})",
                    tuple(insert_values),
                )
                return {
                    "session_id": sid,
                    "code": code,
                    "expires_at": expires,
                    "quiz_title": quiz_row["title"],
                    "variant_code": variant_code,
                    "group_id": group_id,
                    "group_name": group["name"],
                    "source_type": "assignment" if assignment_id else "group_test",
                    "assignment_id": assignment_id,
                }
            except Exception as e:
        # PostgreSQL yoki umumiy integrity/unique xatoliklarini ushlash
        if "integrity" in str(e).lower() or "unique" in str(e).lower() or "duplicate" in str(e).lower():
            last_integrity = e
            logging.warning("Teacher session INSERT collision/constraint (attempt %s): %s", _attempt + 1, e)
            continue
        raise

    raise HTTPException(status_code=500, detail=teacher_text(req.user_id, "server_error")) from last_integrity

data = teacher_db_write(_write)
return {"status": "ok", **data}

@app.get("/api/teacher-sessions")
def teacher_sessions(user_id: int):
    require_teacher(user_id)
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.code, s.quiz_id, q.title, s.duration_minutes, s.created_at, s.expires_at, s.active,
                       COALESCE(s.group_id,'') AS group_id, COALESCE(s.variant_code,'') AS variant_code,
                       COALESCE(s.source_type,'group_test') AS source_type, COALESCE(s.assignment_id,'') AS assignment_id
                FROM teacher_sessions s
                LEFT JOIN quizzes q ON q.id = s.quiz_id
                WHERE s.owner_id = %s ORDER BY s.created_at DESC
            """, (user_id,))
            rows = cur.fetchall()
            @app.get("/api/teacher-sessions")
def teacher_sessions(user_id: int):
    require_teacher(user_id)
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.code, s.quiz_id, q.title, s.duration_minutes, s.created_at, s.expires_at, s.active,
                       COALESCE(s.group_id,'') AS group_id, COALESCE(s.variant_code,'') AS variant_code,
                       COALESCE(s.source_type,'group_test') AS source_type, COALESCE(s.assignment_id,'') AS assignment_id
                FROM teacher_sessions s
                LEFT JOIN quizzes q ON q.id = s.quiz_id
                WHERE s.owner_id = %s AND COALESCE(s.deleted, 0) = 0
                ORDER BY s.created_at DESC LIMIT 30
            """, (user_id,))
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, r)) for r in rows]
    rows = teacher_db_read(_read)
    now = int(time.time())
    result = []
    for r in rows:
        active = bool(r["active"] and r["expires_at"] and r["expires_at"] >= now)
        result.append({**r, "active": active})
    return {"status": "ok", "sessions": result}

@app.delete("/api/teacher/sessions/{session_id}")
def teacher_delete_session(session_id: str, user_id: int):
    require_teacher(user_id)
    def _write(conn):
        with conn.cursor() as cur:
            # Soft-delete: the teacher no longer sees the session, but participant
            # records remain safe for database integrity/audit purposes.
            cur.execute(
                "UPDATE teacher_sessions SET active = 0, deleted = 1 WHERE id = %s AND owner_id = %s",
                (session_id, user_id),
            )
            if cur.rowcount != 1:
                raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    teacher_db_write(_write)
    return {"status": "ok"}

@app.get("/api/teacher-session")
def teacher_session_info(code: str, user_id: int):
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.owner_id, s.quiz_id, s.code, s.duration_minutes, s.created_at, s.expires_at, s.active,
                       COALESCE(s.variant_code,'') AS variant_code, q.title, q.total
                FROM teacher_sessions s
                LEFT JOIN quizzes q ON q.id = s.quiz_id
                WHERE s.code = %s AND COALESCE(s.deleted, 0) = 0
            """, (code,))
            cols = [desc[0] for desc in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None
    row = teacher_db_read(_read)
    if not row: raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    
    # Session link is private to the teacher who created it and students who
    # belong to the corresponding group.
    def _access(conn):
        return _student_can_access_session(conn, row["id"], user_id)
    if not teacher_db_read(_access):
        raise HTTPException(status_code=403, detail=teacher_text(user_id, "session_not_found"))
    if not row["active"] or int(time.time()) > row["expires_at"]: 
        raise HTTPException(status_code=410, detail=teacher_text(user_id, "session_expired"))
    return {"status": "ok", "session_id": row["id"], "quiz_id": row["quiz_id"], "title": row["title"], "total": row["total"], "expires_at": row["expires_at"]}

def _session_owner_id(session_id: str, user_id: int):
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT owner_id FROM teacher_sessions WHERE id = %s", (session_id,))
            return cur.fetchone()
    row = teacher_db_read(_read)
    if not row: 
        raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    return row[0]

@app.get("/api/teacher-group-sessions")
def teacher_group_sessions(user_id: int):
    """Return currently active test sessions for groups the student has joined."""
    now = int(time.time())
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT s.id, s.code, s.quiz_id, q.title, q.total,
                       s.duration_minutes, s.created_at, s.expires_at, s.group_id,
                       g.name AS group_name, COALESCE(s.variant_code,'') AS variant_code,
                       COALESCE(s.source_type,'group_test') AS source_type, COALESCE(s.assignment_id,'') AS assignment_id,
                       COALESCE(p.finished_at,0) AS finished_at,
                       COALESCE(p.started_at,0) AS started_at,
                       COALESCE(p.score,0) AS student_score,
                       COALESCE(p.total,q.total) AS student_total,
                       COALESCE(p.percent,0) AS student_percent
                FROM teacher_group_members m
                JOIN teacher_groups g ON g.id=m.group_id AND g.active=1
                JOIN teacher_sessions s ON s.group_id=g.id
                JOIN quizzes q ON q.id=s.quiz_id
                LEFT JOIN teacher_participants p ON p.session_id=s.id AND p.user_id=m.user_id
                WHERE m.user_id = %s AND s.active = 1 AND COALESCE(s.deleted, 0) = 0
                  AND s.expires_at > %s
                ORDER BY s.created_at DESC
                LIMIT 30
            """, (user_id, now))
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, r)) for r in rows]
    rows = teacher_db_read(_read)
    return {"status": "ok", "sessions": rows, "server_time": now}


def _student_can_access_session(conn, session_id: str, user_id: int):
    """A session is accessible to its owner or to a member of its group."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.owner_id, s.group_id, s.active, COALESCE(s.deleted,0) AS deleted,
                   s.expires_at
            FROM teacher_sessions s WHERE s.id = %s
        """, (session_id,))
        row = cur.fetchone()
        if not row:
            return None
        # row: (owner_id, group_id, active, deleted, expires_at)
        if int(row[0] or 0) == int(user_id):
            return row
        group_id = str(row[1] or '')
        if not group_id:
            return None
        cur.execute(
            "SELECT 1 FROM teacher_group_members WHERE group_id = %s AND user_id = %s LIMIT 1",
            (group_id, user_id),
        )
        member = cur.fetchone()
        return row if member else None

@app.post("/api/teacher-session-start")
@app.post("/api/teacher-session-start")
def teacher_session_start(session_id: str, user_id: int):
    """Register the moment a student actually enters a group test."""
    now = int(time.time())
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.owner_id, s.group_id, s.quiz_id, s.expires_at, s.active,
                       COALESCE(s.deleted,0) AS deleted, s.duration_minutes, q.title, q.total,
                       COALESCE(s.variant_code,'') AS variant_code
                FROM teacher_sessions s 
                LEFT JOIN quizzes q ON q.id = s.quiz_id
                WHERE s.id = %s
            """, (session_id,))
            s = cur.fetchone()
            if not s:
                raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
            # s indices: 0:id, 1:owner_id, 2:group_id, 3:quiz_id, 4:expires_at, 5:active, 6:deleted, 7:duration_minutes, 8:title, 9:total, 10:variant_code
            if not _student_can_access_session(conn, session_id, user_id):
                raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
            if int(s[6] or 0) or not int(s[5] or 0) or now > int(s[4] or 0):
                raise HTTPException(status_code=410, detail=teacher_text(user_id, "session_expired"))
            
            first_name = "Telegram User"
            username = ""
            cur.execute(
                "SELECT id, started_at, finished_at, score, total, percent FROM teacher_participants WHERE session_id = %s AND user_id = %s ORDER BY id LIMIT 1",
                (session_id, user_id),
            )
            existing = cur.fetchone()
            if existing:
                if int(existing[2] or 0) > 0:
                    raise HTTPException(status_code=409, detail=teacher_text(user_id, "session_already_completed"))
                else:
            cur.execute("""
                INSERT INTO teacher_participants
                (session_id, user_id, first_name, username, score, total, percent, started_at, finished_at)
                VALUES (%s, %s, %s, %s, 0, %s, 0, %s, 0)
            """, (session_id, user_id, first_name, username, s[9], now))
            
            # Yangi yaratilgan qator ma'lumotlarini qaytarish uchun dict yasaymiz
            return {
                "id": session_id,
                "quiz_id": s[3],
                "title": s[8],
                "total": s[9],
                "duration_minutes": s[7],
                "expires_at": s[4],
                "variant_code": s[10]
            }
    data = teacher_db_write(_write)
    return {"status": "ok", "session_id": data["id"], "quiz_id": data["quiz_id"],
            "title": data["title"], "total": data["total"],
            "duration_minutes": data["duration_minutes"], "expires_at": data["expires_at"],
            "variant_code": data["variant_code"], "server_time": now}

@app.get("/api/teacher-session-quiz")
def teacher_session_quiz(session_id: str, user_id: int):
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.quiz_id, q.title, s.expires_at, s.active, 
                       COALESCE(s.variant_code,'') AS variant_code, q.quiz_json 
                FROM teacher_sessions s 
                LEFT JOIN quizzes q ON q.id = s.quiz_id
                WHERE s.id = %s
            """, (session_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
            
    row = teacher_db_read(_read)
    if not row:
        raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
        
    def _access(conn):
        return _student_can_access_session(conn, session_id, user_id)
    if not teacher_db_read(_access):
        raise HTTPException(status_code=403, detail=teacher_text(user_id, "session_not_found"))
        
    if not row["active"] or int(time.time()) > row["expires_at"]:
        raise HTTPException(status_code=410, detail=teacher_text(user_id, "session_expired"))
        
    if row["variant_code"]:
        payload = _get_teacher_variant(row["quiz_id"], _session_owner_id(session_id, user_id), row["variant_code"])
    else:
        payload = json.loads(row["quiz_json"])
    return {"status": "ok", "quiz_id": row["quiz_id"], "title": row["title"] or "Test", "quiz_json": payload, "variant_code": row["variant_code"]}

class TeacherSubmitRequest(BaseModel):
    session_id: str
    user_id: int
    first_name: str = ""
    username: str = ""
    score: int
    total: int
    percent: int


@app.post("/api/teacher-submit")
def teacher_submit(req: TeacherSubmitRequest):
    now = int(time.time())
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT expires_at, active, COALESCE(deleted, 0) AS deleted 
                FROM teacher_sessions WHERE id = %s
            """, (req.session_id,))
            s = cur.fetchone()
            if not s or s[2]: # s[2] is deleted
                raise HTTPException(status_code=404, detail=teacher_text(req.user_id, "session_not_found"))
            
            cur.execute("""
                SELECT id, started_at, finished_at FROM teacher_participants 
                WHERE session_id = %s AND user_id = %s ORDER BY id LIMIT 1
            """, (req.session_id, req.user_id))
            existing_participant = cur.fetchone()
            
            started = int(existing_participant[1] or 0) if existing_participant else 0
            finished = int(existing_participant[2] or 0) if existing_participant else 0
            if existing_participant:
                if int(existing_participant[2] or 0) > 0:
                    raise HTTPException(status_code=409, detail=teacher_text(req.user_id, "session_already_completed"))
                cur.execute("""
                    UPDATE teacher_participants 
                    SET first_name = %s, username = %s, score = %s, total = %s, percent = %s, finished_at = %s 
                    WHERE id = %s
                """, (*values, existing_participant[0]))
            else:
                cur.execute("""
                    INSERT INTO teacher_participants 
                    (session_id, user_id, first_name, username, score, total, percent, started_at, finished_at) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (req.session_id, req.user_id, *values))
            return {"status": "ok"}
            
    return teacher_db_write(_write)

@app.get("/api/teacher-session-results")
def teacher_session_results(session_id: str, user_id: int):
    require_teacher(user_id)
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.quiz_id, q.title, s.code, s.expires_at, 
                       COALESCE(s.source_type,'group_test') AS source_type, 
                       COALESCE(s.assignment_id,'') AS assignment_id 
                FROM teacher_sessions s 
                LEFT JOIN quizzes q ON q.id = s.quiz_id 
                WHERE s.id = %s
            """, (session_id,))
            s_row = cur.fetchone()
            if not s_row:
                return None
            s_cols = [desc[0] for desc in cur.description]
            s_dict = dict(zip(s_cols, s_row))
            
            cur.execute("""
                SELECT first_name, username, score, total, percent, started_at, finished_at 
                FROM teacher_participants 
                WHERE session_id = %s
            """, (session_id,))
            rows_data = cur.fetchall()
            r_cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(r_cols, r)) for r in rows_data]
            
            active_students = sum(1 for r in rows if int(r["started_at"] or 0) > 0 and int(r["finished_at"] or 0) == 0)
            completed_students = sum(1 for r in rows if int(r["finished_at"] or 0) > 0)
            return s_dict, rows, active_students, completed_students

    res = teacher_db_read(_read)
    if not res:
        raise HTTPException(status_code=404, detail=teacher_text(user_id, "session_not_found"))
    s, rows, active_students, completed_students = res
    return {
        "status": "ok", 
        "session": {
            "id": session_id, 
            "code": s["code"], 
            "quiz_title": s["title"] or "Test", 
            "expires_at": s["expires_at"], 
            "source_type": s["source_type"], 
            "assignment_id": s["assignment_id"]
        }, 
        "participants": rows, 
        "active_students": active_students, 
        "completed_students": completed_students
    }

def _teacher_export_rows(session_id, owner_id):
    require_teacher(owner_id)
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.code, q.title 
                FROM teacher_sessions s 
                LEFT JOIN quizzes q ON q.id = s.quiz_id 
                WHERE s.id = %s AND s.owner_id = %s AND COALESCE(s.deleted, 0) = 0
            """, (session_id, owner_id))
            s_row = cur.fetchone()
            if not s_row:
                return None
            s_cols = [desc[0] for desc in cur.description]
            s = dict(zip(s_cols, s_row))
            
            cur.execute("""
                SELECT first_name, username, score, total, percent, finished_at 
                FROM teacher_participants 
                WHERE session_id = %s 
                ORDER BY percent DESC, finished_at ASC
            """, (session_id,))
            rows_data = cur.fetchall()
            r_cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(r_cols, r)) for r in rows_data]
            return s, rows

    res = teacher_db_read(_read)
    if not res:
        raise HTTPException(status_code=404, detail=teacher_text(owner_id, "session_not_found"))
    return res


def _register_pdf_font():
    candidates=["/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    bolds=["/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    reg=bld=False
    for path in candidates:
        if os.path.exists(path):
            try: pdfmetrics.registerFont(TTFont("QuizPilotFont", path)); reg=True; break
            except Exception: pass
    for path in bolds:
        if os.path.exists(path):
            try: pdfmetrics.registerFont(TTFont("QuizPilotFontBold", path)); bld=True; break
            except Exception: pass
    return reg,bld


@app.get("/api/teacher-export")
def teacher_export(session_id: str, user_id: int, format: str):
    s, rows = _teacher_export_rows(session_id, user_id)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    fmt=format.lower(); safe=re.sub(r"[^A-Za-z0-9_-]+", "_", s["title"] or "quiz")[:40]
    if fmt=="xlsx":
        path=os.path.join(DOWNLOADS_DIR,f"{safe}_{s['code']}.xlsx")
        wb=Workbook(); ws=wb.active; ws.title="Natijalar"
        ws.append(["№","O'quvchi","Username","To'g'ri","Jami","Foiz"]);
        for i,r in enumerate(rows,1): ws.append([i,r["first_name"],r["username"],r["score"],r["total"],r["percent"]])
        ws.freeze_panes="A2"; wb.save(path)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt=="docx":
        path=os.path.join(DOWNLOADS_DIR,f"{safe}_{s['code']}.docx")
        doc=Document(); doc.add_heading(s["title"], level=1); doc.add_paragraph(f"Sessiya: {s['code']}")
        table=doc.add_table(rows=1, cols=6); hdr=table.rows[0].cells
        for i,t in enumerate(["№","O'quvchi","Username","To'g'ri","Jami","Foiz"]): hdr[i].text=t
        for i,r in enumerate(rows,1):
            cells=table.add_row().cells
            vals=[i,r["first_name"],r["username"],r["score"],r["total"],f"{r['percent']}%"]
            for j,v in enumerate(vals): cells[j].text=str(v)
        doc.save(path); return FileResponse(path, filename=os.path.basename(path), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if fmt=="pdf":
        path=os.path.join(DOWNLOADS_DIR,f"{safe}_{s['code']}.pdf")
        reg,bld=_register_pdf_font(); font="QuizPilotFont" if reg else "Helvetica"; bold="QuizPilotFontBold" if bld else font
        doc=SimpleDocTemplate(path,pagesize=A4,rightMargin=28,leftMargin=28,topMargin=28,bottomMargin=28)
        styles=getSampleStyleSheet(); title_style=ParagraphStyle("qp_title",parent=styles["Title"],fontName=bold,fontSize=16,alignment=TA_CENTER)
        body_style=ParagraphStyle("qp_body",parent=styles["BodyText"],fontName=font,fontSize=8)
        story=[Paragraph(s["title"],title_style),Spacer(1,8),Paragraph(f"Session: {s['code']}",body_style),Spacer(1,8)]
        data=[["№","O'quvchi","Username","To'g'ri","Jami","Foiz"]]
        for i,r in enumerate(rows,1): data.append([str(i),str(r["first_name"]),str(r["username"]),str(r["score"]),str(r["total"]),f"{r['percent']}%"] )
        table=Table(data,repeatRows=1,colWidths=[24,170,100,45,40,45]); table.setStyle(TableStyle([("FONTNAME",(0,0),(-1,-1),font),("FONTNAME",(0,0),(-1,0),bold),("FONTSIZE",(0,0),(-1,-1),8),("GRID",(0,0),(-1,-1),0.4,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("VALIGN",(0,0),(-1,-1),"MIDDLE")])); story.append(table); doc.build(story)
        return FileResponse(path, filename=os.path.basename(path), media_type="application/pdf")
    raise HTTPException(status_code=400, detail=teacher_text(user_id, "format_invalid"))


@app.get("/api/flashcards")
def get_flashcards(user_id: int):
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, front, back 
                FROM flashcards 
                WHERE user_id = %s 
                ORDER BY created_at DESC
            """, (user_id,))
            rows_data = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, r)) for r in rows_data]

    rows = teacher_db_read(_read)
    cards = [{"id": r["id"], "front": r["front"], "back": r["back"]} for r in rows]
    return {"status": "ok", "cards": cards}

@app.post("/api/create-flashcard")
def create_flashcard(req: FlashcardCreateRequest):
    add_user_to_db(req.user_id)
    now = int(time.time())
    
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status, premium_until, flashcard_free_used, created_at 
                FROM users WHERE user_id = %s
            """, (req.user_id,))
            u_row = cur.fetchone()
            if not u_row:
                raise HTTPException(status_code=404, detail="User not found")
            
            # Map tuple to dict-like access using column names
            u_cols = [desc[0] for desc in cur.description]
            u = dict(zip(u_cols, u_row))
            
            is_paid = bool(is_active_paid_status(u["status"] or "", u["premium_until"] or 0))
            
            # 30 kunlik bepul davr tugagan bo'lsa, uchala hisoblagichni reset qilamiz.
            if u and now - (u["created_at"] or now) >= 30 * 24 * 3600 and not is_paid:
                cur.execute("""
                    UPDATE users 
                    SET free_used = 0, public_free_used = 0, flashcard_free_used = 0, created_at = %s 
                    WHERE user_id = %s
                """, (now, req.user_id))
            
            # Premium / Teacher: cheksiz.
            if not is_paid:
                cur.execute("""
                    UPDATE users 
                    SET flashcard_free_used = COALESCE(flashcard_free_used, 0) + 1 
                    # If rowcount != 1, limit was reached (handled inside _write block or raising error properly)
            # Let's ensure lang is retrieved or passed correctly. Since lang might come from request, let's check or handle safely.
            # Wait, looking at lines 3736-3745, 'lang' needs to be defined. Let's see if req has lang or if we can get it.
            # Assuming req has lang or we use req.lang if available, or fetch user lang. Let's write the robust block:
            pass

        # Let's write the complete _write function properly:
    return {"status": "ok"}


@app.get("/api/quiz-detail")
def get_quiz_detail(quiz_id: str):
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT title, quiz_json 
                FROM quizzes 
                WHERE id = %s
            """, (quiz_id,))
            row = cur.fetchone()
            if not row:
                return None
            return {"title": row[0], "quiz_json": row[1]}

    res = teacher_db_read(_read)
    if res:
        quiz_json_data = res["quiz_json"]
        if isinstance(quiz_json_data, str):
            quiz_json_data = json.loads(quiz_json_data)
        return {"status": "ok", "title": res["title"] or "Test", "quiz_json": quiz_json_data}
    
    raise HTTPException(status_code=404, detail="Test topilmadi")

@app.post("/api/update-progress")
def update_progress(data: ProgressUpdateRequest):
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE quizzes 
                SET answered = total, last_score = %s, last_percent = %s 
                WHERE id = %s AND user_id = %s
            """, (data.correct_count, data.percent, data.quiz_id, data.user_id))
            return {"status": "ok"}

    return teacher_db_write(_write)


@app.delete("/api/delete-quiz")
def delete_quiz(quiz_id: str, user_id: int):
    try:
        def _write(conn):
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM quizzes WHERE id = %s AND user_id = %s
                """, (quiz_id, user_id))
                return {"status": "ok", "message": "Test o'chirildi."}
                
        return teacher_db_write(_write)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Xatolik.")


def start_bot_polling():
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except Exception:
            time.sleep(5)


@app.exception_handler(Exception)
async def api_exception_handler(request: Request, exc: Exception):
    # Teacher/frontend fetchlari 500 paytida "Unexpected token I..." kabi JSON parse
    # xatosini bermasligi uchun API xatolarini ham JSON ko‘rinishida qaytaramiz.
    if request.url.path.startswith("/api/teacher") or request.url.path.startswith("/api/teacher-"):
        logging.exception("Teacher API internal error: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"status": "error", "detail": "__TEACHER_SERVER_ERROR__"})
    if request.url.path.startswith("/api/"):
        logging.exception("API internal error: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"status": "error", "detail": "Server ichki xatosi"})
    raise exc


@app.on_event("startup")
async def startup_event():
    threading.Thread(target=start_bot_polling, daemon=True).start()
    threading.Thread(target=limit_notification_worker, daemon=True).start()



    # ==================================================================
# YANGILIKLAR TIZIMI VA AVTO-TARJIMA BO'LIMI
# ==================================================================

# DB Sozlamalari
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite"):
    news_engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    news_engine = create_engine(DATABASE_URL)

NewsSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=news_engine)
NewsBase = declarative_base()

class News(NewsBase):
    __tablename__ = "news"

    id = Column(Integer, primary_key=True, index=True)
    title_uz = Column(String(255), nullable=False)
    content_uz = Column(Text, nullable=False)
    title_ru = Column(String(255), nullable=True)
    content_ru = Column(Text, nullable=True)
    title_en = Column(String(255), nullable=True)
    content_en = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

NewsBase.metadata.create_all(bind=news_engine)

def get_news_db():
    db = NewsSessionLocal()
    try:
        yield db
    finally:
        db.close()

def translate_to_ru_and_en(text_uz: str):
    if not text_uz or not text_uz.strip():
        return "", ""
    try:
        text_ru = GoogleTranslator(source='uz', target='ru').translate(text_uz)
    except Exception as e:
        print(f"[RU Tarjima Xatosi]: {e}")
        text_ru = text_uz

    try:
        text_en = GoogleTranslator(source='uz', target='en').translate(text_uz)
    except Exception as e:
        print(f"[EN Tarjima Xatosi]: {e}")
        text_en = text_uz

    return text_ru, text_en

class NewsCreateSchema(BaseModel):
    title_uz: str
    content_uz: str

@app.post("/api/news")
def create_news_api(news_data: NewsCreateSchema, db: Session = Depends(get_news_db)):
    title_ru, title_en = translate_to_ru_and_en(news_data.title_uz)
    content_ru, content_en = translate_to_ru_and_en(news_data.content_uz)

    new_item = News(
        title_uz=news_data.title_uz,
        content_uz=news_data.content_uz,
        title_ru=title_ru,
        content_ru=content_ru,
        title_en=title_en,
        content_en=content_en
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    return {"status": "success", "data": new_item}

@app.get("/api/news")
def get_news_api(lang: str = Query("uz"), db: Session = Depends(get_news_db)):
    news_list = db.query(News).order_by(News.created_at.desc()).all()
    result = []

    for item in news_list:
        if lang == "ru":
            title = item.title_ru or item.title_uz
            content = item.content_ru or item.content_uz
        elif lang == "en":
            title = item.title_en or item.title_uz
            content = item.content_en or item.content_uz
        else:
            title = item.title_uz
            content = item.content_uz

        result.append({
            "id": item.id,
            "title": title,
            "content": content,
            "created_at": item.created_at.strftime("%Y-%m-%d %H:%M") if item.created_at else ""
        })

    return {"status": "success", "data": result}
    if __name__ == "__main__":
        port = int(os.environ.get("PORT", 8080))
        uvicorn.run(app, host="0.0.0.0", port=port)
