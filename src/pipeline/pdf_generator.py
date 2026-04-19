"""判決書 PDF 生成 — 仿司法院格式。

使用 reportlab 生成 PDF，宋體排版，結構化段落（主文/事實/理由）。
"""
import io
import re
import logging
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logger = logging.getLogger(__name__)

# ── 字體註冊 ──
_FONT_REGISTERED = False

def _ensure_fonts():
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return
    # macOS 宋體
    songti_path = Path("/System/Library/Fonts/Supplemental/Songti.ttc")
    if songti_path.exists():
        pdfmetrics.registerFont(TTFont("Songti", str(songti_path), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("Songti-Bold", str(songti_path), subfontIndex=1))
    else:
        # Fallback: 嘗試 Noto Serif TC（Linux/Docker）
        noto_path = Path("/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc")
        if noto_path.exists():
            pdfmetrics.registerFont(TTFont("Songti", str(noto_path), subfontIndex=0))
            pdfmetrics.registerFont(TTFont("Songti-Bold", str(noto_path), subfontIndex=0))
        else:
            logger.warning("找不到中文字體，PDF 可能無法正確顯示中文")
            return
    _FONT_REGISTERED = True


# ── 段落偵測 ──
_CJK_UPPER = re.compile(r'^[壹貳參肆伍陸柒捌玖拾甲乙丙丁戊己庚辛壬癸]+\s*[、.,．]')
_CJK_NUM = re.compile(r'^[一二三四五六七八九十百零〇]+\s*[、.,．]')
_PAREN_NUM = re.compile(r'^[（(]\s*[一二三四五六七八九十百零〇]+\s*[）)]')
_ENCLOSED = re.compile(r'^[\u3220-\u3229]')
_SECTION_HEADER = re.compile(r'^[主事理][\s\u3000]*[文實由][\s\u3000]*$')


def _detect_level(text):
    t = text.lstrip()
    if not t:
        return None
    if _SECTION_HEADER.match(t):
        return -1
    if _CJK_UPPER.match(t):
        return 0
    if _CJK_NUM.match(t):
        return 1
    if _ENCLOSED.match(t) or _PAREN_NUM.match(t):
        return 2
    return None


def _merge_paragraphs(text):
    """合併硬性斷行，在層級標記處分段。回傳 [(level, text), ...]"""
    if not text:
        return []
    lines = text.split('\n')
    paragraphs = []
    current = None

    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            if current:
                paragraphs.append(current)
                current = None
            continue

        level = _detect_level(trimmed)
        if level is not None:
            if current:
                paragraphs.append(current)
            if level == -1:
                # Section header 獨立
                paragraphs.append((level, trimmed.replace('\u3000', '').replace(' ', '')))
                current = None
            else:
                current = (level, trimmed)
        elif current:
            # 合併續行
            last_char = current[1][-1] if current[1] else ''
            first_char = trimmed[0] if trimmed else ''
            need_space = last_char.isascii() and last_char.isalnum() and first_char.isascii() and first_char.isalnum()
            current = (current[0], current[1] + (' ' if need_space else '') + trimmed)
        else:
            current = (None, trimmed)

    if current:
        paragraphs.append(current)
    return paragraphs


def generate_judgment_pdf(judgment: dict) -> bytes:
    """從 judgment dict 生成 PDF bytes。"""
    _ensure_fonts()

    buf = io.BytesIO()
    font_name = "Songti" if _FONT_REGISTERED else "Helvetica"
    font_bold = "Songti-Bold" if _FONT_REGISTERED else "Helvetica-Bold"

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=25 * mm,
        bottomMargin=25 * mm,
        leftMargin=25 * mm,
        rightMargin=25 * mm,
    )

    # ── Styles ──
    style_title = ParagraphStyle(
        'Title', fontName=font_bold, fontSize=16, leading=24,
        alignment=1,  # center
        spaceAfter=6 * mm,
    )
    style_meta = ParagraphStyle(
        'Meta', fontName=font_name, fontSize=11, leading=16,
        spaceAfter=2 * mm,
    )
    style_section = ParagraphStyle(
        'Section', fontName=font_bold, fontSize=13, leading=20,
        spaceBefore=8 * mm, spaceAfter=4 * mm,
    )
    style_level0 = ParagraphStyle(
        'Level0', fontName=font_bold, fontSize=11, leading=18,
        spaceBefore=4 * mm, spaceAfter=1 * mm,
    )
    style_level1 = ParagraphStyle(
        'Level1', fontName=font_name, fontSize=11, leading=18,
        leftIndent=8 * mm,
        spaceBefore=2 * mm, spaceAfter=1 * mm,
    )
    style_level2 = ParagraphStyle(
        'Level2', fontName=font_name, fontSize=11, leading=18,
        leftIndent=16 * mm,
        spaceBefore=1 * mm, spaceAfter=1 * mm,
    )
    style_body = ParagraphStyle(
        'Body', fontName=font_name, fontSize=11, leading=18,
        leftIndent=4 * mm,
        spaceBefore=1 * mm, spaceAfter=1 * mm,
    )

    # ── Build content ──
    story = []

    # 裁判字號（標題）
    case_id = (judgment.get("case_id") or "").replace('\u3000', ' ').strip()
    story.append(Paragraph(case_id, style_title))

    # Metadata
    court = judgment.get("court") or ""
    date = judgment.get("date") or ""
    if court:
        story.append(Paragraph(f"法院：{court}", style_meta))
    if date:
        story.append(Paragraph(f"裁判日期：{date}", style_meta))

    story.append(Spacer(1, 4 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color="#666666"))
    story.append(Spacer(1, 4 * mm))

    # 組合全文：主文 + 事實 + 理由
    sections = []
    if judgment.get("main_text"):
        sections.append(("主文", judgment["main_text"]))
    if judgment.get("facts"):
        sections.append(("事實", judgment["facts"]))
    if judgment.get("reasoning"):
        sections.append(("理由", judgment["reasoning"]))

    # Fallback to full_text
    if not sections and judgment.get("full_text"):
        sections.append(("", judgment["full_text"]))

    for section_name, text in sections:
        if section_name:
            story.append(Paragraph(section_name, style_section))

        paras = _merge_paragraphs(text)
        for level, content in paras:
            # Escape XML special chars for reportlab
            safe = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            if level == -1:
                story.append(Paragraph(safe, style_section))
            elif level == 0:
                story.append(Paragraph(safe, style_level0))
            elif level == 1:
                story.append(Paragraph(safe, style_level1))
            elif level == 2:
                story.append(Paragraph(safe, style_level2))
            else:
                story.append(Paragraph(safe, style_body))

    doc.build(story)
    return buf.getvalue()
