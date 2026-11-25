# -*- coding: utf-8 -*-
"""Referat uchun manba ajratish va Gemini orqali akademik matn tayyorlash."""
from __future__ import annotations

import io
import json
import logging
import re
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
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
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
                         reviewer: str, validation_feedback: str = "") -> str:
    """Mavzuga mos, batafsil va manbalarni to'qimaslikka yo'naltirilgan prompt."""
    has_source = bool(source_text.strip())
    return f"""Siz oliy ta'lim uchun akademik referat yozuvchi mutaxassissiz.
Foydalanuvchiga topshirishga yaroqli, izchil, ilmiy-uslubiy referat tayyorlang.
Javobni faqat quyidagi JSON sxemasida qaytaring:
{{"title":"...","language":"uz|ru|en","abstract":"...","keywords":["..."],"introduction":"...","chapters":[{{"heading":"...","sections":[{{"heading":"...","body":"..."}}]}}],"conclusion":"...","references":["..."]}}

MAZMUN VA TUZILMA TALABLARI:
- Kirish, kamida 3 ta bob, har bir bobda kamida 2 ta kichik bo'lim, xulosa va foydalanilgan adabiyotlar bo'lsin.
- Har bir bob mavzuning alohida jihatini ochsin; boblar va bo'limlar bir-birini takrorlamasin.
- Har bir kichik bo'limni batafsil va mazmunan mustaqil yoz: ta'rif, nazariy asos, sabab-oqibat, amaliy qo'llanish va mavzuga mos misol bo'lsin.
- Har bir kichik bo'limni odatda 180-250 so'z atrofida yoz; mavzuni yetarli och, lekin JSON javobini haddan tashqari uzaytirma.
- Har bir bo'limda bir necha izchil paragraf bo'lsin. Bob sarlavhasidan keyin bo'limlarni chuqur och, oldingi bo'limdagi fikrlarni takrorlama.
- Hajmni sun'iy takrorlash bilan emas, tushuntirish, tahlil, misol va amaliy ahamiyat bilan kengaytir.
- Kirishda dolzarblik, maqsad, vazifalar, obyekt va predmetni izchil yorit.
- Xulosada boblarni ko'chirmasdan, asosiy natijalar va amaliy takliflarni jamla.
- Tilni mavzu va manbaga mos tanla. O'zbekcha mavzu bo'lsa o'zbekcha yoz.
- JSON kalitlari to'liq bo'lsin; chapters ichidagi har bir bo'limda heading va mazmunli body bo'lishi shart.
- JSONni yuborishdan oldin matnni ichki imlo va grammatik tekshiruvdan o'tkaz: o'zbekcha so'zlarni bo'lib yuborma, buzilgan so'z birikmalarini tuzat, apostrof va qo'shimchalarni to'g'ri yoz.
- Yakuniy tekshiruvda ayniqsa "organik-inorgaik", "algeritmlari" kabi buzilgan shakllar qolmaganini tekshir; ularni kontekstga mos ravon o'zbekcha shaklga kelt.
- "yo'lga qo mehanizmi", "buutli", "prinitsipal", "million lab", "mehnating xavfsizligini", "kompyuteriy ko'rish" kabi xato shakllar bo'lmasin; ravon va tabiiy o'zbek tilida yoz.
- Mazmunni o'zgartirmasdan imlo xatolarini tuzat; noaniq terminni taxminiy fakt bilan almashtirma.
- Markdown, izoh, kod bloki yoki JSON tashqarisida matn qaytarma.

MANBALAR VA ADABIYOTLAR HALOLligi:
- Manba berilgan bo'lsa, faktlarni avvalo shu manbaga tayantir; manbada yo'q ma'lumotni undan olingandek ko'rsatma.
- "references" ro'yxatiga faqat real ekaniga ishonching komil bo'lgan kitoblar, ilmiy asarlar yoki rasmiy tashkilot hujjatlarini kirit.
- Kamida 3 ta mavzuga bevosita mos, alohida bibliografik manba keltir. Har bir yozuvda mavjud va ishonchli bilgan muallif/tashkilot hamda asar/hujjat nomi bo'lsin; bitta manbani bir necha yozuvga bo'lib ko'rsatma.
- Nashr yili, nashriyot, DOI yoki URLni faqat aniq bilsang yoz; noma'lum bibliografik maydonlarni taxmin bilan to'ldirma.
- Agar foydalanuvchi yuklagan manbada adabiyotlar ro'yxati bo'lsa, avvalo o'sha yozuvlarni saqla va mavzuga mos, ishonchli qo'shimcha manbalar bilan to'ldir.
- "Foydalanilgan adabiyotlar", "qo'shimcha tekshirish kerak", "ishonchli manbalar" kabi umumiy jumlalarni alohida adabiyot sifatida kiritma.
- Hech qanday aniq, haqiqiy manbani ishonch bilan keltira olmasang, bibliografik ma'lumotni uydirma qilma: references ro'yxatida faqat "Bibliografik manbalarni mavzu bo'yicha ishonchli aniqlash imkoni bo'lmadi; manbalar qo'shimcha tekshiruvdan so'ng kiritilishi kerak." degan ochiq ogohlantirishni ber. Uni adabiyot yoki haqiqiy manba deb ko'rsatma.
- Adabiyotlar ro'yxatini matndagi asosiy mavzu va tushunchalarga mos tanla; mavzuga aloqasiz mashhur kitoblarni faqat sonni to'ldirish uchun kiritma.
- Statistik raqamlar, qonunlar, tajriba natijalari va iqtiboslarni manbasiz uydirma.

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
QAYTA TUZATISH TALABI:
{validation_feedback or 'Birinchi urinish: barcha talablarni to‘liq bajaring.'}
"""


