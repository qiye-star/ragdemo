from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from ragdemo.parse.local.chart import (
    REASON_FIGURE_ROTATED,
    ChartStrip,
    is_caption_text,
    is_fragment_row,
    residual_fragment_rows,
    strip_charts,
)
from ragdemo.parse.local.columns import page_columns
from ragdemo.parse.local.decor import strip_decor
from ragdemo.parse.local.layout import PageLayout, Word, extract_pages
from ragdemo.parse.local.pipeline import self_check
from ragdemo.parse.local.samples import layout_from_fixture
from ragdemo.parse.local.tables import cluster_rows, find_table_regions
from ragdemo.parse.local.typeset import body_font_size, typeset_document
from tests.parse.local.test_typeset import LEAD, body_lines, line

PW, PH = 595.0, 842.0
FONT = "STSong-Light"
SENTENCE = "这是一段用于测试的正文文字句子足够长不会被当作图表标签"
TABLE = [
    ("营业收入", "91.2", "60.1"),
    ("营业成本", "40.5", "30.2"),
    ("毛利润", "50.7", "29.9"),
    ("净利润", "20.3", "11.8"),
]
RAW_RESEARCH = Path("docs/rawdata/688041-hygon/research")


def text(c: canvas.Canvas, s: str, x: float, top: float, size: float = 10.0) -> None:
    """top = 基线到页顶的距离（pt）。"""
    c.setFont(FONT, size)
    c.drawString(x, PH - top, s)


def body(c: canvas.Canvas, top: float, n: int, x: float = 60.0, s: str = SENTENCE) -> None:
    for i in range(n):
        text(c, s, x, top + i * 15)


def bar_chart(c: canvas.Canvas, x0: float, top: float, w: float = 200.0, h: float = 140.0) -> None:
    base = PH - (top + h)
    c.setLineWidth(0.5)
    c.line(x0, base, x0 + w, base)
    c.line(x0, base, x0, base + h)
    for k in (1, 2):
        c.line(x0, base + k * h / 2, x0 + w, base + k * h / 2)
    for i, v in enumerate((40, 25, 34, 12)):
        c.setFillColorRGB(0.2, 0.4, 0.8)
        c.rect(x0 + 15 + i * 45, base, 26, v * 3, stroke=0, fill=1)
        c.setFillColorRGB(0, 0, 0)
        text(c, f"{v}%", x0 + 17 + i * 45, top + h - v * 3 - 3, size=7)
        text(c, str(2021 + i), x0 + 15 + i * 45, top + h + 10, size=7)
    for k, t in enumerate(("40", "20", "0")):
        text(c, t, x0 - 14, top + k * h / 2 + 3, size=7)


def line_chart(c: canvas.Canvas, x0: float, top: float, w: float = 200.0, h: float = 140.0) -> None:
    base = PH - (top + h)
    c.setLineWidth(0.5)
    c.line(x0, base, x0 + w, base)
    c.line(x0, base, x0, base + h)
    c.line(x0, base + h / 2, x0 + w, base + h / 2)
    p = c.beginPath()
    p.moveTo(x0 + 10, base + 20)
    for i, y in enumerate((60, 45, 100, 120), start=1):
        p.lineTo(x0 + 10 + i * 45, base + y)
    c.drawPath(p, stroke=1, fill=0)
    for i in range(5):
        text(c, str(2020 + i), x0 + 2 + i * 45, top + h + 10, size=7)


def plain_table(
    c: canvas.Canvas, top: float, xs: tuple[float, float, float] = (60.0, 200.0, 340.0)
) -> None:
    for i, row in enumerate(TABLE):
        for x, cell in zip(xs, row, strict=True):
            text(c, cell, x, top + i * 15)


def ruled_table(c: canvas.Canvas, x0: float, top: float) -> None:
    widths, row_h = (90.0, 70.0, 70.0), 16.0
    right = x0 + sum(widths)
    c.setLineWidth(0.5)
    for i in range(len(TABLE) + 1):
        y = PH - (top + i * row_h)
        c.line(x0, y, right, y)
    x = x0
    for wd in (0.0, *widths):
        x += wd
        c.line(x, PH - top, x, PH - (top + len(TABLE) * row_h))
    for i, row in enumerate(TABLE):
        x = x0
        for wd, cell in zip(widths, row, strict=True):
            text(c, cell, x + 4, top + i * row_h + 12)
            x += wd


