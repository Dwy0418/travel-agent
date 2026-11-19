"""行程单 PDF 导出：fpdf2 + 内嵌 Noto Sans SC（SIL OFL 1.1）。

为什么用 fpdf2 而不是 WeasyPrint / pdfkit：
- 纯 Python 依赖（Pillow / fonttools），不依赖系统级 libcairo / pango，
  Streamlit Community Cloud 的构建环境可以直接 pip 装上。
- 原生支持 TrueType / OpenType 子集化嵌入，8.3MB 的中文字体只会嵌入
  实际用到的字形，一份 3 天行程单 PDF 通常在 100KB 量级。
- 页眉页脚、分页、表格行不跨页这些排版能力都是内置的，不用起浏览器。

字体来源：Noto Sans SC Regular（notofonts/noto-cjk 的 SubsetOTF），
版权 © 2014-2021 Adobe，SIL Open Font License 1.1，允许随软件再分发，
许可证全文见同目录 OFL-NotoCJK.txt。
"""
from __future__ import annotations

import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import requests
from fpdf import FPDF
from fpdf.enums import TableBordersLayout
from fpdf.fonts import FontFace
from PIL import Image

from . import tools, zh_labels

_logger = logging.getLogger("travel_agent.detail")

ASSET_DIR = Path(__file__).resolve().parent / "assets"
FONT_PATH = ASSET_DIR / "NotoSansSC-Regular.otf"
APP_NAME = "TRAVEL AGENT · AI 旅行规划师"

PHOTO_TIMEOUT = (3, 8)          # 连接 3s / 读取 8s：慢图不拖死整份 PDF
PHOTO_MAX_BYTES = 3 * 1024 * 1024
PHOTO_MAX_WIDTH = 520
PHOTOS_PER_DAY = 3
PHOTO_WORKERS = 5

NAVY = (30, 41, 59)
ACCENT = (13, 116, 144)
GRAY = (100, 116, 139)
LIGHT = (241, 245, 249)
LINE = (226, 232, 240)


# --------------------------------------------------------------------------- #
# 文本 / 图片工具
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> str:
    """去掉 emoji（非 BMP）与换行，避免字体缺字或表格串行。"""
    text = str(value or "")
    text = "".join(ch for ch in text if ord(ch) <= 0xFFFF)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return re.sub(r"\s{2,}", " ", text).strip()


def _money(value: Any, free: str = "免费") -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount <= 0 and free:
        return free
    return f"¥{amount:,.0f}"


def _fit(pdf: FPDF, text: str, width: float) -> str:
    """按当前字号把文字截到宽度内，末尾补省略号。"""
    text = str(text or "")
    if pdf.get_string_width(text) <= width:
        return text
    ellipsis = "…"
    while text and pdf.get_string_width(text + ellipsis) > width:
        text = text[:-1]
    return (text + ellipsis) if text else ellipsis


def _fetch_photo(url: str, aspect: float = 1.5) -> bytes | None:
    """服务端取高德图，超时 / 非图片 / 过大一律返回 None 走占位图。"""
    url = str(url or "")
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        resp = requests.get(
            url,
            timeout=PHOTO_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (TravelAgent PDF)"},
            stream=True,
        )
        if resp.status_code != 200:
            return None
        chunks: list[bytes] = []
        size = 0
        for chunk in resp.iter_content(64 * 1024):
            size += len(chunk)
            if size > PHOTO_MAX_BYTES:
                return None
            chunks.append(chunk)
    except requests.RequestException as exc:
        _logger.info("pdf_photo_failed %s %s", url, exc)
        return None
    try:
        image = Image.open(io.BytesIO(b"".join(chunks)))
        image.load()
        image = image.convert("RGB")
    except Exception:  # noqa: BLE001 - 坏图直接占位
        return None
    ratio = PHOTO_MAX_WIDTH / max(image.width, 1)
    if ratio < 1:
        image = image.resize((PHOTO_MAX_WIDTH, max(1, int(image.height * ratio))), Image.LANCZOS)
    # 按版面格子的宽高比裁切，避免图片两侧留白或被拉伸
    target_h = int(round(image.width / max(aspect, 0.2)))
    if image.height > target_h:
        top = (image.height - target_h) // 2
        image = image.crop((0, top, image.width, top + target_h))
    else:
        target_w = int(round(image.height * aspect))
        if target_w < image.width:
            left = (image.width - target_w) // 2
            image = image.crop((left, 0, left + target_w, image.height))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=72, optimize=True)
    return buffer.getvalue()


