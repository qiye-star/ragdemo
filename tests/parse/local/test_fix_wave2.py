"""修复轮 2（Opus 对修复轮 1 的复核）回归测试：矩阵判定水平、独立的顺序基准、页码误判。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from io import BytesIO

import pytest
from reportlab.pdfgen import canvas

from ragdemo.parse.local import pipeline
from ragdemo.parse.local.layout import char_is_horizontal, extract_pages
from ragdemo.parse.local.number_closure import ordered_numbers
from ragdemo.parse.local.pipeline import parse_pages_deterministic, parse_pages_local
from ragdemo.parse.local.typeset import TypesetResult

BODY = "Revenue grew strongly during the period and management expects momentum %d"
AXIS = "Axis 12.5 in 2024"


class Echo:
    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
        return page_text + "\n\n" + "\n\n".join(table_markdowns)


def build(n: int, extra: object) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    for p in range(n):
        c.setFont("Helvetica", 10)
        for i in range(6):
            c.drawString(60, 800 - 130 - i * 15, BODY % i)
        extra(c, p)  # type: ignore[operator]
        c.showPage()
    c.save()
    return buf.getvalue()


def _matrix_text(c: canvas.Canvas, p: int, m: tuple[float, ...]) -> None:
    if p == 0:
        t = c.beginText()
        t.setTextTransform(*m)
        t.textOut(AXIS)
        c.drawText(t)


def _handed_back(pdf: bytes) -> None:
    d = parse_pages_deterministic(pdf)[0]
    assert d.status == "layout_unsupported" and d.markdown == "", d.status
    loc = parse_pages_local(pdf, Echo())[0]
    assert loc.status == "no_text_layer" and loc.markdown == "", loc.status


# ---------- C1：按字符文本矩阵判水平，不信 pdfminer 的 upright ----------
@pytest.mark.parametrize("ang", [60, 89, -60, -30, 135])
@pytest.mark.parametrize("pages", [1, 4])
def test_oblique_text_is_handed_back(ang: int, pages: int) -> None:
    def f(c: canvas.Canvas, p: int) -> None:
        if p == 0:
            c.saveState()
            c.translate(200, 300)
            c.rotate(ang)
            c.drawString(0, 0, AXIS)
            c.restoreState()

    _handed_back(build(pages, f))


@pytest.mark.parametrize("a", [-4.4e-08, 4.4e-08])
@pytest.mark.parametrize("pages", [1, 4])
def test_a_ninety_degree_matrix_with_float_noise_is_handed_back(a: float, pages: int) -> None:
    # 真实语料：太平洋研报首页图表日期刻度，matrix [-4.4e-08, 1, -1, -4.4e-08] 且 upright=True
    _handed_back(build(pages, lambda c, p: _matrix_text(c, p, (a, 1.0, -1.0, a, 300, 300))))


def test_mirrored_and_negative_scale_text_is_handed_back() -> None:
    _handed_back(build(1, lambda c, p: _matrix_text(c, p, (-1, 0, 0, 1, 300, 300))))
    _handed_back(build(1, lambda c, p: _matrix_text(c, p, (1, 0, 0, -1, 300, 300))))


def test_one_degree_tilt_and_synthetic_italic_stay_ok() -> None:
    def tilt(c: canvas.Canvas, p: int) -> None:
        if p == 0:
            c.saveState()
            c.translate(100, 300)
            c.rotate(1)
            c.drawString(0, 0, AXIS)
            c.restoreState()

    for f in (tilt, lambda c, p: _matrix_text(c, p, (1, 0, 0.25, 1, 100, 300))):
        for pages in (1, 4):
            d = parse_pages_deterministic(build(pages, f))[0]
            assert d.status == "ok" and "12.5" in d.markdown and "2024" in d.markdown, d.status


def test_char_is_horizontal_rule() -> None:
    assert char_is_horizontal((1, 0, 0, 1, 0, 0))
    assert char_is_horizontal((10, 0.1, 0, 10, 0, 0))  # ≈0.6°
    assert char_is_horizontal((1, 0, 0.3, 1, 0, 0))  # 斜体（c != 0 没问题）
    assert not char_is_horizontal((1, 0.1, 0, 1, 0, 0))  # ≈5.7°
    assert not char_is_horizontal((0, 1, -1, 0, 0, 0))
    assert not char_is_horizontal((-4.4e-08, 1, -1, -4.4e-08, 0, 0))
    assert not char_is_horizontal((-1, 0, 0, 1, 0, 0))
    assert not char_is_horizontal((1, 0, 0, -1, 0, 0))


def test_extract_pages_marks_oblique_words_as_not_upright() -> None:
    def f(c: canvas.Canvas, p: int) -> None:
        c.saveState()
        c.translate(200, 300)
        c.rotate(60)
        c.drawString(0, 0, AXIS)
        c.restoreState()

    words = extract_pages(build(1, f))[0].words
    assert "".join(w.text for w in words if not w.upright).count("2") >= 2  # 12.5 与 2024 都在
    assert all(w.upright for w in words if w.text.startswith(("Revenue", "grew")))


# ---------- I2：顺序基准是未剥离、未重聚类的原始 extract_text ----------
def _i2a(c: canvas.Canvas, p: int) -> None:
    if p >= 1:
        c.setFont("Helvetica", 10)
        c.drawString(60, 770, "ACME Research Weekly Notes")
    c.setFont("Helvetica", 20)
    c.drawString(60, 600 - 7.93, "2.0")  # 顶对齐：大号 2.0 在左，小号文字在同一顶线右侧
    c.setFont("Helvetica", 10)
    c.drawString(120, 600, "Q1 revenue 1.0 then")
    c.drawString(60, 570, "Q2 revenue 3.0 later in the year")


def test_order_reference_is_the_original_extract_text_even_when_the_page_is_stripped() -> None:
    pdf = build(4, _i2a)
    originals = extract_pages(pdf)
    for r, lay in zip(parse_pages_deterministic(pdf), originals, strict=True):
        if r.status == "ok":
            # ok 的页：输出数字必须是原始 extract_text 数字序列的子序列，且余下恰为被剥文字的数字
            orig = ordered_numbers(lay.text)
            out = ordered_numbers(r.markdown)
            it = iter(orig)
            assert all(any(t == o for o in it) for t in out)
            assert Counter(orig) - Counter(out) == Counter(ordered_numbers(r.stripped_text))
    assert [r.status for r in parse_pages_deterministic(pdf)] == ["closure_failed"] * 4


def test_the_order_check_runs_even_when_other_closure_checks_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = build(1, lambda c, p: None)
    good = parse_pages_deterministic(pdf)[0]
    a, b, c_ = BODY % 0, BODY % 1, BODY % 2

    def fake(pages: Sequence[object]) -> list[TypesetResult]:
        md = good.markdown.replace(a, "@@").replace(c_, a).replace("@@", c_)  # 对调 0 与 2
        md = md.replace(b, "momentum")  # 同时丢掉数字 1
        return [TypesetResult("ok", md, "", [])]

    monkeypatch.setattr(pipeline, "typeset_document", fake)
    r = parse_pages_deterministic(pdf)[0]
    assert r.status == "closure_failed" and "顺序" in r.reason and "丢失了数字" in r.reason


# ---------- I3 ----------
def _stripped(pdf: bytes) -> list[str]:
    return [r.stripped_text for r in parse_pages_deterministic(pdf)]


def test_a_footer_year_that_rises_by_one_per_page_is_data_not_a_page_number() -> None:
    pdf = build(4, lambda c, p: c.drawString(290, 20, str(2021 + p)))
    for p, r in enumerate(parse_pages_deterministic(pdf)):
        assert str(2021 + p) in r.markdown and r.stripped_text == ""


def test_an_integer_under_a_constant_colon_label_is_data_even_if_it_rises_by_one() -> None:
    def f(c: canvas.Canvas, p: int) -> None:
        c.drawString(60, 45, "Cumulative installed units (thousand):")
        c.drawString(60, 25, str(311 + p))

    for p, r in enumerate(parse_pages_deterministic(build(5, f))):
        assert str(311 + p) in r.markdown and r.stripped_text == ""


def test_small_integers_under_a_colon_label_are_data_too() -> None:
    def f(c: canvas.Canvas, p: int) -> None:
        c.drawString(60, 45, "Number of board meetings held:")
        c.drawString(60, 25, str(3 + p))

    for p, r in enumerate(parse_pages_deterministic(build(5, f))):
        assert str(3 + p) in r.markdown and r.stripped_text == ""


def test_real_page_numbers_within_the_plausible_range_are_still_stripped() -> None:
    pdf = build(5, lambda c, p: c.drawString(290, 20, str(p + 1)))
    assert _stripped(pdf) == ["1", "2", "3", "4", "5"]


def test_a_two_column_header_with_a_unit_is_kept_when_values_are_right_aligned() -> None:
    def f(c: canvas.Canvas, p: int) -> None:
        c.drawString(60, 770, "Segment")
        c.drawString(300, 770, "Amount (RMB 100 million)")
        c.drawRightString(560, 740, f"{12 + p}.3")
        c.drawString(90, 740, "Chips")

    for r in parse_pages_deterministic(build(4, f)):
        assert "100 million" in r.markdown and r.stripped_text == ""


def test_a_header_alone_in_the_band_with_a_unit_is_kept() -> None:
    def f(c: canvas.Canvas, p: int) -> None:
        c.drawString(60, 770, "Segment")
        c.drawString(300, 770, "Amount (RMB 100 million)")

    for r in parse_pages_deterministic(build(4, f)):
        assert "100 million" in r.markdown and r.stripped_text == ""


def test_a_chinese_unit_marker_row_is_kept() -> None:
    from ragdemo.parse.local.decor import strip_decor
    from ragdemo.parse.local.layout import PageLayout, Word

    def page(i: int) -> PageLayout:
        top = [Word("ACME", 60, 100, 20, 30), Word("单位 万元", 400, 450, 20, 30)]
        body = [Word(f"body{j}", 60, 100, 300 + 14 * j, 310 + 14 * j) for j in range(6)]
        return PageLayout(i, 600, 800, [*top, *body], "ACME 单位 万元\nbody")

    for sp in strip_decor([page(i) for i in range(1, 5)]):
        assert sp.stripped_text == "" and any(w.text == "单位 万元" for w in sp.layout.words)


def test_a_pure_running_header_is_still_stripped() -> None:
    pdf = build(4, lambda c, p: c.drawString(60, 770, "ACME Securities Research Weekly"))
    assert all("ACME" in t for t in _stripped(pdf))


def test_the_rotated_watermark_page_stays_ok_and_the_watermark_is_in_stripped_text() -> None:
    def f(c: canvas.Canvas, p: int) -> None:
        c.saveState()
        c.translate(300, 300)
        c.rotate(90)
        c.drawString(0, 0, "CONFIDENTIAL")
        c.restoreState()

    for r in parse_pages_deterministic(build(4, f)):
        assert r.status == "ok"
        assert Counter(r.stripped_text.replace(" ", "")) == Counter("CONFIDENTIAL")
        assert "CONFIDENTIAL" not in r.markdown and "LAITNEDIFNOC" not in r.markdown
