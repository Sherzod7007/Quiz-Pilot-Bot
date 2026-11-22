# -*- coding: utf-8 -*-
"""Quiz AI Smart Branding — shared QR branding for generated files.

All settings are read from Railway environment variables at generation time.
New document generators can reuse add_qr_to_docx(), add_qr_to_pdf_bytes(),
or add_qr_to_pptx() without duplicating QR logic.
"""
import io
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_VALID_POSITIONS = {
    "top_left", "top_right", "bottom_left", "bottom_right", "center",
    "document_end",
}


def _env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in _TRUE_VALUES


def get_branding_config(feature=None):
    """Return validated live Railway settings for the requested output feature."""
    enabled = _env_bool("QUIZ_AI_BRANDING_ENABLED", False)
    if feature:
        feature_key = "QUIZ_AI_BRANDING_" + str(feature).strip().upper()
        if feature_key in os.environ:
            enabled = enabled and _env_bool(feature_key, True)

    position = os.getenv("QUIZ_AI_QR_POSITION", "bottom_right").strip().lower()
    if position not in _VALID_POSITIONS:
        logger.warning("QUIZ_AI_QR_POSITION noto'g'ri; bottom_right ishlatiladi.")
        position = "bottom_right"

    # QR kodning asl o'lchami saqlanadi; barcha formatlar aynan shu qiymatdan foydalanadi.
    try:
        size = max(36, min(180, int(os.getenv("QUIZ_AI_QR_SIZE", "84"))))
    except (TypeError, ValueError):
        size = 84

    color = os.getenv("QUIZ_AI_QR_COLOR", "#2589D8").strip()
    if not (len(color) == 7 and color.startswith("#") and
            all(c in "0123456789abcdefABCDEF" for c in color[1:])):
        logger.warning("QUIZ_AI_QR_COLOR noto'g'ri; standart rang ishlatiladi.")
        color = "#2589D8"

    is_referat = str(feature or "").strip().upper() == "REFERAT"
    return {
        "enabled": enabled,
        "url": os.getenv("QUIZ_AI_QR_URL", "https://t.me/Quiz_AI_Test_Bot").strip(),
        "position": position,
        "size": size,
        "color": color,
        "logo_enabled": _env_bool("QUIZ_AI_QR_LOGO_ENABLED", False),
        "logo_path": os.getenv("QUIZ_AI_LOGO_PATH", str(Path(__file__).with_name("quiz_ai_logo.jpg"))).strip(),
        # QR ostida hech qanday yozuv chiqarilmaydi.
        "caption": "",
    }


