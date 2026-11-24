# -*- coding: utf-8 -*-
"""Referatni DOCX va PDF ko'rinishida avtomatik mundarija va titul ramkasi bilan tayyorlash."""
from __future__ import annotations

import io
import logging
import re
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.shared import Cm, Pt, RGBColor, Inches
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
from pypdf import PdfReader

logger = logging.getLogger(__name__)

NAVY = "1F3A5F"
GOLD = "C9A227"


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


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



def _normalize_heading(value: str) -> str:
    """Sarlavhani PDF matnidan izlash uchun harflar/raqamlarga keltiradi."""
    return re.sub(r"[^0-9a-zа-яёўғқҳ]", "", str(value or "").casefold())


def _chapter_title(index: int, raw_title: str) -> str:
    """AI sarlavhasidagi mavjud bob raqamini olib, yagona raqam qo'shadi."""
    title = _clean(raw_title)
    title = re.sub(r"^\s*(?:(?:\d+\s*[-.)]\s*BOB\.?\s*)+)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^\s*BOB\s*\d+\s*[-.)]?\s*", "", title, flags=re.IGNORECASE)
    return f"{index}-BOB. {title.upper()}"


def extract_referat_toc_pages(pdf_bytes: bytes, data: dict) -> dict:
    """Yaratilgan PDF sahifalaridan mundarija uchun haqiqiy sahifa raqamlarini oladi."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_texts = [(page.extract_text() or "") for page in reader.pages]
        normalized_pages = [_normalize_heading(text) for text in page_texts]

        entries = [("KIRISH", "KIRISH")]
        for index, chapter in enumerate(data.get("chapters", []), 1):
            raw_heading = _clean(chapter.get("heading"))
            chapter_label = _chapter_title(index, raw_heading)
            entries.append((chapter_label, chapter_label))
            for section in chapter.get("sections", []):
                section_heading = _clean(section.get("heading"))
                entries.append((section_heading, section_heading))
        entries.append(("XULOSA", "XULOSA"))
        entries.append(("FOYDALANILGAN ADABIYOTLAR", "FOYDALANILGAN ADABIYOTLAR"))

        result = {}
        cursor = min(2, max(0, len(normalized_pages) - 1))
        for display, search_text in entries:
            needle = _normalize_heading(search_text)
            found = None
            if needle:
                for page_index in range(cursor, len(normalized_pages)):
                    if needle in normalized_pages[page_index]:
                        found = page_index + 1
                        cursor = page_index
                        break
            result[display] = found if found is not None else "—"
        return result
    except Exception:
        logger.exception("PDF sahifalaridan referat mundarija raqamlarini olishda xatolik")
        return {}


def _add_static_toc_entry(doc, label: str, page_number, *, level: int = 0):
    """Word mundarijasiga tashqi yoki yangilanadigan maydonsiz tayyor satr qo'shadi."""
    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0)
    p.paragraph_format.left_indent = Cm(0.65 if level else 0)
    p.paragraph_format.space_before = Pt(2 if level == 0 else 0)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.0
    p.paragraph_format.tab_stops.add_tab_stop(
        Inches(6.25), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS
    )
    run = p.add_run(label)
    run.font.name = "Times New Roman"
    run.font.size = Pt(12 if level == 0 else 11)
    if level == 0:
        run.bold = True
    page_run = p.add_run("\t" + str(page_number or "—"))
    page_run.font.name = "Times New Roman"
    page_run.font.size = Pt(12)
    return p


