import os
import re
import io
import json
import random
import string
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, status, Request, BackgroundTasks, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, Float
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship

import pdfplumber
import docx
import httpx

# ==========================================
# CONFIGURATION & LOGGING
# ==========================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./quiz_app.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

engine = create_engine(
    DATABASE_URL, 
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ==========================================
# DATABASE MODELS
# ==========================================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_class=True, primary_key=True, index=True)
    telegram_id = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    is_premium = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    quizzes = relationship("Quiz", back_populates="owner", cascade="all, delete-orphan")
    results = relationship("QuizResult", back_populates="user", cascade="all, delete-orphan")

class Quiz(Base):
    __tablename__ = "quizzes"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_public = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="quizzes")
    questions = relationship("Question", back_populates="quiz", cascade="all, delete-orphan")
    results = relationship("QuizResult", back_populates="quiz", cascade="all, delete-orphan")

class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id"), nullable=False)
    question_text = Column(Text, nullable=False)
    options = Column(JSON, nullable=False) # ["Option A", "Option B", ...]
    correct_option_index = Column(Integer, nullable=False)
    explanation = Column(Text, nullable=True)

    quiz = relationship("Quiz", back_populates="questions")

class QuizResult(Base):
    __tablename__ = "quiz_results"

    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    score = Column(Integer, nullable=False)
    total_questions = Column(Integer, nullable=False)
    percentage = Column(Float, nullable=False)
    completed_at = Column(DateTime, default=datetime.utcnow)

    quiz = relationship("Quiz", back_populates="results")
    user = relationship("User", back_populates="results")

Base.metadata.create_all(bind=engine)

# ==========================================
# FASTAPI APP SETUP
# ==========================================
app = FastAPI(title="Quiz Pilot API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# HELPER FUNCTIONS & AI LOGIC
# ==========================================
def extract_text_from_pdf(file_bytes: bytes) -> str:
    text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
    return text

def extract_text_from_docx(file_bytes: bytes) -> str:
    doc = docx.Document(io.BytesIO(file_bytes))
    return "\n".join([p.text for p in doc.paragraphs if p.text])

async def generate_questions_from_text(text: str, num_questions: int = 10) -> List[Dict[str, Any]]:
    if not GEMINI_API_KEY:
        # Fallback dummy questions if API Key is missing
        return [
            {
                "question": f"Sample Question {i+1} derived from text?",
                "options": ["Option A", "Option B", "Option C", "Option D"],
                "correct_option_index": 0,
                "explanation": "Sample explanation"
            } for i in range(min(num_questions, 5))
        ]

    prompt = f"""
    You are an expert quiz creator. Create a JSON list of {num_questions} multiple-choice quiz questions based on the following text.
    Each item in the list must be a JSON object with:
    - "question": string
    - "options": list of 4 string choices
    - "correct_option_index": integer (0, 1, 2, or 3)
    - "explanation": brief explanation of why the correct option is right.

    Text:
    {text[:4000]}

    Respond ONLY with valid JSON array without markdown formatting.
    """

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}]
        }, timeout=30.0)

        if response.status_code == 200:
            res_json = response.json()
            raw_text = res_json['candidates'][0]['content']['parts'][0]['text']
            # Clean possible markdown formatting
            clean_json = raw_text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_json)
        else:
            logger.error(f"Gemini API Error: {response.text}")
            raise HTTPException(status_code=500, detail="AI Service Error")

            # ==========================================
# FASTAPI ENDPOINTS
# ==========================================
@app.get("/")
def read_root():
    return {"status": "online", "app": "Quiz Pilot API", "version": "2.0.0"}

@app.post("/api/users/auth")
def auth_user(data: Dict[str, Any], db: Session = Depends(get_db)):
    telegram_id = str(data.get("telegram_id"))
    if not telegram_id:
        raise HTTPException(status_code=400, detail="Telegram ID required")

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=data.get("username"),
            first_name=data.get("first_name"),
            last_name=data.get("last_name")
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return {"status": "ok", "user_id": user.id, "telegram_id": user.telegram_id}

