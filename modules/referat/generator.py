# -*- coding: utf-8 -*-
"""Referat uchun manba ajratish va Gemini orqali akademik matn tayyorlash."""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from docx import Document
from pypdf import PdfReader

logger = logging.getLogger(__name__)


class ReferatError(ValueError):
    """Foydalanuvchi kiritgan mavzu yoki manba bilan bog'liq xatolik."""


def extract_source_text(raw: bytes, filename: str, max_pages: int = 150,
                        max_chars: int = 80000) -> str:
    """PDF yoki DOCX matnini xavfsiz ajratadi; matnni jim kesmaydi."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise ReferatError("Faqat PDF yoki DOCX fayl yuklash mumkin.")
    if not raw:
        raise ReferatError("Yuklangan fayl bo'sh.")
    try:
        if suffix == ".pdf":
            reader = PdfReader(io.BytesIO(raw))
            if len(reader.pages) > max_pages:
                raise ReferatError(f"PDF sahifalari soni {max_pages} ta limitdan oshdi.")
            chunks = [(page.extract_text() or "") for page in reader.pages]
            text = "\n\n".join(chunks)
        else:
            doc = Document(io.BytesIO(raw))
            chunks = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    chunks.append(" | ".join(cell.text.strip() for cell in row.cells))
            text = "\n".join(chunks)
    except ReferatError:
        raise
    except Exception as exc:
        logger.exception("Referat manba faylini o'qishda xatolik | fayl_turi=%s", suffix)
        raise ReferatError("Faylni o'qib bo'lmadi. Matnli PDF yoki to'g'ri DOCX yuboring.") from exc
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        raise ReferatError("Fayldan matn ajratilmadi. PDF skanerlangan rasm bo'lishi mumkin.")
    if len(text) > max_chars:
        raise ReferatError(f"Manba matni {max_chars:,} belgidan oshdi. Faylni kichikroq qismlarga bo'ling.")
    return text


def build_referat_prompt(*, topic: str, source_text: str, student_name: str,
                         university: str, faculty: str, group: str, subject: str,
                         reviewer: str) -> str:
    """Manba va titul ma'lumotlariga mos, o'zbekcha akademik prompt."""
    has_source = bool(source_text.strip())
    return f"""Siz oliy ta'lim uchun akademik referat yozuvchi mutaxassissiz.
Foydalanuvchiga topshirishga yaroqli, izchil, ilmiy-uslubiy referat tayyorlang.
Javobni faqat quyidagi JSON tuzilmasida qaytaring:
{{"title":"...","language":"uz|ru|en","abstract":"...","keywords":["..."],"introduction":"...","chapters":[{{"heading":"...","sections":[{{"heading":"...","body":"..."}}]}}],"conclusion":"...","references":["..."]}}

TALABLAR:
- Matn tilini mavzu va manba tiliga moslang; foydalanuvchi mavzuni o'zbekcha bergan bo'lsa o'zbekcha yozing.
- Akademik, ravon, mazmunli va takrorlarsiz yozing. Kirish, kamida 3 ta mazmunli bob, xulosa va foydalanilgan adabiyotlar bo'lsin.
- Kirishda mavzuning dolzarbligi, maqsad, vazifalar va o'rganish obyekti qisqacha yoritilsin.
- Har bir bobda kamida 2 ta kichik bo'lim bo'lsin. Bo'limlar mazmunan yetarli darajada batafsil bo'lsin.
- Xulosada boblarni takrorlamasdan asosiy natijalar va amaliy takliflarni jamlang.
- Manbada yo'q aniq statistik raqamlar, tajriba natijalari, qonun nomlari yoki bibliografik ma'lumotlarni to'qimang.
- Manba berilgan bo'lsa, faktlar va mazmunni avvalo shu manbaga tayangan holda yozing. Manbada yo'q ma'lumotni manbadan olingandek ko'rsatmang.
- Adabiyotlar ro'yxatida manbada aniq ko'rsatilgan bibliografik ma'lumotlarni saqlang. Yetarli ma'lumot bo'lmasa, mavjud manbani umumiy nomi bilan belgilang; soxta muallif/nashr/yil yaratmang.
- Javobda Markdown yoki kod bloklari ishlatmang; JSON qiymatlari oddiy matn bo'lsin.

FOYDALANUVCHI MA'LUMOTLARI:
Mavzu: {topic or 'Yuklangan manba asosida'}
Talaba: {student_name or 'Ko‘rsatilmagan'}
Universitet: {university or 'Ko‘rsatilmagan'}
Fakultet: {faculty or 'Ko‘rsatilmagan'}
Guruh: {group or 'Ko‘rsatilmagan'}
Fan: {subject or 'Ko‘rsatilmagan'}
Tekshiruvchi: {reviewer or 'Ko‘rsatilmagan'}

MANBA BOR: {'HA' if has_source else 'YO‘Q'}
MANBA MATNI:
{source_text if has_source else 'Foydalanuvchi mavzusi asosida umumiy akademik bilimlardan foydalaning. Tekshirilmagan fakt yoki soxta manbalarni kiritmang.'}
"""


def generate_referat_content(*, client: Any, model: str, topic: str,
                             source_text: str = "", student_name: str = "",
                             university: str = "", faculty: str = "", group: str = "",
                             subject: str = "", reviewer: str = "",
                             max_output_tokens: int = 16000) -> dict:
    """Gemini JSON javobini oladi va asosiy bo'limlarni tekshiradi."""
    if not topic.strip() and not source_text.strip():
        raise ReferatError("Referat yaratish uchun mavzu kiriting yoki PDF/DOCX yuklang.")
    prompt = build_referat_prompt(
        topic=topic.strip(), source_text=source_text,
        student_name=student_name, university=university, faculty=faculty,
        group=group, subject=subject, reviewer=reviewer,
    )
    from google.genai import types
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.35,
            max_output_tokens=max_output_tokens,
        ),
    )
    if not response or not response.text:
        raise ReferatError("AI bo'sh javob qaytardi. Iltimos, qayta urinib ko'ring.")
    import json
    try:
        data = json.loads(response.text)
    except Exception as exc:
        logger.warning("Referat AI javobi JSON formatida emas")
        raise ReferatError("AI javobini hujjatga aylantirib bo'lmadi. Qayta urinib ko'ring.") from exc
    required = ("title", "introduction", "chapters", "conclusion", "references")
    missing = [key for key in required if key not in data]
    if missing or not isinstance(data.get("chapters"), list) or len(data["chapters"]) < 2:
        raise ReferatError("AI referatning barcha zarur bo'limlarini tayyorlay olmadi. Qayta urinib ko'ring.")
    for chapter in data["chapters"]:
        if not isinstance(chapter, dict) or not chapter.get("heading") or not chapter.get("sections"):
            raise ReferatError("Referat boblari tuzilmasi to'liq emas. Qayta urinib ko'ring.")
    data["title"] = str(data["title"]).strip()[:240]
    data["language"] = data.get("language") if data.get("language") in {"uz", "ru", "en"} else "uz"
    data["keywords"] = data.get("keywords") if isinstance(data.get("keywords"), list) else []
    data["references"] = data.get("references") if isinstance(data.get("references"), list) else []
    return data