def pdf_page(draw: Callable[[canvas.Canvas], None]) -> bytes:
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(PW, PH))
    draw(c)
    c.showPage()
    c.save()
    return buf.getvalue()


def strip_first(pdf: bytes) -> tuple[PageLayout, ChartStrip]:
    (lay,) = extract_pages(pdf)
    return lay, strip_charts(lay, body_font_size([lay]))


def alnum(texts: Iterable[str]) -> Counter[str]:
    return Counter(ch for t in texts for ch in t if ch.isalnum())


def assert_lossless(lay: PageLayout, cs: ChartStrip) -> None:
    assert alnum(w.text for w in lay.words) == alnum(w.text for w in cs.layout.words) + alnum(
        [cs.stripped_text]
    )


def bar_page(c: canvas.Canvas) -> None:
    body(c, 60, 3)
    text(c, "图1：营业收入与增速", 60, 106, size=9)
    bar_chart(c, 80, 120)
    text(c, "资料来源：公司公告，测试整理", 60, 296, size=8)
    body(c, 330, 3)


def test_extract_pages_reports_images_rects_lines_and_curves_as_graphics() -> None:
    def draw(c: canvas.Canvas) -> None:
        bar_chart(c, 80, 120)
        line_chart(c, 320, 120)
        c.drawImage(ImageReader(Image.new("RGB", (40, 30), (200, 220, 240))), 60, 400, 120, 90)

    (lay,) = extract_pages(pdf_page(draw))
    kinds = Counter(g.kind for g in lay.graphics)
    assert kinds["image"] == 1 and kinds["rect"] >= 4 and kinds["line"] >= 6 and kinds["curve"] >= 1
    assert all(g.x0 <= g.x1 and g.top <= g.bottom for g in lay.graphics)


def test_a_vector_bar_chart_is_one_figure_its_labels_are_stripped_and_the_caption_stays() -> None:
    lay, cs = strip_first(pdf_page(bar_page))
    (fig,) = cs.figures
    assert fig.detection == "vector" and fig.caption == "图1：营业收入与增速"
    assert fig.source_note == "资料来源：公司公告，测试整理"
    body_texts = {w.text for w in cs.layout.words}
    assert not body_texts & {"40%", "25%", "34%", "12%", "2021", "2024", "40", "20", "0"}
    assert any("图1" in t for t in body_texts) and any("资料来源" in t for t in body_texts)
    assert sum(1 for w in cs.layout.words if w.text == SENTENCE) == 6
    assert fig.stripped_words == cs.stripped_words == len(lay.words) - len(cs.layout.words)
    x0, top, x1, bottom = fig.bbox
    assert x0 <= 66 and top <= 122 and x1 >= 280 and bottom >= 262  # 绘图框 ∪ 标签
    assert_lossless(lay, cs)


def test_a_line_chart_drawn_as_a_polyline_is_a_figure() -> None:
    lay, cs = strip_first(
        pdf_page(lambda c: (body(c, 80, 3), line_chart(c, 80, 140), body(c, 330, 3)))
    )
    (fig,) = cs.figures
    assert fig.detection == "vector" and not {"2020", "2024"} & {w.text for w in cs.layout.words}
    assert_lossless(lay, cs)


def test_an_embedded_chart_image_is_a_figure_and_a_header_logo_and_a_qr_code_are_not() -> None:
    def draw(c: canvas.Canvas) -> None:
        img = ImageReader(Image.new("RGB", (400, 250), (200, 220, 240)))
        c.drawImage(
            ImageReader(Image.new("RGB", (40, 40), (0, 0, 0))), 40, PH - 50, 36, 36
        )  # 页眉 logo
        body(c, 90, 3)
        c.drawImage(img, 60, PH - 300, 260, 160)  # top=140 bottom=300
        c.drawImage(
            ImageReader(Image.new("RGB", (70, 70), (0, 0, 0))), 450, PH - 680, 70, 70
        )  # 二维码
        body(c, 330, 3)

    lay, cs = strip_first(pdf_page(draw))
    (fig,) = cs.figures
    assert fig.detection == "embedded_image"
    assert fig.bbox == pytest.approx((60.0, 140.0, 320.0, 300.0), abs=1.0)
    assert cs.stripped_words == 0 and len(cs.layout.words) == len(lay.words)


def test_a_ruled_grid_with_cell_text_is_a_table_not_a_chart_and_nothing_is_stripped() -> None:
    lay, cs = strip_first(
        pdf_page(lambda c: (body(c, 80, 3), ruled_table(c, 60, 140), body(c, 260, 3)))
    )
    assert cs.figures == () and cs.items == ()
    assert len(cs.layout.words) == len(lay.words)