def _add_fixed_qr_to_docx(doc, qr_module):
    """PDF bilan bir xil PNG QR kodini Word oxirgi sahifasiga aniq koordinatada joylaydi."""
    if not qr_module:
        return False
    try:
        config = qr_module.get_branding_config("REFERAT")
        png = qr_module._make_qr_png(config)
        if not png:
            return False

        size_pt = float(config.get("size", 84))
        margin_pt = 22.0
        page_width_pt = float(A4[0])
        page_height_pt = float(A4[1])
        position = str(config.get("position", "bottom_right")).lower()

        if position == "top_left":
            x_pt, y_pt = margin_pt, margin_pt
        elif position == "top_right":
            x_pt, y_pt = page_width_pt - margin_pt - size_pt, margin_pt
        elif position == "bottom_left":
            x_pt, y_pt = margin_pt, page_height_pt - margin_pt - size_pt
        elif position == "center":
            x_pt = (page_width_pt - size_pt) / 2
            y_pt = (page_height_pt - size_pt) / 2
        else:  # bottom_right va document_end
            x_pt = page_width_pt - margin_pt - size_pt
            y_pt = page_height_pt - margin_pt - size_pt

        # Rasm oxirgi matn paragrafi bilan bog'lanadi, lekin sahifaga nisbatan
        # mutlaq koordinatada turadi; shu bois matn oqimini surmaydi.
        paragraph = doc.paragraphs[-1] if doc.paragraphs else doc.add_paragraph()
        run = paragraph.add_run()
        picture = run.add_picture(io.BytesIO(png), width=Pt(size_pt))
        inline = picture._inline

        anchor = OxmlElement("wp:anchor")
        for key, value in {
            "distT": "0", "distB": "0", "distL": "0", "distR": "0",
            "simplePos": "0", "relativeHeight": "251658240",
            "behindDoc": "0", "locked": "0", "layoutInCell": "1",
            "allowOverlap": "1",
        }.items():
            anchor.set(key, value)

        simple = OxmlElement("wp:simplePos")
        simple.set("x", "0")
        simple.set("y", "0")

        pos_h = OxmlElement("wp:positionH")
        pos_h.set("relativeFrom", "page")
        offset_h = OxmlElement("wp:posOffset")
        offset_h.text = str(int(x_pt * 12700))
        pos_h.append(offset_h)

        pos_v = OxmlElement("wp:positionV")
        pos_v.set("relativeFrom", "page")
        offset_v = OxmlElement("wp:posOffset")
        offset_v.text = str(int(y_pt * 12700))
        pos_v.append(offset_v)

        extent = inline.find(qn("wp:extent"))
        doc_pr = inline.find(qn("wp:docPr"))
        frame_pr = inline.find(qn("wp:cNvGraphicFramePr"))
        graphic = inline.find(qn("a:graphic"))

        effect = OxmlElement("wp:effectExtent")
        for side in ("l", "t", "r", "b"):
            effect.set(side, "0")
        wrap = OxmlElement("wp:wrapNone")

        for child in (simple, pos_h, pos_v, extent, effect, wrap, doc_pr, frame_pr, graphic):
            if child is not None:
                anchor.append(child)

        inline.getparent().replace(inline, anchor)
        return True
    except Exception:
        logger.exception("Referat DOCX fayliga QR kodni PDF bilan bir xil joylashtirishda xatolik")
        return False



