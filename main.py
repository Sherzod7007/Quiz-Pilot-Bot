# -*- coding: utf-8 -*-
import logging
import json
import os
import time
import sqlite3
from pypdf import PdfReader
import docx
import telebot
from telebot import types
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from typing import List, Optional

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)

raw_keys = os.getenv("GOOGLE_API_KEYS", "")
GOOGLE_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()] if raw_keys else []
current_key_index = 0

DOWNLOADS_DIR = 'downloads'
DB_PATH = "/data/quiz_pilot.db" if os.path.exists("/data") else "quiz_pilot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute('''CREATE TABLE IF NOT EXISTS quizzes (id TEXT PRIMARY KEY, user_id INTEGER, title TEXT, total INTEGER, answered INTEGER, quiz_json TEXT, created_at INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, created_at INTEGER)''')
    conn.commit()
    conn.close()

init_db()

class QuizItem(BaseModel):
    question: str = Field(description="Savol matni")
    options: List[str] = Field(description="To'g'ri javob va 3 ta noto'g'ri variantdan iborat jami 4 ta variant ro'yxati")
    correct_index: int = Field(description="To'g'ri javob joylashtirilgan indeks raqami")
    explanation: str = Field(description="Ushbu javob nega to'g'riligini tushuntiruvchi qisqa qoida")

class QuizResponse(BaseModel):
    quizzes: List[QuizItem] = Field(description="Test savollari ro'yxati")

class ProgressUpdateRequest(BaseModel):
    quiz_id: str
    user_id: int