def _table_words(lay: PageLayout) -> list[Word]:
    cells = {cell for row in TABLE for cell in row}
    return [w for w in lay.words if w.text in cells]


def test_a_chart_directly_above_a_table_keeps_every_table_word_in_the_body() -> None:
    lay, cs = strip_first(
        pdf_page(lambda c: (body(c, 80, 2), bar_chart(c, 80, 120), plain_table(c, 284)))
    )
    assert len(cs.figures) == 1
    kept = {id(w) for w in cs.layout.words}
    assert len(_table_words(lay)) == 12 and all(id(w) in kept for w in _table_words(lay))
    assert not any(cell in cs.stripped_text for row in TABLE for cell in row)
    rows = cluster_rows([w for w in cs.layout.words if w.upright])
    assert any(
        sum(1 for r in reg for w in r if w.text in {"营业收入", "净利润"}) == 2
        for reg in find_table_regions(rows)
    )
    assert_lossless(lay, cs)


def test_a_chart_beside_a_table_keeps_every_table_word_in_the_body() -> None:
    lay, cs = strip_first(
        pdf_page(lambda c: (bar_chart(c, 80, 120), plain_table(c, 160, xs=(330.0, 420.0, 500.0))))
    )
    assert len(cs.figures) == 1
    kept = {id(w) for w in cs.layout.words}
    assert all(id(w) in kept for w in _table_words(lay))
    assert_lossless(lay, cs)


def test_a_chart_next_to_two_column_body_text_strips_only_the_chart() -> None:
    col = "左栏正文文字句子足够长不会被当作标签"

    def draw(c: canvas.Canvas) -> None:
        body(c, 100, 10, x=320, s="右栏正文文字句子足够长不会被当作标签")
        body(
            c, 100, 5, x=60, s=col
        )  # 左栏 5 行：T3 调参后 MIN_SIDE_ROWS = 4，3 行的左栏本来就不判多栏
        bar_chart(c, 80, 200)

    lay, cs = strip_first(pdf_page(draw))
    assert len(cs.figures) == 1
    assert sum(1 for w in cs.layout.words if w.text.startswith("右栏")) == 10
    assert sum(1 for w in cs.layout.words if w.text == col) == 5
    assert page_columns(cs.layout, body_font_size([lay])).multicolumn  # 双栏正文照样被拦
    assert_lossless(lay, cs)


def _rotated(c: canvas.Canvas, s: str, x: float, top: float) -> None:
    c.saveState()
    c.translate(x, PH - top)
    c.rotate(90)
    c.setFont(FONT, 7)
    c.drawString(0, 0, s)
    c.restoreState()


def test_a_rotated_axis_title_without_digits_is_stripped_and_one_with_digits_is_unsafe() -> None:
    _, cs = strip_first(
        pdf_page(
            lambda c: (body(c, 80, 3), bar_chart(c, 90, 140), _rotated(c, "单位亿元", 70, 240))
        )
    )
    assert len(cs.figures) == 1 and cs.unsafe_reason is None
    # 竖排文字的字序由 pdfplumber 决定且是倒的（见 layout.unsafe_orientation_reason）：两种都认
    flat = cs.stripped_text.replace(" ", "")
    assert all(w.upright for w in cs.layout.words) and ("单位亿元" in flat or "元亿位单" in flat)
    _, bad = strip_first(
        pdf_page(
            lambda c: (body(c, 80, 3), bar_chart(c, 90, 140), _rotated(c, "2024亿元", 70, 240))
        )
    )
    assert bad.unsafe_reason == REASON_FIGURE_ROTATED


def test_a_filled_sidebar_panel_with_sentences_is_not_a_chart() -> None:
    def draw(c: canvas.Canvas) -> None:
        c.setFillColorRGB(0.9, 0.9, 0.95)
        c.roundRect(380, PH - 500, 180, 380, 8, stroke=0, fill=1)
        c.setFillColorRGB(0.8, 0.1, 0.1)
        for i in range(3):
            c.rect(390 + i * 14, PH - 140, 10, 10, stroke=0, fill=1)
        c.setFillColorRGB(0, 0, 0)
        body(c, 170, 6, x=390, s="分析师执业证书编号与联系方式说明")
        body(c, 100, 12, x=60, s="正文")

    lay, cs = strip_first(pdf_page(draw))
    assert cs.figures == () and cs.items == () and len(cs.layout.words) == len(lay.words)