def _make_qr_png(config):
    """Create a real QR image from the configured URL; never use a mock image."""
    if not config["enabled"]:
        return None
    if not config["url"]:
        logger.warning("QR branding yoqilgan, ammo QUIZ_AI_QR_URL kiritilmagan; QR joylanmadi.")
        return None

    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_H
        from PIL import Image

        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(config["url"])
        qr.make(fit=True)
        image = qr.make_image(
            fill_color=config["color"],
            back_color="white",
        ).convert("RGBA")

        if config["logo_enabled"] and config["logo_path"]:
            logo_path = Path(config["logo_path"])
            if logo_path.is_file():
                try:
                    logo = Image.open(str(logo_path)).convert("RGBA")
                    max_logo = max(1, image.width // 4)
                    logo.thumbnail((max_logo, max_logo), Image.Resampling.LANCZOS)
                    # Oq fon QR markazidagi logoni ajratib, skanerlashni ishonchliroq qiladi.
                    pad = max(4, image.width // 50)
                    plate = Image.new(
                        "RGBA",
                        (logo.width + pad * 2, logo.height + pad * 2),
                        (255, 255, 255, 255),
                    )
                    plate.alpha_composite(logo, (pad, pad))
                    image.alpha_composite(
                        plate,
                        ((image.width - plate.width) // 2,
                         (image.height - plate.height) // 2),
                    )
                except Exception:
                    logger.exception("Quiz AI QR markaziga logo qo'shib bo'lmadi.")
            else:
                logger.warning("QUIZ_AI_LOGO_PATH topilmadi; QR logosiz yaratildi.")

        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG", optimize=True)
        return output.getvalue()
    except Exception:
        logger.exception("Quiz AI QR kodini yaratishda xatolik.")
        return None


def _position_xy(position, page_width, page_height, size, margin=22, caption_height=14):
    if position == "top_left":
        return margin, page_height - margin - size - caption_height
    if position == "top_right":
        return page_width - margin - size, page_height - margin - size - caption_height
    if position == "bottom_left":
        return margin, margin + caption_height
    if position == "center":
        return (page_width - size) / 2, (page_height - size) / 2
    # document_end is treated as bottom-right for fixed-page formats.
    return page_width - margin - size, margin + caption_height


def add_qr_to_pptx(presentation, feature="SLIDES"):
    """Place the configured QR on the final slide of a python-pptx presentation."""
    config = get_branding_config(feature)
    png = _make_qr_png(config)
    if not png or not presentation.slides:
        return False

    from pptx.util import Inches

    slide = presentation.slides[-1]
    slide_w = presentation.slide_width
    slide_h = presentation.slide_height
    size = Inches(config["size"] / 72.0)
    margin = Inches(0.22)
    position = config["position"]
    if position == "top_left":
        left, top = margin, margin
    elif position == "top_right":
        left, top = slide_w - margin - size, margin
    elif position == "bottom_left":
        left, top = margin, slide_h - margin - size
    elif position == "center":
        left, top = (slide_w - size) // 2, (slide_h - size) // 2
    else:
        left, top = slide_w - margin - size, slide_h - margin - size

    # Oq fonli QR rasm; mavjud slayd dizaynining ustiga minimal joy egallaydi.
    slide.shapes.add_picture(io.BytesIO(png), left, top, width=size, height=size)
    return True


def _find_safe_pdf_position(page, preferred, page_width, page_height, size, margin=18):
    """Find a free corner on a PDF page; return None when all corners are occupied."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    occupied = []

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        try:
            x, y = float(tm[4]), float(tm[5])
            font_size = max(5.0, float(font_size or 10))
            font_name = (font_dict or {}).get("/BaseFont", "Helvetica")
            try:
                width = stringWidth(text, "Helvetica", font_size)
            except Exception:
                width = max(font_size * 0.45, len(text) * font_size * 0.45)
            occupied.append((x - 2, y - font_size * 0.45,
                             x + max(width, 2) + 2, y + font_size * 1.25))
        except Exception:
            # Noma'lum matn koordinatasi bo'lsa, xavfsiz tomonga og'amiz:
            # keyingi tekshiruv barcha burchaklarni band deb hisoblaydi.
            occupied.append((0, 0, page_width, page_height))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        return None

    positions = [preferred, "bottom_right", "bottom_left", "top_right", "top_left"]
    unique_positions = []
    for position in positions:
        if position in ("center", "document_end"):
            continue
        if position not in unique_positions:
            unique_positions.append(position)

    for position in unique_positions:
        if position == "top_left":
            x, y = margin, page_height - margin - size
        elif position == "top_right":
            x, y = page_width - margin - size, page_height - margin - size
        elif position == "bottom_left":
            x, y = margin, margin
        else:
            x, y = page_width - margin - size, margin

        candidate = (x, y, x + size, y + size)
        if candidate[0] < 0 or candidate[1] < 0 or candidate[2] > page_width or candidate[3] > page_height:
            continue
        if not any(not (candidate[2] < bx1 or candidate[0] > bx2 or
                        candidate[3] < by1 or candidate[1] > by2)
                   for bx1, by1, bx2, by2 in occupied):
            return x, y
    return None


def add_qr_to_pdf_bytes(pdf_bytes, feature="SLIDES"):
    """Add a compact QR without covering page text; use a clean page if needed."""
    config = get_branding_config(feature)
    png = _make_qr_png(config)
    if not png:
        return pdf_bytes

    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if not reader.pages:
        return pdf_bytes

    is_summary = str(feature).strip().upper() == "SUMMARY"
    target_index = len(reader.pages) - 1
    if is_summary:
        while target_index > 0:
            page_text = reader.pages[target_index].extract_text() or ""
            without_brand = re.sub(r"quiz\s*ai", "", page_text, flags=re.IGNORECASE)
            normalized = re.sub(r"[^a-z0-9]+", "", without_brand.lower())
            if not normalized:
                target_index -= 1
                continue
            break

    page = reader.pages[target_index]
    page_width = float(page.mediabox.width)
    page_height = float(page.mediabox.height)
    size = float(config["size"])
    preferred = "bottom_left" if is_summary else config["position"]
    safe_xy = _find_safe_pdf_position(page, preferred, page_width, page_height, size)

    # Matn bilan to'qnashadigan joy topilmasa, QR uchun alohida toza sahifa.
    add_separate_page = safe_xy is None
    if add_separate_page:
        x, y = page_width - 18 - size, 18
    else:
        x, y = safe_xy

    overlay_buffer = io.BytesIO()
    layer = canvas.Canvas(overlay_buffer, pagesize=(page_width, page_height))
    # Word, PDF va PPTX'da bir xil PNG ishlatiladi; qo'shimcha oq ramka
    # chizilmaydi. PNG ichidagi standart QR quiet-zone saqlanadi.
    layer.drawImage(ImageReader(io.BytesIO(png)), x, y,
                    width=size, height=size, preserveAspectRatio=True, mask="auto")
    layer.save()
    overlay_buffer.seek(0)
    overlay_page = PdfReader(overlay_buffer).pages[0]

    writer = PdfWriter()
    for index, item in enumerate(reader.pages):
        if is_summary and index > target_index:
            continue
        writer.add_page(item)
    if add_separate_page:
        qr_page = writer.add_blank_page(width=page_width, height=page_height)
        qr_page.merge_page(overlay_page)
    else:
        writer.pages[target_index].merge_page(overlay_page)

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def add_qr_to_docx(document, feature="TEACHER", page_break=False):
    """Append a centered/positioned QR block to a generated Word document.

    page_break=True places the QR branding on a dedicated final page. Existing
    callers retain their previous behavior because the default is False.
    """
    config = get_branding_config(feature)
    png = _make_qr_png(config)
    if not png:
        return False

    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt

    feature_name = str(feature).strip().upper()
    is_summary = feature_name == "SUMMARY"
    is_referat = feature_name == "REFERAT"
    position = config["position"]
    alignment = WD_ALIGN_PARAGRAPH.CENTER
    if is_summary:
        # Konspekt QR kodi yangi sahifa ochmasdan, oxirgi matndan keyin chapda turadi.
        # Sarlavha va alohida "Quiz AI" yozuvi qo'shilmaydi.
        alignment = WD_ALIGN_PARAGRAPH.LEFT
        page_break = False
    elif is_referat:
        # Referat QR'i yangi sahifaga o'tmaydi; chap/o'ng joylashuv Railway'dan olinadi.
        page_break = False
        if position in ("top_left", "bottom_left"):
            alignment = WD_ALIGN_PARAGRAPH.LEFT
        elif position in ("top_right", "bottom_right"):
            alignment = WD_ALIGN_PARAGRAPH.RIGHT
    elif position in ("top_left", "bottom_left"):
        alignment = WD_ALIGN_PARAGRAPH.LEFT
    elif position in ("top_right", "bottom_right"):
        alignment = WD_ALIGN_PARAGRAPH.RIGHT

    if page_break:
        document.add_page_break()
    paragraph = document.add_paragraph()
    paragraph.alignment = alignment if not page_break else WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.keep_together = True
    run = paragraph.add_run()
    run.add_picture(io.BytesIO(png), width=Inches(config["size"] / 72.0))
    return True