# Faqat ko'p uchragan, aniq tanilgan buzilgan shakllar uchun ehtiyotkor tuzatishlar.
_KNOWN_TEXT_FIXES = (
    ("yo\'lga qo mehanizmi", "yo\'lga qo\'yilishi"),
    ("buutli", "bulutli"),
    ("prinitsipal ravishda", "prinsipial jihatdan"),
    ("prinitsipal", "prinsipial"),
    ("million lab", "millionlab"),
    ("mehnating xavfsizligini", "mehnat xavfsizligini"),
    ("kompyuteriy ko\'rish", "kompyuter ko\'rishi"),
    ("de-ruhsizlantirish", "mehnat jarayonini avtomatlashtirish"),
    ("fizik o mezonlari", "fizik o\'lchamlari"),
    (" o mezonlari", " o\'lchamlari"),
    ("organik-inorgaik", "organik va noorganik"),
    ("algeritmlari", "algoritmlari"),
)


def _correct_known_text_errors(value: Any) -> Any:
    """JSON ichidagi matnlarni formatga tegmasdan, faqat aniq ma'lum typo'lar bo'yicha tuzatadi."""
    if isinstance(value, str):
        result = value
        for wrong, right in _KNOWN_TEXT_FIXES:
            result = re.sub(re.escape(wrong), right, result, flags=re.IGNORECASE)
        return result
    if isinstance(value, list):
        return [_correct_known_text_errors(item) for item in value]
    if isinstance(value, dict):
        return {key: _correct_known_text_errors(item) for key, item in value.items()}
    return value


def _known_language_quality_issues(data: Any) -> list[str]:
    """Qayta urinish kerak bo'lgan aniq, ma'lum buzilgan iboralarni tekshiradi."""
    serialized = json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data or "")
    issues = []
    for wrong, _right in _KNOWN_TEXT_FIXES:
        if re.search(re.escape(wrong), serialized, flags=re.IGNORECASE):
            issues.append(f"Matnda buzilgan ibora bor: {wrong}")
    return issues