def test_bracket_split_inside_a_chart_is_stripped_and_the_unchanged_closure_passes() -> None:
    def draw(c: canvas.Canvas) -> None:
        body(c, 80, 3, s="本期营业收入同比增长百分之十二点五正文")
        bar_chart(c, 80, 120)
        text(c, "(4%", 200, 200, size=7)
        text(c, ")", 216, 200, size=7)
        body(c, 330, 3, s="下文继续说明经营情况正文句子足够长")

    lay, cs = strip_first(pdf_page(draw))
    assert len(cs.figures) == 1 and "(4%" in cs.stripped_text and ")" in cs.stripped_text
    (res,) = typeset_document([cs.layout])
    assert res.status == "ok"
    _, reason = self_check(res.markdown, cs.layout.text, res.tables, lay.text, cs.stripped_text)
    assert reason == ""


def test_the_bracket_split_in_body_text_is_still_a_closure_failure_no_exemption() -> None:
    # v1 唯一误报的最小复现：原文 "(4%)"（括号即负数），排版后括号与数字分开 → 读成 "4%"。
    # 闭合校验不改、不豁免：没被图表区域剥走时，自检必须照样拦下。
    _, reason = self_check("4 %\n\n)", "( 4 %\n)", [], "( 4 %\n)", "")
    assert reason != ""


def test_short_numeric_rows_are_fragments_and_residual_rows_are_counted() -> None:
    assert is_fragment_row(line("( 4 %", 100)) and is_fragment_row(line(")", 100))
    assert is_fragment_row(
        line("2021", 100, x=60) + line("2022", 100, x=160) + line("2023", 100, x=260)
    )
    assert not is_fragment_row(line("plain body words number 3 of the paragraph", 100))
    rows = [
        *body_lines(100, 5),
        line("40%", 200),
        line("20%", 200 + LEAD),
        line("0%", 200 + 2 * LEAD),
    ]
    assert residual_fragment_rows(rows) == 3


def test_caption_and_source_rows_are_recognised() -> None:
    assert (
        is_caption_text("图 3：公司营收结构")
        and is_caption_text("图表12 毛利率")
        and is_caption_text("Figure 2. Revenue")
    )
    assert not is_caption_text("图形处理器业务继续增长")


def test_masked_real_chart_page_has_a_figure_region() -> None:
    # 研报脱敏夹具不入库（计划开放问题 2 / 控制者裁决）：只在本机夹具目录里有时才跑
    path = Path("data/experiments/l0v2/fixtures/r_20250305_taipingyang_p1.json")
    if not path.exists():
        pytest.skip(
            "研报脱敏夹具不入库；本机没有 data/experiments/l0v2/fixtures（先跑 l0v2.py fixtures）"
        )
    d = json.loads(path.read_text(encoding="utf-8"))
    lay = layout_from_fixture(d)
    assert lay.graphics, "夹具缺 graphics：先按 Step 5 重新生成夹具"
    cs = strip_charts(lay, body_font_size([lay]))
    assert cs.figures and cs.unsafe_reason is None
    assert alnum(w.text for w in lay.words) == alnum(w.text for w in cs.layout.words) + alnum(
        [cs.stripped_text]
    )


def test_real_v1_false_alarm_page_strips_the_bracket_fragments() -> None:
    hits = sorted(RAW_RESEARCH.glob("*_AP202503051644062530.pdf"))
    if not hits:
        pytest.skip("原始研报不入库（docs/rawdata/.gitignore），本机没有这份文件")
    pages = extract_pages(hits[0].read_bytes())
    s = strip_decor(pages)[0]
    cs = strip_charts(s.layout, body_font_size([p.layout for p in strip_decor(pages)]))
    assert cs.figures and "%" in cs.stripped_text  # 带括号的百分比碎片随图表区域剥离，不再进入正文


def shaded_table(c: canvas.Canvas, x0: float, top: float, cols: int) -> None:
    """无边框线、每个单元格一块填充底色的表格：几何上是一堆填充矩形，与柱状图同形。"""
    widths, row_h = (90.0, 70.0, 70.0)[:cols], 16.0
    for i, row in enumerate(TABLE):
        x = x0
        for wd, cell in zip(widths, row[:cols], strict=True):
            c.setFillColorRGB(0.85, 0.9, 0.95 if i % 2 else 1.0)
            c.rect(x, PH - (top + (i + 1) * row_h), wd - 2, row_h - 2, stroke=0, fill=1)
            c.setFillColorRGB(0, 0, 0)
            text(c, cell, x + 4, top + i * row_h + 11)
            x += wd


