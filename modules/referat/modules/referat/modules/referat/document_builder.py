# -*- coding: utf-8 -*-
"""Referatni DOCX va PDF ko'rinishida bir xil mantiqiy tartibda tayyorlash."""
from __future__ import annotations

import io
import logging
import re
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepTogether

logger = logging.getLogger(__name__)


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sections(data):
    rows = []
    rows.append(("KIRISH", data.get("introduction", ""), 1))
    for index, chapter in enumerate(data.get("chapters", []), 1):
        rows.append((f"{index}-BOB. {_clean(chapter.get('heading'))}", "", 1))
        for sub in chapter.get("sections", []):
            rows.append((_clean(sub.get("heading")), _clean(sub.get("body")), 2))
    rows.append(("XULOSA", data.get("conclusion", ""), 1))
    rows.append(("FOYDALANILGAN ADABIYOTLAR", "", 1))
    for index, item in enumerate(data.get("references", []), 1):
        rows.append((f"{index}. {_clean(item)}", "", 3))
    return rows


def _add_page_border(section):
    """Word titul sahifasi uchun sahifa chegarasini XML orqali o'rnatadi."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    sect_pr = section._sectPr
    borders = OxmlElement("w:pgBorders")
    borders.set(qn("w:offsetFrom"), "page")
    for edge in ("top", "left", "bottom", "right"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "12")
        element.set(qn("w:space"), "18")
        element.set(qn("w:color"), "1F4E79")
        borders.append(element)
    sect_pr.append(borders)


def build_referat_docx(data: dict, meta: dict | None = None, qr_module=None) -> bytes:
    meta = meta or {}
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(3)
    section.right_margin = Cm(1.5)
    _add_page_border(section)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(14)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.first_line_indent = Cm(1.25)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    # 1-sahifa: titul varag'i.
    for _ in range(2): doc.add_paragraph()
    for key in ("university", "faculty"):
        value = _clean(meta.get(key))
        if value:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(value.upper()); run.bold = True
    for _ in range(3): doc.add_paragraph()
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("REFERAT"); r.bold = True; r.font.size = Pt(20)
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(_clean(data.get("title"))); r.bold = True; r.font.size = Pt(16)
    for _ in range(3): doc.add_paragraph()
    for label, key in (("Bajardi:", "student_name"), ("Fakultet:", "faculty"), ("Guruh:", "group"), ("Fan:", "subject"), ("Tekshirdi:", "reviewer")):
        value = _clean(meta.get(key))
        if value:
            p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p.paragraph_format.first_line_indent = Cm(0)
            p.add_run(f"{label} {value}")
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    p.add_run("2026")

    # 2-sahifa: statik mundarija. Word'da maydon yangilash oynasi chiqmaydi.
    doc.add_page_break()
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    r = p.add_run("MUNDARIJA"); r.bold = True; r.font.size = Pt(16)
    toc = ["KIRISH"]
    for index, chapter in enumerate(data.get("chapters", []), 1):
        toc.append(f"{index}-BOB. {_clean(chapter.get('heading'))}")
        toc.extend(f"    {_clean(sub.get('heading'))}" for sub in chapter.get("sections", []))
    toc.extend(["XULOSA", "FOYDALANILGAN ADABIYOTLAR"])
    for item in toc:
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.line_spacing = 1.25
        p.add_run(item)

    # Har bir asosiy qism yangi sahifadan boshlanadi.
    doc.add_page_break()
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    r = p.add_run("KIRISH"); r.bold = True
    doc.add_paragraph(_clean(data.get("introduction")))

    for index, chapter in enumerate(data.get("chapters", []), 1):
        doc.add_page_break()
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Cm(0)
        r = p.add_run(f"{index}-BOB. {_clean(chapter.get('heading')).upper()}"); r.bold = True
        for sub in chapter.get("sections", []):
            p = doc.add_paragraph(); p.paragraph_format.first_line_indent = Cm(0)
            r = p.add_run(_clean(sub.get("heading"))); r.bold = True
            doc.add_paragraph(_clean(sub.get("body")))

    doc.add_page_break()
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    r = p.add_run("XULOSA"); r.bold = True
    doc.add_paragraph(_clean(data.get("conclusion")))

    doc.add_page_break()
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    r = p.add_run("FOYDALANILGAN ADABIYOTLAR"); r.bold = True
    for index, item in enumerate(data.get("references", []), 1):
        p = doc.add_paragraph(f"{index}. {_clean(item)}")
        p.paragraph_format.first_line_indent = Cm(0)

    if qr_module:
        try:
            qr_module.add_qr_to_docx(doc, feature="REFERAT")
        except Exception:
            logger.exception("Referat DOCX fayliga QR kod joylashda xatolik")
    output = io.BytesIO(); doc.save(output)
    return output.getvalue()


def build_referat_pdf(data: dict, meta: dict | None = None, qr_module=None) -> bytes:
    meta = meta or {}
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=1.8*cm, leftMargin=3*cm,
                            topMargin=2.2*cm, bottomMargin=2*cm)
    base = getSampleStyleSheet()
    title = ParagraphStyle("ReferatTitle", parent=base["Title"], fontName="Times-Bold", fontSize=18,
                           leading=24, alignment=TA_CENTER, textColor=colors.black, spaceAfter=16)
    heading = ParagraphStyle("ReferatHeading", parent=base["Heading2"], fontName="Times-Bold", fontSize=14,
                             leading=19, alignment=TA_CENTER, textColor=colors.black, spaceBefore=8, spaceAfter=12)
    subheading = ParagraphStyle("ReferatSubheading", parent=base["Heading3"], fontName="Times-Bold", fontSize=13,
                                leading=18, textColor=colors.black, spaceBefore=10, spaceAfter=6)
    body = ParagraphStyle("ReferatBody", parent=base["BodyText"], fontName="Times-Roman", fontSize=12,
                          leading=18, alignment=TA_JUSTIFY, firstLineIndent=1.25*cm, spaceAfter=8)
    plain = ParagraphStyle("ReferatPlain", parent=body, firstLineIndent=0)
    story = [Spacer(1, 2.0*cm)]
    for key in ("university", "faculty"):
        if _clean(meta.get(key)): story.append(Paragraph(escape(_clean(meta[key]).upper()), heading))
    story += [Spacer(1, 2.3*cm), Paragraph("REFERAT", title),
              Paragraph(escape(_clean(data.get("title"))), title), Spacer(1, 2.2*cm)]
    for label, key in (("Bajardi:", "student_name"), ("Fakultet:", "faculty"), ("Guruh:", "group"), ("Fan:", "subject"), ("Tekshirdi:", "reviewer")):
        if _clean(meta.get(key)):
            story.append(Paragraph(f"{escape(label)} {escape(_clean(meta[key]))}", plain))
    story += [Spacer(1, 1.5*cm), Paragraph("2026", heading), PageBreak(), Paragraph("MUNDARIJA", title)]
    toc = ["KIRISH"]
    for index, chapter in enumerate(data.get("chapters", []), 1):
        toc.append(f"{index}-BOB. {_clean(chapter.get('heading'))}")
        toc.extend("    " + _clean(sub.get("heading")) for sub in chapter.get("sections", []))
    toc.extend(["XULOSA", "FOYDALANILGAN ADABIYOTLAR"])
    for item in toc: story.append(Paragraph(escape(item), plain))
    story += [PageBreak(), Paragraph("KIRISH", heading), Paragraph(escape(_clean(data.get("introduction"))), body)]
    for index, chapter in enumerate(data.get("chapters", []), 1):
        story += [PageBreak(), Paragraph(escape(f"{index}-BOB. {_clean(chapter.get('heading')).upper()}"), heading)]
        for sub in chapter.get("sections", []):
            story.append(Paragraph(escape(_clean(sub.get("heading"))), subheading))
            story.append(Paragraph(escape(_clean(sub.get("body"))), body))
    story += [PageBreak(), Paragraph("XULOSA", heading), Paragraph(escape(_clean(data.get("conclusion"))), body),
              PageBreak(), Paragraph("FOYDALANILGAN ADABIYOTLAR", heading)]
    for index, item in enumerate(data.get("references", []), 1):
        story.append(Paragraph(escape(f"{index}. {_clean(item)}"), plain))
    doc.build(story)
    result = output.getvalue()
    if qr_module:
        try: result = qr_module.add_qr_to_pdf_bytes(result, feature="REFERAT")
        except Exception: logger.exception("Referat PDF fayliga QR kod joylashda xatolik")
    return result

