# -*- coding: utf-8 -*-
"""Quiz AI Smart Branding — shared QR branding for generated files.

All settings are read from Railway environment variables at generation time.
New document generators can reuse add_qr_to_docx(), add_qr_to_pdf_bytes(),
or add_qr_to_pptx() without duplicating QR logic.
"""
import io
import logging
import os
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


def _position_xy(position, page_width, page_height, size, margin=22):
    """Calculate the position of the same square QR image for fixed-page formats."""
    if position == "top_left":
        return margin, page_height - margin - size
    if position == "top_right":
        return page_width - margin - size, page_height - margin - size
    if position == "bottom_left":
        return margin, margin
    if position == "center":
        return (page_width - size) / 2, (page_height - size) / 2
    # document_end is treated as bottom-right for fixed-page formats.
    return page_width - margin - size, margin


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

    # PDF, Word va PPTX uchun bir xil kvadrat QR PNG ishlatiladi.
    slide.shapes.add_picture(io.BytesIO(png), left, top, width=size, height=size)
    return True


def add_qr_to_pdf_bytes(pdf_bytes, feature="SLIDES"):
    """Overlay the QR on the final PDF page while preserving existing pages/content."""
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

    page = reader.pages[-1]
    box = page.mediabox
    page_width = float(box.width)
    page_height = float(box.height)
    size = float(config["size"])
    x, y = _position_xy(config["position"], page_width, page_height, size)
    overlay_buffer = io.BytesIO()
    layer = canvas.Canvas(overlay_buffer, pagesize=(page_width, page_height))
    layer.saveState()
    # PDF ham PPTX va Word kabi faqat umumiy QR PNG tasvirini joylaydi.
    layer.drawImage(
        ImageReader(io.BytesIO(png)),
        x, y, width=size, height=size,
        preserveAspectRatio=True, mask="auto"
    )
    layer.restoreState()
    layer.save()
    overlay_buffer.seek(0)
    overlay_page = PdfReader(overlay_buffer).pages[0]
    page.merge_page(overlay_page)

    writer = PdfWriter()
    for item in reader.pages:
        writer.add_page(item)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def add_qr_to_docx(document, feature="TEACHER"):
    """Append a centered/positioned QR block to a generated Word document."""
    config = get_branding_config(feature)
    png = _make_qr_png(config)
    if not png:
        return False

    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches

    position = config["position"]
    alignment = WD_ALIGN_PARAGRAPH.CENTER
    if position in ("top_left", "bottom_left"):
        alignment = WD_ALIGN_PARAGRAPH.LEFT
    elif position in ("top_right", "bottom_right"):
        alignment = WD_ALIGN_PARAGRAPH.RIGHT

    paragraph = document.add_paragraph()
    paragraph.alignment = alignment
    run = paragraph.add_run()
    # Word ham PDF va PPTX kabi aynan shu QR PNG'ni ishlatadi; alohida yozuv yo'q.
    run.add_picture(io.BytesIO(png), width=Inches(config["size"] / 72.0))
    return True