@pytest.mark.parametrize("cols", [3, 2])
def test_a_table_drawn_with_shaded_cells_is_never_a_chart(cols: int) -> None:
    # 真实调参集页面暴露的洞：整张表在候选绘图框内部，"框外的表格词才受保护"导致整张表被当图表剥掉
    lay, cs = strip_first(pdf_page(lambda c: (body(c, 80, 3), shaded_table(c, 60, 140, cols))))
    assert cs.figures == () and cs.items == ()
    assert len(cs.layout.words) == len(lay.words)


# ---------------------------------------------------------------- T4 评审修复（C1–C3、I2）
def shaded_rows(
    c: canvas.Canvas,
    x0: float,
    top: float,
    rows: list[tuple[str, ...]],
    xs: tuple[float, ...],
    rect_w: float | None = None,
    rect_cols: int | None = None,
) -> None:
    """每个单元格一块填充底色；xs 是各列文字起点相对 x0 的偏移。"""
    for i, row in enumerate(rows):
        for k, (dx, cell) in enumerate(zip(xs, row, strict=True)):
            if k < (rect_cols or len(xs)):
                c.setFillColorRGB(0.85, 0.9, 0.95 if (i + k) % 2 else 1.0)
                c.rect(x0 + dx - 2, PH - (top + (i + 1) * 16), rect_w or 66, 14, stroke=0, fill=1)
            c.setFillColorRGB(0, 0, 0)
            text(c, cell, x0 + dx, top + i * 16 + 11)


def kept_texts(cs: ChartStrip) -> set[str]:
    return {w.text for w in cs.layout.words}


def test_c1_a_two_row_shaded_table_is_never_a_chart() -> None:
    rows = [("营业收入", "91.2", "60.1"), ("营业成本", "40.5", "30.2")]
    lay, cs = strip_first(
        pdf_page(lambda c: (body(c, 80, 3), shaded_rows(c, 60, 140, rows, (0, 70, 140))))
    )
    assert cs.figures == () and cs.items == ()
    assert len(cs.layout.words) == len(lay.words)


def test_c1_a_tight_two_column_key_value_table_is_never_a_chart() -> None:
    rows = [("营业收入", "91.2"), ("营业成本", "40.5"), ("毛利润", "50.7"), ("净利润", "20.3")]
    lay, cs = strip_first(
        pdf_page(lambda c: (body(c, 80, 3), shaded_rows(c, 60, 140, rows, (0, 45), rect_w=40)))
    )  # 键与值之间只隔 5pt（< FRAGMENT_SEGMENT_GAP）：一行只有一个段
    assert cs.figures == () and cs.items == ()
    assert len(cs.layout.words) == len(lay.words)


def test_c1_a_table_straddling_the_plot_box_edge_discards_the_box() -> None:
    # 底色块只盖住前两列，第三列在框外：表格横跨绘图框边缘
    rows = [(a, b, c_) for a, b, c_ in TABLE]
    lay, cs = strip_first(
        pdf_page(
            lambda c: (
                body(c, 80, 3),
                shaded_rows(c, 60, 140, rows, (0, 90, 160), rect_w=88, rect_cols=2),
            )
        )
    )
    assert cs.figures == () and cs.items == () and cs.discarded
    assert len(cs.layout.words) == len(lay.words)


def _image_chart(c: canvas.Canvas) -> None:
    c.drawImage(ImageReader(Image.new("RGB", (400, 250), (200, 220, 240))), 60, PH - 300, 260, 160)


@pytest.mark.parametrize(
    "rows,xs",
    [
        ([("营业收入", "91.2"), ("营业成本", "40.5")], (0, 120)),
        ([("营业收入", "91.2", "60.1"), ("营业成本", "40.5", "30.2")], (0, 90, 180)),
    ],
)
def test_c2_a_small_table_directly_under_an_image_chart_stays_in_the_body(
    rows: list[tuple[str, ...]], xs: tuple[float, ...]
) -> None:
    def draw(c: canvas.Canvas) -> None:
        body(c, 90, 3)
        _image_chart(c)
        for i, row in enumerate(rows):
            for dx, cell in zip(xs, row, strict=True):
                text(c, cell, 70 + dx, 315 + i * 15)

    lay, cs = strip_first(pdf_page(draw))
    cells = {cell for row in rows for cell in row}
    assert cells <= kept_texts(cs)
    assert not any(cell in cs.stripped_text for cell in cells)
    assert_lossless(lay, cs)


