# -*- coding: utf-8 -*-
"""Referatni DOCX va PDF ko'rinishida avtomatik mundarija va titul ramkasi bilan tayyorlash."""
from __future__ import annotations

import io
import logging
import re
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepTogether,
)
from reportlab.platypus.tableofcontents import TableOfContents

logger = logging.getLogger(__name__)

NAVY = "1F3A5F"
GOLD = "C9A227"


def _clean(value):
    return re.sub(r"\\s+", " ", str(value or "")).strip()


def _add_page_border(section):
    """Word titul sahifasiga PDF bilan bir xil ikki rangli ramka qo'yadi.

    Ramka first-page header ichidagi sahifaga nisbatan joylashtirilgan VML
    shakllardan tuziladi; shu sababli u faqat titul sahifasida ko'rinadi.
    """
    sect_pr = section._sectPr
    old = sect_pr.find(qn("w:pgBorders"))
    if old is not None:
        sect_pr.remove(old)

    section.different_first_page_header_footer = True
    header = section.first_page_header
    paragraph = header.paragraphs[0]
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)

    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    # PDF bilan bir xil A4 ramka o'lchamlari va ranglari.
    shapes = [
        (0.85, 0.85, 19.30, 28.00, NAVY, 2.0),
        (1.05, 1.05, 18.90, 27.60, GOLD, 1.15),
    ]
    for x_cm, y_cm, w_cm, h_cm, color, width_pt in shapes:
        x_pt = x_cm * 72 / 2.54
        y_pt = y_cm * 72 / 2.54
        w_pt = w_cm * 72 / 2.54
        h_pt = h_cm * 72 / 2.54
        shape_xml = f'''<w:r {nsdecls("w")} xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
          <w:pict>
            <v:rect id="QuizAIFrame{color}" o:allowincell="f"
              style="position:absolute;left:0;top:0;margin-left:{x_pt:.2f}pt;margin-top:{y_pt:.2f}pt;width:{w_pt:.2f}pt;height:{h_pt:.2f}pt;z-index:-251654144;mso-position-horizontal-relative:page;mso-position-vertical-relative:page"
              filled="f" stroked="t" strokecolor="#{color}" strokeweight="{width_pt:.2f}pt">
              <v:fill color="#FFFFFF" opacity="0"/>
              <v:stroke color="#{color}" weight="{width_pt:.2f}pt"/>
            </v:rect>
          </w:pict>
        </w:r>'''
        paragraph._p.append(parse_xml(shape_xml))


def _add_toc_field(paragraph):
    """Word ochilganda mundarija sahifa raqamlarini yangilash uchun TOC maydoni."""
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = ' TOC \\\\o "1-2" \\\\h \\\\z \\\\u '
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "Mundarijani Word'da yangilash uchun maydon ustiga o'ng tugma bosing."
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for item in (begin, instr, separate, text, end):
        run._r.append(item)


def _set_update_fields(doc):
    settings = doc.settings._element
    update = settings.find(qn("w:updateFields"))
    if update is None:
        update = OxmlElement("w:updateFields")
        settings.append(update)
    update.set(qn("w:val"), "true")


