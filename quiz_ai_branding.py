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

    try:
        size = max(36, min(180, int(os.getenv("QUIZ_AI_QR_SIZE", "84"))))
    except (TypeError, ValueError):
        size = 84

    color = os.getenv("QUIZ_AI_QR_COLOR", "#2589D8").strip()
    if not (len(color) == 7 and color.startswith("#") and
            all(c in "0123456789abcdefABCDEF" for c in color[1:])):
        logger.warning("QUIZ_AI_QR_COLOR noto'g'ri; standart rang ishlatiladi.")
        color = "#2589D8"

    return {
        "enabled": enabled,
        "url": os.getenv("QUIZ_AI_QR_URL", "https://t.me/Quiz_AI_Test_Bot").strip(),
        "position": position,
        "size": size,
        "color": color,
        "logo_enabled": _env_bool("QUIZ_AI_QR_LOGO_ENABLED", False),
        "logo_path": os.getenv("QUIZ_AI_LOGO_PATH", str(Path(__file__).with_name("quiz_ai_logo.jpg"))).strip(),
        "caption": os.getenv("QUIZ_AI_QR_CAPTION", "Quiz AI").strip()[:60],
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

    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.enum.shapes import MSO_SHAPE

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
    if config["caption"]:
        caption = slide.shapes.add_textbox(left, top + size, size, Inches(0.20))
        paragraph = caption.text_frame.paragraphs[0]
        paragraph.text = config["caption"]
        paragraph.alignment = PP_ALIGN.CENTER
        paragraph.font.size = Pt(8)
        paragraph.font.bold = True
        paragraph.font.color.rgb = RGBColor(35, 80, 110)
    return True


def add_qr_to_pdf_bytes(pdf_bytes, feature="SLIDES"):
    """Overlay QR on the final content page; summary branding pages are omitted."""
    config = get_branding_config(feature)
    png = _make_qr_png(config)
    if not png:
        return pdf_bytes

    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    source = io.BytesIO(pdf_bytes)
    reader = PdfReader(source)
    if not reader.pages:
        return pdf_bytes

    is_summary = str(feature).strip().upper() == "SUMMARY"

    # Konspekt PDF'ida oxiriga qo'shilgan faqat "Quiz AI" yozuvi/QR turgan
    # brending sahifalarini chiqarib tashlaymiz va QR'ni oxirgi mazmunli
    # sahifaning pastki chap tomoniga joylaymiz.
    target_index = len(reader.pages) - 1
    if is_summary:
        while target_index > 0:
            page_text = (reader.pages[target_index].extract_text() or "")
            # "Quiz AI" sarlavhasi va caption'i bir yoki bir necha marta
            # uchrasa ham ularni chiqarib, sahifada haqiqiy matn qolganini tekshiramiz.
            without_brand = re.sub(r"quiz\s*ai", "", page_text, flags=re.IGNORECASE)
            normalized = re.sub(r"[^a-z0-9]+", "", without_brand.lower())
            # Faqat brending yozuvi bo'lgan yoki mutlaqo bo'sh sahifa.
            if normalized == "":
                target_index -= 1
                continue
            break

    page = reader.pages[target_index]
    box = page.mediabox
    page_width = float(box.width)
    page_height = float(box.height)
    size = float(config["size"])

    # PDF Konspekt QR'i doim pastki chapda; Word funksiyasiga tegilmaydi.
    position = "bottom_left" if is_summary else config["position"]
    x, y = _position_xy(position, page_width, page_height, size)
    overlay_buffer = io.BytesIO()
    layer = canvas.Canvas(overlay_buffer, pagesize=(page_width, page_height))
    layer.saveState()
    layer.setFillColorRGB(1, 1, 1)
    caption_enabled = bool(config["caption"]) and not is_summary
    layer.roundRect(
        x - 4, y - 4, size + 8,
        size + (22 if caption_enabled else 8), 4, fill=1, stroke=0
    )
    layer.drawImage(
        ImageReader(io.BytesIO(png)), x, y,
        width=size, height=size, preserveAspectRatio=True, mask="auto"
    )
    if caption_enabled:
        layer.setFillColorRGB(0.12, 0.28, 0.40)
        layer.setFont("Helvetica-Bold", 8)
        layer.drawCentredString(x + size / 2, y - 13, config["caption"])
    layer.restoreState()
    layer.save()
    overlay_buffer.seek(0)
    overlay_page = PdfReader(overlay_buffer).pages[0]
    page.merge_page(overlay_page)

    writer = PdfWriter()
    for index, item in enumerate(reader.pages):
        if is_summary and index > target_index:
            continue
        writer.add_page(item)
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
    from docx.shared import Inches, Pt, RGBColor

    is_summary = str(feature).strip().upper() == "SUMMARY"
    position = config["position"]
    alignment = WD_ALIGN_PARAGRAPH.CENTER
    if is_summary:
        # Konspekt QR kodi yangi sahifa ochmasdan, oxirgi matndan keyin chapda turadi.
        # Sarlavha va alohida "Quiz AI" yozuvi qo'shilmaydi.
        alignment = WD_ALIGN_PARAGRAPH.LEFT
        page_break = False
    elif position in ("top_left", "bottom_left"):
        alignment = WD_ALIGN_PARAGRAPH.LEFT
    elif position in ("top_right", "bottom_right"):
        alignment = WD_ALIGN_PARAGRAPH.RIGHT

    if page_break:
        document.add_page_break()
        heading = document.add_paragraph()
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        heading_run = heading.add_run("Quiz AI")
        heading_run.bold = True
        heading_run.font.size = Pt(18)
        heading_run.font.color.rgb = RGBColor(35, 80, 110)
    paragraph = document.add_paragraph()
    paragraph.alignment = alignment if not page_break else WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    run.add_picture(io.BytesIO(png), width=Inches(config["size"] / 72.0))
    # Konspektda faqat QR tasviri qoladi; boshqa hujjatlarning caption'i saqlanadi.
    if config["caption"] and not is_summary:
        caption = document.add_paragraph()
        caption.alignment = alignment
        text_run = caption.add_run(config["caption"])
        text_run.bold = True
        text_run.font.size = Pt(8)
        text_run.font.color.rgb = RGBColor(35, 80, 110)
    return True