def _validate_referat(data: Any) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["Javob JSON obyekt emas."]
    for key in ("title", "introduction", "chapters", "conclusion", "references"):
        if key not in data:
            errors.append(f"{key} kaliti yo'q.")
    if not isinstance(data.get("title"), str) or len(data.get("title", "").strip()) < 4:
        errors.append("title matni bo'sh yoki juda qisqa.")
    if not isinstance(data.get("introduction"), str) or len(data.get("introduction", "").strip()) < 250:
        errors.append("Kirish kamida 250 belgidan iborat bo'lishi kerak.")
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or len(chapters) < 3:
        errors.append("Kamida 3 ta bob bo'lishi shart.")
    else:
        for ci, chapter in enumerate(chapters, 1):
            if not isinstance(chapter, dict) or not isinstance(chapter.get("heading"), str) or not chapter["heading"].strip():
                errors.append(f"{ci}-bob sarlavhasi noto'g'ri.")
                continue
            sections = chapter.get("sections")
            if not isinstance(sections, list) or len(sections) < 2:
                errors.append(f"{ci}-bobda kamida 2 ta kichik bo'lim bo'lishi kerak.")
                continue
            for si, section in enumerate(sections, 1):
                if not isinstance(section, dict):
                    errors.append(f"{ci}-bob, {si}-bo'lim tuzilmasi noto'g'ri.")
                    continue
                if not isinstance(section.get("heading"), str) or not section["heading"].strip():
                    errors.append(f"{ci}-bob, {si}-bo'lim sarlavhasi yo'q.")
                body = section.get("body", "").strip() if isinstance(section.get("body"), str) else ""
                word_count = len(re.findall(r"\b[\wʻ’'-]+\b", body, flags=re.UNICODE))
                if word_count < 100 or len(body) < 600:
                    errors.append(
                        f"{ci}-bob, {si}-bo'lim juda qisqa: {word_count} so'z. "
                        "Kamida 100 so'z va 600 belgi bilan mazmunan to'ldir."
                    )
    if not isinstance(data.get("conclusion"), str) or len(data.get("conclusion", "").strip()) < 200:
        errors.append("Xulosa kamida 200 belgidan iborat bo'lishi kerak.")
    refs = data.get("references")
    if not isinstance(refs, list) or not refs or any(not isinstance(x, str) or not x.strip() for x in refs):
        errors.append("Foydalanilgan adabiyotlar ro'yxati bo'sh yoki noto'g'ri.")
    else:
        cleaned_refs = [x.strip() for x in refs if isinstance(x, str) and x.strip()]
        placeholder_patterns = (
            r"foydalanuvchi taqdim etgan mavzu",
            r"aniq bibliografik manbalar.*tekshir",
            r"bibliografik tekshiruv.*talab",
            r"umumiy bilimlar.*manbalar",
            r"manbalar.*qo'shimcha tekshir",
            r"ishonchli manbalar",
            r"adabiyotlar ro'yxati",
        )
        placeholder_refs = [
            item for item in cleaned_refs
            if any(re.search(pattern, item, flags=re.IGNORECASE) for pattern in placeholder_patterns)
        ]
        if placeholder_refs:
            errors.append("Adabiyotlar ichida umumiy placeholder jumla bor; uni haqiqiy bibliografik manbalar bilan almashtir.")
        credible_entries = [
            item for item in cleaned_refs
            if len(re.findall(r"\b[\wʻ’'-]+\b", item, flags=re.UNICODE)) >= 4
            and not any(re.search(pattern, item, flags=re.IGNORECASE) for pattern in placeholder_patterns)
        ]
        transparent_no_source_notice = (
            len(cleaned_refs) == 1
            and "bibliografik manbalarni mavzu bo'yicha ishonchli aniqlash imkoni bo'lmadi" in cleaned_refs[0].lower()
        )
        if len(credible_entries) < 3 and not transparent_no_source_notice:
            errors.append(
                f"Kamida 3 ta alohida bibliografik manba kerak; hozir {len(credible_entries)} ta mos yozuv bor. "
                "Mavzuga bevosita mos, mavjudligiga ishonchli manbalarni keltir; manbalarni uydirma."
            )
    if "abstract" in data and not isinstance(data["abstract"], str):
        errors.append("abstract matn bo'lishi kerak.")
    if "keywords" in data and not isinstance(data["keywords"], list):
        errors.append("keywords ro'yxat bo'lishi kerak.")
    return errors