def build_referat_docx(data: dict, meta: dict | None = None, qr_module=None) -> bytes:
    meta = meta or {}
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(3)
    section.right_margin = Cm(1.5)
    _add_page_border(section)
    _set_update_fields(doc)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(14)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.first_line_indent = Cm(1.25)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    for style_name in ("Heading 1", "Heading 2"):
        style = doc.styles[style_name]
        style.font.name = "Times New Roman"
        style.font.color.rgb = RGBColor(0, 0, 0)

    # 1-sahifa: titul varag'i.
    for _ in range(2):
        doc.add_paragraph()
    for key in ("university", "faculty"):
        value = _clean(meta.get(key))
        if value:
            p = doc.add_paragraph(style="Heading 2")
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Cm(0)
            run = p.add_run(value.upper())
            run.bold = True
    for _ in range(3):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    r = p.add_run("REFERAT")
    r.bold = True
    r.font.size = Pt(20)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    r = p.add_run(_clean(data.get("title")))
    r.bold = True
    r.font.size = Pt(16)
    for _ in range(3):
        doc.add_paragraph()
    for label, key in (("Bajardi:", "student_name"), ("Fakultet:", "faculty"),
                       ("Guruh:", "group"), ("Fan:", "subject"), ("Tekshirdi:", "reviewer")):
        value = _clean(meta.get(key))
        if value:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p.paragraph_format.first_line_indent = Cm(0)
            p.add_run(f"{label} {value}")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    p.add_run("2026")

    # 2-sahifa: Word maydonli avtomatik mundarija.
    doc.add_page_break()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    r = p.add_run("MUNDARIJA")
    r.bold = True
    r.font.size = Pt(16)
    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0)
    _add_toc_field(p)

    # Mundarijada ko'rinadigan bo'limlar Heading uslublari bilan belgilangan.
    doc.add_page_break()
    p = doc.add_paragraph("KIRISH", style="Heading 1")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    doc.add_paragraph(_clean(data.get("introduction")))

    for index, chapter in enumerate(data.get("chapters", []), 1):
        doc.add_page_break()
        p = doc.add_paragraph(f"{index}-BOB. {_clean(chapter.get('heading')).upper()}", style="Heading 1")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Cm(0)
        for sub in chapter.get("sections", []):
            p = doc.add_paragraph(_clean(sub.get("heading")), style="Heading 2")
            p.paragraph_format.first_line_indent = Cm(0)
            doc.add_paragraph(_clean(sub.get("body")))

    doc.add_page_break()
    p = doc.add_paragraph("XULOSA", style="Heading 1")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    doc.add_paragraph(_clean(data.get("conclusion")))

    doc.add_page_break()
    p = doc.add_paragraph("FOYDALANILGAN ADABIYOTLAR", style="Heading 1")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    for index, item in enumerate(data.get("references", []), 1):
        p = doc.add_paragraph(f"{index}. {_clean(item)}")
        p.paragraph_format.first_line_indent = Cm(0)

    if qr_module:
        try:
            qr_module.add_qr_to_docx(doc, feature="REFERAT")
        except Exception:
            logger.exception("Referat DOCX fayliga QR kod joylashda xatolik")
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


class _ReferatPDFTemplate(SimpleDocTemplate):
    """Mundarija uchun sarlavhalarning haqiqiy sahifa raqamlarini yig'adi."""
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and getattr(flowable, "_toc_level", None) is not None:
            text = flowable.getPlainText()
            key = "ref-%s-%s" % (flowable._toc_level, abs(hash(text)))
            self.canv.bookmarkPage(key)
            self.notify("TOCEntry", (flowable._toc_level, text, self.page, key))


def _draw_title_frame(canvas, doc):
    """Faqat birinchi PDF sahifasida ko'k-oltin ikki chiziqli ramka."""
    if canvas.getPageNumber() != 1:
        return
    width, height = A4
    canvas.saveState()
    canvas.setLineWidth(2.0)
    canvas.setStrokeColor(colors.HexColor("#" + NAVY))
    canvas.rect(0.85 * cm, 0.85 * cm, width - 1.7 * cm, height - 1.7 * cm)
    canvas.setLineWidth(1.15)
    canvas.setStrokeColor(colors.HexColor("#" + GOLD))
    canvas.rect(1.05 * cm, 1.05 * cm, width - 2.1 * cm, height - 2.1 * cm)
    canvas.restoreState()