def build_referat_docx(data: dict, meta: dict | None = None, qr_module=None,
                       toc_pages: dict | None = None) -> bytes:
    """PDF uslubiga mos DOCX; mundarija tayyor matn bo'lib, Word maydonlarini ishlatmaydi."""
    meta = meta or {}
    toc_pages = toc_pages or {}
    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(3)
    section.right_margin = Cm(1.8)
    _add_page_border(section)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.first_line_indent = Cm(1.25)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    normal.paragraph_format.space_after = Pt(8)

    h1 = doc.styles["Heading 1"]
    h1.font.name = "Times New Roman"
    h1.font.size = Pt(14)
    h1.font.bold = True
    h1.font.color.rgb = RGBColor(0, 0, 0)
    h1.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    h1.paragraph_format.first_line_indent = Cm(0)
    h1.paragraph_format.line_spacing = Pt(19)
    h1.paragraph_format.space_before = Pt(8)
    h1.paragraph_format.space_after = Pt(12)
    h1.paragraph_format.keep_with_next = True

    h2 = doc.styles["Heading 2"]
    h2.font.name = "Times New Roman"
    h2.font.size = Pt(13)
    h2.font.bold = True
    h2.font.color.rgb = RGBColor(0, 0, 0)
    h2.paragraph_format.first_line_indent = Cm(0)
    h2.paragraph_format.line_spacing = Pt(18)
    h2.paragraph_format.space_before = Pt(10)
    h2.paragraph_format.space_after = Pt(6)
    h2.paragraph_format.keep_with_next = False

    # 1-sahifa: PDF bilan bir xil mazmundagi titul.
    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0)
    p.paragraph_format.line_spacing = Pt(57)
    p.paragraph_format.space_after = Pt(0)

    for key in ("university", "faculty"):
        value = _clean(meta.get(key))
        if value:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Cm(0)
            p.paragraph_format.space_after = Pt(12)
            run = p.add_run(value.upper())
            run.bold = True
            run.font.name = "Times New Roman"
            run.font.size = Pt(14)

    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0)
    p.paragraph_format.line_spacing = Pt(65)
    p.paragraph_format.space_after = Pt(0)

    for label in ("REFERAT", _clean(data.get("title"))):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.space_after = Pt(8)
        run = p.add_run(label)
        run.bold = True
        run.font.name = "Times New Roman"
        run.font.size = Pt(18)

    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0)
    p.paragraph_format.line_spacing = Pt(62)
    p.paragraph_format.space_after = Pt(0)

    for label, key in (("Bajardi:", "student_name"), ("Fakultet:", "faculty"),
                       ("Guruh:", "group"), ("Fan:", "subject"), ("Tekshirdi:", "reviewer")):
        value = _clean(meta.get(key))
        if value:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p.paragraph_format.first_line_indent = Cm(0)
            p.paragraph_format.space_after = Pt(4)
            p.add_run(f"{label} {value}")

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    p.paragraph_format.space_before = Pt(34)
    year_run = p.add_run("2026")
    year_run.font.name = "Times New Roman"
    year_run.font.size = Pt(14)
    year_run.bold = True

    # 2-sahifa: statik mundarija. TOC/FIELD/updateFields ishlatilmaydi.
    doc.add_page_break()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    p.paragraph_format.space_after = Pt(18)
    run = p.add_run("MUNDARIJA")
    run.bold = True
    run.font.name = "Times New Roman"
    run.font.size = Pt(16)

    _add_static_toc_entry(doc, "KIRISH", toc_pages.get("KIRISH", 3))
    for index, chapter in enumerate(data.get("chapters", []), 1):
        chapter_label = _chapter_title(index, chapter.get("heading"))
        _add_static_toc_entry(doc, chapter_label, toc_pages.get(chapter_label, "—"))
        for sub in chapter.get("sections", []):
            subheading = _clean(sub.get("heading"))
            _add_static_toc_entry(doc, subheading, toc_pages.get(subheading, "—"), level=1)
    _add_static_toc_entry(doc, "XULOSA", toc_pages.get("XULOSA", "—"))
    _add_static_toc_entry(
        doc, "FOYDALANILGAN ADABIYOTLAR",
        toc_pages.get("FOYDALANILGAN ADABIYOTLAR", "—")
    )

    # 3-sahifadan boshlab asosiy matn.
    p = doc.add_paragraph("KIRISH", style="Heading 1")
    p.paragraph_format.page_break_before = True
    p.paragraph_format.keep_with_next = True
    doc.add_paragraph(_clean(data.get("introduction")))

    for index, chapter in enumerate(data.get("chapters", []), 1):
        p = doc.add_paragraph(_chapter_title(index, chapter.get("heading")), style="Heading 1")
        p.paragraph_format.page_break_before = True
        p.paragraph_format.keep_with_next = True
        for sub in chapter.get("sections", []):
            p = doc.add_paragraph(_clean(sub.get("heading")), style="Heading 2")
            p.paragraph_format.keep_with_next = True
            doc.add_paragraph(_clean(sub.get("body")))

    p = doc.add_paragraph("XULOSA", style="Heading 1")
    p.paragraph_format.page_break_before = True
    doc.add_paragraph(_clean(data.get("conclusion")))

    p = doc.add_paragraph("FOYDALANILGAN ADABIYOTLAR", style="Heading 1")
    p.paragraph_format.page_break_before = True
    for index, item in enumerate(data.get("references", []), 1):
        p = doc.add_paragraph(f"{index}. {_clean(item)}")
        p.paragraph_format.first_line_indent = Cm(0)

    if qr_module:
        _add_fixed_qr_to_docx(doc, qr_module)

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
        story += [PageBreak(), indexed(_chapter_title(index, chapter.get("heading")), 0, heading)]
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