def _collect_photos(days: list[dict[str, Any]]) -> dict[int, list[tuple[str, str]]]:
    """每天最多取 3 张景点实拍图，返回 {day_index: [(景点名, url)]}。"""
    picked: dict[int, list[tuple[str, str]]] = {}
    seen: set[str] = set()
    for index, day in enumerate(days):
        slots: list[tuple[str, str]] = []
        for item in day.get("items") or []:
            if item.get("type") != "attraction":
                continue
            for url in item.get("photos") or []:
                url = str(url or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                slots.append((_clean(item.get("name")), url))
                break
            if len(slots) >= PHOTOS_PER_DAY:
                break
        if slots:
            picked[index] = slots
    return picked


# --------------------------------------------------------------------------- #
# 版式
# --------------------------------------------------------------------------- #
class _ItineraryPDF(FPDF):
    """A4 竖版：页眉放应用名 + 行程标题，页脚放页码与免责声明。"""

    def __init__(self, doc_title: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.doc_title = doc_title
        self.set_margins(14, 20, 14)
        self.set_auto_page_break(auto=True, margin=20)
        self.alias_nb_pages()
        self.add_font("noto", "", str(FONT_PATH))
        self.set_font("noto", size=10)
        self.set_text_color(*NAVY)

    def header(self) -> None:  # noqa: D102 - fpdf2 钩子
        self.set_font("noto", size=8)
        self.set_text_color(*GRAY)
        self.set_xy(self.l_margin, 8)
        self.cell(self.epw * 0.45, 5, APP_NAME, align="L")
        self.set_xy(self.l_margin + self.epw * 0.45, 8)
        self.cell(self.epw * 0.55, 5, _fit(self, self.doc_title, self.epw * 0.55), align="R")
        self.set_draw_color(*LINE)
        self.set_line_width(0.3)
        self.line(self.l_margin, 14.5, self.w - self.r_margin, 14.5)
        self.set_y(20)
        self.set_text_color(*NAVY)

    def footer(self) -> None:  # noqa: D102 - fpdf2 钩子
        self.set_y(-16)
        self.set_draw_color(*LINE)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.h - 16, self.w - self.r_margin, self.h - 16)
        self.set_y(-13)
        self.set_font("noto", size=8)
        self.set_text_color(*GRAY)
        self.cell(self.epw * 0.72, 5, "本行程单由 AI 规划生成，营业时间与票价请以景区当日公告为准", align="L")
        self.cell(self.epw * 0.28, 5, f"第 {self.page_no()} / {{nb}} 页", align="R")
        self.set_text_color(*NAVY)


def _section_title(pdf: _ItineraryPDF, title: str) -> None:
    if pdf.will_page_break(16):
        pdf.add_page()
    pdf.ln(1)
    pdf.set_font("noto", size=12)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.9)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 18, pdf.get_y())
    pdf.ln(2.5)
    pdf.set_text_color(*NAVY)