@app.get("/api/quizzes")
def get_user_quizzes(telegram_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
    if not user:
        return []

    quizzes = db.query(Quiz).filter(Quiz.owner_id == user.id).all()
    result = []
    for q in quizzes:
        result.append({
            "id": q.id,
            "title": q.title,
            "description": q.description,
            "question_count": len(q.questions),
            "created_at": q.created_at.isoformat()
        })
    return result

@app.get("/api/quizzes/{quiz_id}")
def get_quiz_details(quiz_id: int, db: Session = Depends(get_db)):
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")

    questions = []
    for q in quiz.questions:
        questions.append({
            "id": q.id,
            "question_text": q.question_text,
            "options": q.options,
            "correct_option_index": q.correct_option_index,
            "explanation": q.explanation
        })

    return {
        "id": quiz.id,
        "title": quiz.title,
        "description": quiz.description,
        "questions": questions
    }

@app.post("/api/quizzes/generate-from-file")
async def generate_quiz_from_file(
    telegram_id: str = Form(...),
    title: str = Form(...),
    num_questions: int = Form(10),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
    if not user:
        user = User(telegram_id=str(telegram_id))
        db.add(user)
        db.commit()
        db.refresh(user)

    file_bytes = await file.read()
    filename = file.filename.lower()

    if filename.endswith(".pdf"):
        extracted_text = extract_text_from_pdf(file_bytes)
    elif filename.endswith(".docx"):
        extracted_text = extract_text_from_docx(file_bytes)
    elif filename.endswith(".txt"):
        extracted_text = file_bytes.decode("utf-8", errors="ignore")
    else:
        raise HTTPException(status_code=400, detail="Fayl formati qo'llab-quvvatlanmaydi (PDF, DOCX, TXT)")

    if not extracted_text.strip():
        raise HTTPException(status_code=400, detail="Fayldan matn o'qib bo'lmadi")

    generated_data = await generate_questions_from_text(extracted_text, num_questions)

    new_quiz = Quiz(
        title=title,
        description=f"AI tomonidan {filename} faylidan yaratildi",
        owner_id=user.id
    )
    db.add(new_quiz)
    db.commit()
    db.refresh(new_quiz)

    for item in generated_data:
        question = Question(
            quiz_id=new_quiz.id,
            question_text=item["question"],
            options=item["options"],
            correct_option_index=item["correct_option_index"],
            explanation=item.get("explanation", "")
        )
        db.add(question)

    db.commit()
    return {"status": "success", "quiz_id": new_quiz.id, "title": new_quiz.title}

@app.post("/api/quizzes/{quiz_id}/submit")
def submit_quiz_result(quiz_id: int, data: Dict[str, Any], db: Session = Depends(get_db)):
    telegram_id = str(data.get("telegram_id"))
    score = int(data.get("score", 0))
    total = int(data.get("total", 0))

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    percentage = (score / total * 100) if total > 0 else 0.0

    result = QuizResult(
        quiz_id=quiz_id,
        user_id=user.id,
        score=score,
        total_questions=total,
        percentage=percentage
    )
    db.add(result)
    db.commit()
    return {"status": "saved", "percentage": percentage}

# ==========================================
# TELEGRAM BOT LOGIC & POLLING
# ==========================================
async def send_telegram_message(chat_id: str, text: str, reply_markup: Optional[Dict] = None):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def handle_bot_update(update: Dict[str, Any]):
    if "message" not in update:
        return

    message = update["message"]
    chat_id = str(message["chat"]["id"])
    text = message.get("text", "")
    user_info = message.get("from", {})

    # Auto register user
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == chat_id).first()
        if not user:
            user = User(
                telegram_id=chat_id,
                username=user_info.get("username"),
                first_name=user_info.get("first_name"),
                last_name=user_info.get("last_name")
            )
            db.add(user)
            db.commit()

        if text.startswith("/start"):
            welcome_text = (
                f"Xush kelibsiz, <b>{user_info.get('first_name', 'Foydalanuvchi')}</b>!\n\n"
                "<b>Quiz Pilot Bot</b> - PDF va hujjatingizdan avtomatik testlar yaratuvchi va Mini App orqali "
                "test yechish platformasi.\n\n"
                "Quyidagi Mini App tugmasi orqali ilovaga kiring yoki test yaratish uchun fayl yuboring!"
            )
            web_app_url = os.environ.get("WEB_APP_URL", "https://your-mini-app-url.up.railway.app")
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🚀 Mini App-ni ochish", "web_app": {"url": web_app_url}}],
                    [{"text": "💬 Guruhimizga qo'shilish", "url": "https://t.me/Quiz_AI_Chat"}]
                ]
            }
            await send_telegram_message(chat_id, welcome_text, keyboard)
    finally:
        db.close()
        # ==========================================
# BOT LONG-POLLING LOOP & APP LIFECYCLE
# ==========================================
async def start_telegram_bot_polling():
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN sozlanmagan, Telegram Bot polling boshlanmadi.")
        return

    offset = 0
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    
    logger.info("Telegram Bot polling muvaffaqiyatli ishga tushdi...")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                response = await client.get(url, params={"offset": offset, "timeout": 20})
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            asyncio.create_task(handle_bot_update(update))
                elif response.status_code == 409:
                    logger.error("Conflict: Bot boshqa joyda ham ishlab turibdi (Long polling conflict).")
                    await asyncio.sleep(5)
                else:
                    logger.error(f"Telegram getUpdates xatosi: {response.status_code}")
                    await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Polling davomida kutilmagan xatolik: {e}")
                await asyncio.sleep(5)

@app.on_event("startup")
async def on_startup():
    # FastAPI ishga tushganda orqa fonda bot pollingini ham yurgizadi
    asyncio.create_task(start_telegram_bot_polling())

# Static fayllar (Frontend/Mini App) mavjud bo'lsa ulash
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