def generate_referat_content(*, client: Any, model: str, topic: str,
                             source_text: str = "", student_name: str = "",
                             university: str = "", faculty: str = "", group: str = "",
                             subject: str = "", reviewer: str = "",
                             max_output_tokens: int = 16000,
                             max_attempts: int = 2) -> dict:
    """Gemini javobini sxema va mazmun bo'yicha tekshiradi, xato bo'lsa qayta yaratadi."""
    if not topic.strip() and not source_text.strip():
        raise ReferatError("Referat yaratish uchun mavzu kiriting yoki PDF/DOCX yuklang.")
    from google.genai import types

    feedback = ""
    last_errors = []
    attempts = max(1, min(int(max_attempts), 2))
    for attempt in range(1, attempts + 1):
        prompt = build_referat_prompt(
            topic=topic.strip(), source_text=source_text,
            student_name=student_name, university=university, faculty=faculty,
            group=group, subject=subject, reviewer=reviewer,
            validation_feedback=feedback,
        )
        # Transport/provider xatolari (429/503/timeout) yuqoriga uzatiladi:
        # ular JSON tuzatish urinishlari emas, model/kalit boshqaruvi bilan hal qilinadi.
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.3,
                max_output_tokens=max_output_tokens,
            ),
        )
        try:
            if not response or not response.text:
                raise ValueError("AI bo'sh javob qaytardi.")
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:].strip()
            # Model ba'zan JSONdan keyin ortiqcha matn yoki ikkinchi JSON bo'lagini qo'shadi.
            # Birinchi to'liq JSON obyektini ajratib olamiz; noto'g'ri JSON baribir validation'dan o'tmaydi.
            decoder = json.JSONDecoder()
            start = raw.find("{")
            if start < 0:
                raise ValueError("Javob ichida JSON obyekt topilmadi.")
            data, _json_end = decoder.raw_decode(raw[start:])
            data = _correct_known_text_errors(data)
            errors = _validate_referat(data) + _known_language_quality_issues(data)
            if not errors:
                data["title"] = data["title"].strip()[:240]
                data["language"] = data.get("language") if data.get("language") in {"uz", "ru", "en"} else "uz"
                data["abstract"] = str(data.get("abstract") or "").strip()
                data["keywords"] = [str(x).strip() for x in data.get("keywords", []) if str(x).strip()]
                data["references"] = [x.strip() for x in data["references"]]
                logger.info("Referat AI javobi tekshiruvdan o'tdi | urinish=%s | boblar=%s", attempt, len(data["chapters"]))
                return data
            last_errors = errors
            feedback = "Oldingi javob quyidagi tekshiruvlardan o'tmadi. To'liq qayta yozib, barchasini tuzating:\n- " + "\n- ".join(errors)
            logger.warning("Referat tuzilmasi tekshiruvdan o'tmadi | urinish=%s | xatolar=%s", attempt, len(errors))
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            last_errors = [str(exc)[:300]]
            feedback = "Oldingi javob yaroqli JSON bo'lmadi yoki sxemaga mos emas. Faqat to'g'ri JSON obyekt qaytaring va barcha talablarni bajaring."
            logger.warning("Referat AI javobini tekshirishda xatolik | urinish=%s | sabab=%s", attempt, type(exc).__name__)
    raise ReferatError(
        "AI referatni talab qilingan tuzilma va hajmda tayyorlay olmadi. "
        "Iltimos, qayta urinib ko'ring. " + " ".join(last_errors[:3])
    )