def _title_block(pdf: _ItineraryPDF, result: dict[str, Any], city: str, n_days: int) -> None:
    itinerary = result.get("itinerary") or {}
    pdf.set_font("noto", size=22)
    pdf.set_text_color(*NAVY)
    pdf.multi_cell(0, 11, f"{city} {n_days} 天行程单", new_x="LMARGIN", new_y="NEXT")
    summary = _clean(itinerary.get("summary"))
    if summary:
        pdf.set_font("noto", size=10)
        pdf.set_text_color(*GRAY)
        pdf.multi_cell(0, 6, summary, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_text_color(*NAVY)


def _overview_cards(pdf: _ItineraryPDF, c: dict[str, Any], budget: dict[str, Any], days: list[dict[str, Any]]) -> None:
    companions = "、".join(c.get("companions") or []) or "无"
    preferences = _clean(c.get("preferences")) or "无特殊偏好"
    cards = [
        ("目的地", _clean(c.get("city"))),
        ("出发日期", _clean(c.get("start_date")) or str(date.today())),
        ("行程天数", f"{int(c.get('days') or len(days) or 1)} 天"),
        ("出行人数", f"{int(c.get('people') or 1)} 人"),
        ("预算上限", _money(c.get("budget"), free="¥0")),
        ("预计花费", _money(budget.get("total"), free="¥0")),
        ("同行人", companions),
        ("行程偏好", preferences),
    ]
    gap = 4.0
    box_w = (pdf.epw - gap * 3) / 4
    box_h = 16.0
    for row in range(2):
        x = pdf.l_margin
        y = pdf.get_y()
        for col in range(4):
            label, value = cards[row * 4 + col]
            pdf.set_draw_color(*LINE)
            pdf.set_fill_color(*LIGHT)
            pdf.set_line_width(0.25)
            pdf.rect(x, y, box_w, box_h, style="DF")
            pdf.set_xy(x + 3, y + 2)
            pdf.set_font("noto", size=7.5)
            pdf.set_text_color(*GRAY)
            pdf.cell(box_w - 6, 4.5, label)
            pdf.set_xy(x + 3, y + 7.5)
            pdf.set_font("noto", size=11)
            pdf.set_text_color(*NAVY)
            pdf.cell(box_w - 6, 6, _fit(pdf, value, box_w - 6))
            x += box_w + gap
        pdf.set_y(y + box_h + gap)
    pdf.set_text_color(*NAVY)


def _day_header(pdf: _ItineraryPDF, index: int, day: dict[str, Any], need: float = 50.0) -> None:
    # 必须把「日头 + 首图 + 至少两行表格」一起留在同一页，避免标题落在页脚
    if pdf.will_page_break(need):
        pdf.add_page()
    y = pdf.get_y()
    pdf.set_fill_color(*NAVY)
    pdf.rect(pdf.l_margin, y, pdf.epw, 9, style="F")
    pdf.set_xy(pdf.l_margin + 3, y + 1.6)
    pdf.set_font("noto", size=11)
    pdf.set_text_color(255, 255, 255)
    day_date = _clean(day.get("date"))
    pdf.cell(pdf.epw * 0.4, 6, f"第 {index} 天" + (f" · {day_date}" if day_date else ""))
    weather = _clean(day.get("weather_note"))
    if weather:
        pdf.set_font("noto", size=8)
        pdf.set_xy(pdf.l_margin + pdf.epw * 0.4, y + 1.9)
        pdf.cell(pdf.epw * 0.6 - 3, 5.5, _fit(pdf, weather, pdf.epw * 0.6 - 6), align="R")
    pdf.set_y(y + 12)
    pdf.set_text_color(*NAVY)


def _photo_strip(pdf: _ItineraryPDF, slots: list[tuple[str, bytes | None]]) -> None:
    if not slots:
        return
    need = 52
    if pdf.will_page_break(need):
        pdf.add_page()
    gap = 3.0
    count = len(slots)
    box_w = (pdf.epw - gap * (count - 1)) / count
    box_h = 36.0
    y = pdf.get_y()
    for index, (name, image) in enumerate(slots):
        x = pdf.l_margin + index * (box_w + gap)
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.25)
        if image is not None:
            pdf.image(io.BytesIO(image), x=x, y=y, w=box_w, h=box_h, keep_aspect_ratio=True)
            pdf.rect(x, y, box_w, box_h)
        else:
            pdf.set_fill_color(*LIGHT)
            pdf.rect(x, y, box_w, box_h, style="DF")
            pdf.set_font("noto", size=8)
            pdf.set_text_color(*GRAY)
            pdf.set_xy(x, y + box_h / 2 - 2.5)
            pdf.cell(box_w, 5, "暂无实拍图", align="C")
        pdf.set_xy(x, y + box_h + 1)
        pdf.set_font("noto", size=7.5)
        pdf.set_text_color(*GRAY)
        pdf.cell(box_w, 4.5, _fit(pdf, name, box_w), align="C")
    pdf.set_y(y + box_h + 7)
    pdf.set_text_color(*NAVY)