def build_referat_pdf(data: dict, meta: dict | None = None, qr_module=None) -> bytes:
    meta = meta or {}
    output = io.BytesIO()
    doc = _ReferatPDFTemplate(
        output, pagesize=A4, rightMargin=1.8 * cm, leftMargin=3 * cm,
        topMargin=2.2 * cm, bottomMargin=2 * cm,
        title="Quiz AI Referat", author="Quiz AI",
    )
    base = getSampleStyleSheet()
    title = ParagraphStyle("ReferatTitle", parent=base["Title"], fontName="Times-Bold",
                           fontSize=18, leading=24, alignment=TA_CENTER,
                           textColor=colors.black, spaceAfter=16)
    heading = ParagraphStyle("ReferatHeading", parent=base["Heading2"], fontName="Times-Bold",
                             fontSize=14, leading=19, alignment=TA_CENTER,
                             textColor=colors.black, spaceBefore=8, spaceAfter=12)
    subheading = ParagraphStyle("ReferatSubheading", parent=base["Heading3"], fontName="Times-Bold",
                                fontSize=13, leading=18, textColor=colors.black,
                                spaceBefore=10, spaceAfter=6)
    body = ParagraphStyle("ReferatBody", parent=base["BodyText"], fontName="Times-Roman",
                          fontSize=12, leading=18, alignment=TA_JUSTIFY,
                          firstLineIndent=1.25 * cm, spaceAfter=8)
    plain = ParagraphStyle("ReferatPlain", parent=body, firstLineIndent=0)
    toc1 = ParagraphStyle("ReferatTOC1", parent=plain, fontName="Times-Bold",
                          leftIndent=0, spaceBefore=5, spaceAfter=3)
    toc2 = ParagraphStyle("ReferatTOC2", parent=plain, leftIndent=18,
                          fontSize=11, leading=15, spaceAfter=2)
    toc = TableOfContents()
    toc.levelStyles = [toc1, toc2]
    story = [Spacer(1, 2.0 * cm)]
    for key in ("university", "faculty"):
        if _clean(meta.get(key)):
            story.append(Paragraph(escape(_clean(meta[key]).upper()), heading))
    story += [Spacer(1, 2.3 * cm), Paragraph("REFERAT", title),
              Paragraph(escape(_clean(data.get("title"))), title), Spacer(1, 2.2 * cm)]
    for label, key in (("Bajardi:", "student_name"), ("Fakultet:", "faculty"),
                       ("Guruh:", "group"), ("Fan:", "subject"), ("Tekshirdi:", "reviewer")):
        if _clean(meta.get(key)):
            story.append(Paragraph(f"{escape(label)} {escape(_clean(meta[key]))}", plain))
    story += [Spacer(1, 1.5 * cm), Paragraph("2026", heading),
              PageBreak(), Paragraph("MUNDARIJA", title), toc]

    def indexed(text, level, style):
        p = Paragraph(escape(text), style)
        p._toc_level = level
        return p

    story += [PageBreak(), indexed("KIRISH", 0, heading),
              Paragraph(escape(_clean(data.get("introduction"))), body)]
    for index, chapter in enumerate(data.get("chapters", []), 1):
        story += [PageBreak(), indexed(f"{index}-BOB. {_clean(chapter.get('heading')).upper()}", 0, heading)]
        for sub in chapter.get("sections", []):
            story.append(indexed(_clean(sub.get("heading")), 1, subheading))
            story.append(Paragraph(escape(_clean(sub.get("body"))), body))
    story += [PageBreak(), indexed("XULOSA", 0, heading),
              Paragraph(escape(_clean(data.get("conclusion"))), body),
              PageBreak(), indexed("FOYDALANILGAN ADABIYOTLAR", 0, heading)]
    for index, item in enumerate(data.get("references", []), 1):
        story.append(Paragraph(escape(f"{index}. {_clean(item)}"), plain))

    doc.multiBuild(story, onFirstPage=_draw_title_frame, onLaterPages=lambda canvas, d: None)
    result = output.getvalue()
    if qr_module:
        try:
            result = qr_module.add_qr_to_pdf_bytes(result, feature="REFERAT")
        except Exception:
            logger.exception("Referat PDF fayliga QR kod joylashda xatolik")
    return result