def test_c3_list_items_and_footnotes_beside_a_chart_are_not_band_labels() -> None:
    def draw(c: canvas.Canvas) -> None:
        body(c, 60, 3)
        bar_chart(c, 80, 120)
        text(c, "1、同比增12%", 82, 290, size=7)  # 带编号的列表项（字号小于正文）
        text(c, "注1：2024年", 82, 300, size=7)  # 脚注

    lay, cs = strip_first(pdf_page(draw))
    assert len(cs.figures) == 1
    assert {"1、同比增12%", "注1：2024年"} <= kept_texts(cs)
    assert_lossless(lay, cs)


def test_c3_band_labels_at_body_size_are_not_stripped() -> None:
    def draw(c: canvas.Canvas) -> None:
        body(c, 60, 3)
        bar_chart(c, 80, 120)
        text(c, "全年", 82, 292)  # 10pt 的短行：与正文同字号，不是图表刻度
        text(c, "2024", 150, 292)

    lay, cs = strip_first(pdf_page(draw))
    assert len(cs.figures) == 1 and {"全年", "2024"} <= kept_texts(cs)
    assert_lossless(lay, cs)


def test_c3_short_infographic_lines_over_a_large_image_stay_in_the_body() -> None:
    def draw(c: canvas.Canvas) -> None:
        c.drawImage(ImageReader(Image.new("RGB", (400, 250), (200, 220, 240))), 40, 120, 500, 550)
        for i, s in enumerate(
            ("市占率 35%", "营收 12亿", "增速 18%", "毛利率 45%", "客户 200家", "区域 5个")
        ):
            text(c, s, 100, 200 + i * 20)

    lay, cs = strip_first(pdf_page(draw))
    assert cs.stripped_words == 0 and len(cs.layout.words) == len(lay.words)


def test_i2_a_caption_sharing_a_baseline_with_a_left_column_line_is_still_the_caption() -> None:
    def draw(c: canvas.Canvas) -> None:
        text(c, SENTENCE, 60, 106)
        text(c, "图1：营业收入", 345, 106, size=9)
        bar_chart(c, 345, 120)

    _, cs = strip_first(pdf_page(draw))
    (fig,) = cs.figures
    assert fig.caption == "图1：营业收入"
    assert "图1：营业收入" in kept_texts(cs) and SENTENCE in kept_texts(cs)


def test_i2_two_charts_side_by_side_each_get_their_own_caption() -> None:
    def draw(c: canvas.Canvas) -> None:
        text(c, "图5：甲公司", 40, 106, size=9)
        text(c, "图6：乙公司", 320, 106, size=9)
        bar_chart(c, 40, 120)
        bar_chart(c, 320, 120)

    _, cs = strip_first(pdf_page(draw))
    figs = sorted(cs.figures, key=lambda f: f.bbox[0])
    assert [f.caption for f in figs] == ["图5：甲公司", "图6：乙公司"]


def test_i5_fragment_rows_inside_table_regions_are_not_residual_chart_fragments() -> None:
    table = [
        line("12%", 200 + i * LEAD, x=60)
        + line("13%", 200 + i * LEAD, x=160)
        + line("14%", 200 + i * LEAD, x=260)
        for i in range(4)
    ]
    assert all(is_fragment_row(r) for r in table)
    assert residual_fragment_rows([*body_lines(100, 5), *table]) == 0
    loose = [line("40%", 300 + i * LEAD) for i in range(3)]
    assert residual_fragment_rows([*body_lines(100, 5), *table, *loose]) == 3


def test_c1_a_single_shaded_header_strip_is_never_a_chart() -> None:
    # 实测（调参集研报）：表头行的底色条（一行 >= 3 段文字，框高不到 8 个正文字号）被当成矢量图表
    rows = [("项目", "2023A", "2024A", "2025E", "2026E", "2027E")]
    lay, cs = strip_first(
        pdf_page(
            lambda c: (body(c, 80, 3), shaded_rows(c, 60, 140, rows, (0, 70, 140, 210, 280, 350)))
        )
    )
    assert cs.figures == () and cs.items == ()
    assert len(cs.layout.words) == len(lay.words)


def test_c1_a_tall_chart_with_a_single_tick_row_inside_is_still_a_chart() -> None:
    _, cs = strip_first(pdf_page(bar_page))
    assert len(cs.figures) == 1