def add_user_to_db(user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (user_id, int(time.time())))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Foydalanuvchi qo'shishda xato: {e}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Keshni yo'q qilish uchun HTML to'g'ridan-to'g'ri shu yerga joylashtirildi
HTML_CONTENT = """<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Quiz Pilot - Super TMA</title>
    <script src="https://telegram.org"></script>
    <style>
        :root {
            --bg-color: #121214;
            --card-bg: #1a1a1e;
            --text-color: #ffffff;
            --text-hint: #8e8e93;
            --accent-blue: #2f80ed;
            --accent-green: #34c759;
            --accent-red: #ff3b30;
            --border-color: #2c2c35;
            --accent-yellow: #f2c94c;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0; padding: 15px; padding-bottom: 90px;
            -webkit-user-select: none; user-select: none;
        }
        .header h1 { font-size: 26px; margin: 0 0 15px 0; font-weight: 700; }
        .tabs { display: flex; gap: 8px; margin-bottom: 20px; }
        .tab { background: #24242b; border: none; padding: 10px 16px; border-radius: 20px; color: var(--text-hint); font-weight: 600; font-size: 14px; cursor: pointer; flex: 1; text-align: center; }
        .tab.active { background: var(--accent-blue); color: #fff; }
        .quiz-card { background: var(--card-bg); border-radius: 20px; padding: 18px; margin-bottom: 14px; border: 1px solid var(--border-color); }
        .card-top { display: flex; justify-content: space-between; align-items: flex-start; }
        .card-title { font-size: 18px; font-weight: 700; margin: 0 0 6px 0; max-width: 65%; }
        .action-group { display: flex; gap: 8px; }
        .btn-circle { background: #24242b; border: none; border-radius: 12px; width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; font-size: 15px; cursor: pointer; color: #fff; }
        .progress-row { display: flex; justify-content: space-between; align-items: center; margin-top: 5px; }
        .progress-line-container { background: #2c2c35; border-radius: 6px; height: 6px; flex: 1; margin-right: 15px; overflow: hidden; }
        .progress-line-bar { height: 100%; border-radius: 6px; transition: width 0.3s; background: var(--accent-blue); }
        .card-footer { display: flex; justify-content: space-between; font-size: 13px; color: var(--text-hint); margin-top: 8px; }
        .screen { display: none; }
        .screen.active { display: block; }
        .upload-box { border: 2px dashed var(--border-color); border-radius: 20px; padding: 40px 20px; text-align: center; cursor: pointer; background: var(--card-bg); margin-bottom: 20px; color: var(--text-hint); }
        .input-text { width: 100%; box-sizing: border-box; background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 14px; padding: 15px; color: #fff; font-size: 15px; resize: vertical; margin-bottom: 20px; }
        .submit-btn { background: var(--accent-blue); color: #fff; border: none; width: 100%; padding: 16px; border-radius: 14px; font-size: 16px; font-weight: bold; cursor: pointer; }
        #game-screen { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: var(--bg-color); z-index: 1000; padding: 20px; display: none; overflow-y: auto; }
        .game-header { display: flex; justify-content: space-between; font-weight: bold; margin-bottom: 30px; }
        .q-text { font-size: 20px; font-weight: 700; margin-bottom: 25px; line-height: 1.4; }
        .options-list { display: flex; flex-direction: column; gap: 12px; }
        .opt-btn { background: var(--card-bg); border: 1px solid var(--border-color); padding: 16px; border-radius: 14px; text-align: left; font-size: 16px; color: #fff; cursor: pointer; }
        .opt-btn.correct { background: var(--accent-green) !important; border-color: var(--accent-green); }
        .opt-btn.wrong { background: var(--accent-red) !important; border-color: var(--accent-red); }
        .explanation-box { background: #24242b; border-radius: 12px; padding: 14px; margin-top: 20px; font-size: 14px; border-left: 4px solid var(--accent-blue); display: none; }
        .next-btn { background: var(--accent-blue); color: white; border: none; padding: 15px; border-radius: 14px; font-size: 16px; font-weight: bold; width: 100%; margin-top: 25px; cursor: pointer; display: none; }
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; height: 75px; background: #16161a; display: flex; justify-content: space-around; border-top: 1px solid var(--border-color); align-items: center; z-index: 99; }
        .nav-item { display: flex; flex-direction: column; align-items: center; background: none; border: none; font-size: 11px; color: var(--text-hint); cursor: pointer; }
        
        .nav-item.active { color: var(--accent-blue); }
        .nav-item.active#nav-create .nav-icon { color: var(--accent-yellow) !important; }
        .nav-icon { font-size: 20px; margin-bottom: 4px; color: var(--text-hint); }
        .loader { display: none; text-align: center; padding: 20px; font-size: 16px; color: var(--accent-blue); font-weight: bold; }
    </style>
</head>
<body>
    <div id="create-screen" class="screen">
        <div class="header"><h1>Yangi Test Yaratish</h1></div>
        <div class="upload-box" onclick="document.getElementById('file-input').click()">
            <span style="font-size: 40px; display: block; margin-bottom: 10px;">📂</span>
            <span id="upload-text">Darslik yuklash (PDF yoki DOCX)</span>
            <input type="file" id="file-input" accept=".pdf,.docx" style="display: none;" onchange="fileSelected(this)">
        </div>
        <div style="text-align: center; color: var(--text-hint); margin-bottom: 20px;">yoki matn kiriting:</div>
        <textarea id="text-input" class="input-text" rows="6" placeholder="Mavzu yoki konspekt matnini joylang..."></textarea>
        <button id="generate-btn" class="submit-btn" onclick="startGeneration()">AI yordamida yaratish 🚀</button>
        <div id="loading-status" class="loader">⏳ AI test tayyorlamoqda, iltimos kuting...</div>
    </div>

    <div id="library-screen" class="screen active">
        <div class="header"><h1>Kutubxona</h1></div>
        <div class="tabs">
            <button class="tab active">❓ Testlar</button>
            <button class="tab" onclick="alert('Tez kunda ommaviy bo\'lim ishga tushadi!')">🌐 Ommaviy</button>
            <button class="tab" onclick="alert('Tez kunda flesh-kartochkalar qo\'shiladi!')">🗂️ Kartochkalar</button>
        </div>
        <div id="quiz-list">
            <div style="text-align: center; color: var(--text-hint); padding: 40px;">Sizda hali testlar yo'q. "Yaratish" bo'limidan yangi test qo'shing!</div>
        </div>
    </div>

    <div id="game-screen">
        <div class="game-header">
            <span id="game-title">Test</span>
            <span style="color: var(--accent-red); cursor: pointer;" onclick="closeGame()">✖ Chiqish</span>
        </div>
        <div id="game-progress" style="font-size: 14px; color: var(--text-hint); margin-bottom: 10px;">Savol: 1/10</div>