def _timeline_table(pdf: _ItineraryPDF, day: dict[str, Any]) -> None:
    rows: list[list[str]] = []
    day_total = 0.0
    # PDF 与网页表格、总预算共用同一个条目集合，避免三处各算各的。
    for _day_index, _day, item in tools.iter_cost_items({"days": [day]}):
        try:
            cost = float(item.get("cost") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        day_total += cost
        kind = zh_labels.item_type(item.get("type"))
        note = _clean(item.get("note"))
        activity = kind if not note else f"{kind}\n{note}"
        start = _clean(item.get("start"))
        end = _clean(item.get("end"))
        span = f"{start}-{end}" if start and end else (start or end or "—")
        rows.append(
            [
                span,
                _clean(item.get("name")) or "—",
                activity,
                zh_labels.transport(item.get("transport")) or "—",
                _money(cost),
            ]
        )
    if not rows:
        return
    pdf.set_fill_color(255, 255, 255)
    pdf.set_text_color(*NAVY)
    with pdf.table(
        col_widths=[15, 26, 40, 11, 8],
        headings_style=FontFace(family="noto", emphasis="", size_pt=8.5, color=(255, 255, 255), fill_color=ACCENT),
        text_align="LEFT",
        line_height=4.9,
        padding=1.8,
        borders_layout=TableBordersLayout.HORIZONTAL_LINES,
    ) as table:
        head = table.row()
        for text in ("时间", "地点", "活动", "交通", "花费"):
            head.cell(text)
        for row_data in rows:
            row = table.row()
            for text in row_data:
                row.cell(text)
        total_row = table.row()
        total_row.cell(
            f"当日小计 {_money(day_total, free='¥0')}　共 {len(rows)} 项安排",
            colspan=5,
            align="R",
            style=FontFace(family="noto", emphasis="", size_pt=8, color=GRAY),
        )
    pdf.ln(1.5)
    pdf.set_text_color(*NAVY)


def _budget_block(pdf: _ItineraryPDF, c: dict[str, Any], budget: dict[str, Any]) -> None:
    # 费用合计是一块整体，别让表头落在上一页、只剩一两行跑过来
    if pdf.will_page_break(70):
        pdf.add_page()
    _section_title(pdf, "费用合计")
    breakdown = budget.get("breakdown") or {}
    rows = [[zh_labels.budget_item(key), _money(value, free="¥0")] for key, value in breakdown.items()]
    pdf.set_fill_color(255, 255, 255)
    pdf.set_text_color(*NAVY)
    if rows:
        with pdf.table(
            col_widths=[3, 1],
            headings_style=FontFace(family="noto", emphasis="", size_pt=8.5, color=(255, 255, 255), fill_color=ACCENT),
            text_align="LEFT",
            line_height=4.6,
            padding=1.5,
            borders_layout=TableBordersLayout.HORIZONTAL_LINES,
        ) as table:
            head = table.row()
            head.cell("项目")
            head.cell("金额")
            for row_data in rows:
                row = table.row()
                row.cell(row_data[0])
                row.cell(row_data[1])
    total = budget.get("total") or 0
    cap = float(c.get("budget") or 0)
    try:
        total_val = float(total)
    except (TypeError, ValueError):
        total_val = 0.0
    pdf.ln(2)
    bar_y = pdf.get_y()
    pdf.set_fill_color(*ACCENT)
    pdf.rect(pdf.l_margin, bar_y, pdf.epw, 10, style="F")
    pdf.set_xy(pdf.l_margin + 4, bar_y + 2)
    pdf.set_font("noto", size=10)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(pdf.epw * 0.5, 6, "费用合计")
    pdf.set_xy(pdf.l_margin + pdf.epw * 0.5, bar_y + 1.5)
    pdf.set_font("noto", size=13)
    pdf.cell(pdf.epw * 0.5 - 4, 7, _money(total_val, free="¥0"), align="R")
    pdf.set_y(bar_y + 12)
    pdf.set_text_color(*GRAY)
    pdf.set_font("noto", size=8)
    if cap > 0:
        diff = cap - total_val
        tail = f"预算上限 {_money(cap)}　结余 {_money(diff)}" if diff >= 0 else f"预算上限 {_money(cap)}　超出 {_money(-diff)}"
        people = max(1, int(c.get("people") or 1))
        tail += f"　人均 {_money(total_val / people)}"
        pdf.cell(0, 5, tail, new_x="LMARGIN", new_y="NEXT")
    if budget.get("route_note"):
        pdf.multi_cell(0, 5, _clean(budget.get("route_note")), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*NAVY)


def _tips_block(pdf: _ItineraryPDF, tips: list[str]) -> None:
    if not tips:
        return
    # 先算高度，整块要么全在本页，要么整体换页，避免只剩一两条孤零零留在新页
    pdf.set_font("noto", size=9.5)
    needed = 12.0
    for tip in tips:
        text = _clean(tip)
        if text:
            needed += max(1, len(pdf.multi_cell(0, 5.2, f"·  {text}", dry_run=True, output="LINES"))) * 5.2
    if pdf.will_page_break(needed):
        pdf.add_page()
    _section_title(pdf, "行程要点")
    pdf.set_font("noto", size=9.5)
    for tip in tips:
        text = _clean(tip)
        if text:
            pdf.multi_cell(0, 5.2, f"·  {text}", new_x="LMARGIN", new_y="NEXT")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_itinerary_pdf(result: dict[str, Any]) -> bytes:
    """把 agent 的结果渲染成 A4 行程单 PDF（bytes，可直接给 st.download_button）。"""
    constraints = result.get("constraints") or {}
    itinerary = result.get("itinerary") or {}
    budget = result.get("budget") or {}
    days = itinerary.get("days") or []
    city = _clean(itinerary.get("city") or constraints.get("city") or "目的地")
    n_days = int(constraints.get("days") or len(days) or 1)

    photo_map = _collect_photos(days)
    images: dict[int, list[bytes | None]] = {}
    if photo_map:
        # 图片按版面格子的比例裁切：一排放 1~3 张，格子越少越宽
        gap = 3.0
        jobs: list[tuple[int, str, str, float]] = []
        for idx, slots in photo_map.items():
            box_w = (182.0 - gap * (len(slots) - 1)) / len(slots)
            aspect = box_w / 36.0
            jobs.extend((idx, name, url, aspect) for name, url in slots)
        with ThreadPoolExecutor(max_workers=min(PHOTO_WORKERS, len(jobs))) as pool:
            fetched = list(pool.map(lambda job: _fetch_photo(job[2], job[3]), jobs))
        cursor = 0
        for idx, slots in photo_map.items():
            images[idx] = fetched[cursor : cursor + len(slots)]
            cursor += len(slots)

    pdf = _ItineraryPDF(f"{city} {n_days} 天行程单")
    pdf.add_page()
    _title_block(pdf, result, city, n_days)
    _overview_cards(pdf, {**constraints, "city": city, "days": n_days}, budget, days)

    for index, day in enumerate(days, 1):
        slots = photo_map.get(index - 1) or []
        _day_header(pdf, index, day, need=78.0 if slots else 52.0)
        if slots:
            _photo_strip(
                pdf,
                [(name, image) for (name, _), image in zip(slots, images.get(index - 1) or [])],
            )
        _timeline_table(pdf, day)

    _budget_block(pdf, constraints, budget)
    _tips_block(pdf, itinerary.get("tips") or [])

    output = pdf.output()
    return bytes(output)
