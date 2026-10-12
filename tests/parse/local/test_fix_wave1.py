"""修复轮 1（Opus 复核的 false-pass 路径）回归测试：旋转倒序、数字顺序、页边数据行被剥离。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from ragdemo.parse.local import pipeline
from ragdemo.parse.local.layout import PageLayout, Word, extract_pages
from ragdemo.parse.local.number_closure import ordered_numbers
from ragdemo.parse.local.pipeline import (
    parse_layouts_deterministic,
    parse_pages_deterministic,
    parse_pages_local,
)
from ragdemo.parse.local.typeset import TypesetResult, _no_space_between, typeset_document

BODY = "Revenue grew strongly during the period and management expects momentum %d"


class EchoStructurer:
    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
        return page_text + "\n\n" + "\n\n".join(table_markdowns)


def _build(npages: int, extra: object, size: tuple[int, int] = (600, 800)) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    for p in range(npages):
        c.setFont("Helvetica", 10)
        for i in range(6):
            c.drawString(60, size[1] - 130 - i * 15, BODY % i)
        extra(c, p)  # type: ignore[operator]
        c.showPage()
    c.save()
    return buf.getvalue()


def _rot_numbers(c: canvas.Canvas, p: int) -> None:
    if p == 0:
        c.saveState()
        c.translate(300, 300)
        c.rotate(90)
        c.drawString(0, 0, "Net profit 12.5 -3.4% 2024E")
        c.restoreState()


def _rot_letters(c: canvas.Canvas, p: int) -> None:
    c.saveState()
    c.translate(300, 300)
    c.rotate(90)
    c.drawString(0, 0, "CONFIDENTIAL")
    c.restoreState()


def _assert_handed_back(pdf: bytes, page_idx: int, word: str) -> None:
    r = parse_pages_deterministic(pdf)[page_idx]
    assert r.status == "layout_unsupported" and r.markdown == "" and word in r.reason
    (loc,) = [x for x in parse_pages_local(pdf, EchoStructurer())[page_idx : page_idx + 1]]
    assert loc.status == "no_text_layer" and loc.markdown == "" and word in loc.reason


# ---------- C1 ----------
def test_a_one_page_doc_with_rotated_numbers_is_handed_back_not_ok_with_reversed_numbers() -> None:
    _assert_handed_back(_build(1, _rot_numbers), 0, "旋转")


def test_a_four_page_doc_with_rotated_numbers_is_handed_back_not_silently_stripped() -> None:
    pdf = _build(4, _rot_numbers)
    _assert_handed_back(pdf, 0, "旋转")
    assert [r.status for r in parse_pages_deterministic(pdf)[1:]] == ["ok"] * 3


def test_a_rotated_watermark_without_digits_is_still_stripped_and_the_page_stays_ok() -> None:
    pdf = _build(4, _rot_letters)
    assert [r.status for r in parse_pages_deterministic(pdf)] == ["ok"] * 4
    for r in parse_pages_deterministic(pdf):
        # 水印整词进了 stripped_text（pdfplumber 可能给出倒序，字符一个不少），且不在输出里
        assert Counter(r.stripped_text.replace(" ", "")) == Counter("CONFIDENTIAL")
        assert "CONFIDENTIAL" not in r.markdown and "LAITNEDIFNOC" not in r.markdown


def _landscape(angle: int) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    for p in range(4):
        if p == 1:
            c.saveState()
            c.translate(300, 400)
            c.rotate(angle)
            c.setFont("Helvetica", 10)
            rows = [("Item", "2023A", "2024E"), ("Revenue", "1,234.5", "1,560.2")]
            for i, (a, b, d) in enumerate(rows):
                c.drawString(-250, 100 - i * 15, a)
                c.drawString(-100, 100 - i * 15, b)
                c.drawString(0, 100 - i * 15, d)
            for i in range(4):
                c.drawString(-250, 20 - i * 15, BODY % i)
            c.restoreState()
        else:
            c.setFont("Helvetica", 10)
            for i in range(6):
                c.drawString(60, 800 - 130 - i * 15, BODY % i)
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.mark.parametrize("angle", [90, 270])
def test_a_mostly_rotated_page_is_handed_back_not_kept_as_body(angle: int) -> None:
    _assert_handed_back(_landscape(angle), 1, "旋转")


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_a_page_with_a_rotate_attribute_is_handed_back(rot: int) -> None:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(800, 600))
    c.setFont("Helvetica", 10)
    c.drawString(
        60, 500, "Net profit was 12.5 million and revenue 1,234.5 million, EPS 0.42 in 2024."
    )
    c.drawString(
        60, 485, "Gross margin improved to 31.5 percent compared with the previous fiscal year."
    )
    c.showPage()
    c.save()
    w = PdfWriter()
    w.append(PdfReader(BytesIO(buf.getvalue())))
    w.pages[0].rotate(rot)
    out = BytesIO()
    w.write(out)
    assert extract_pages(out.getvalue())[0].rotation == rot
    r = parse_pages_deterministic(out.getvalue())[0]
    assert r.status == "layout_unsupported" and "旋转" in r.reason
    loc = parse_pages_local(out.getvalue(), EchoStructurer())[0]
    assert loc.status == "no_text_layer" and "旋转" in loc.reason


def test_right_to_left_text_matrix_is_handed_back() -> None:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    c.setFont("Helvetica", 10)
    c.saveState()
    c.translate(500, 700)
    c.scale(-1, -1)  # 文字倒着走：text matrix a < 0，但 pdfplumber 仍报 upright=True
    c.drawString(0, 0, "Net profit was 12.5 million and revenue 1,234.5 million in 2024 overall.")
    c.restoreState()
    for i in range(5):
        c.drawString(60, 600 - i * 15, BODY % i)
    c.showPage()
    c.save()
    lay = extract_pages(buf.getvalue())[0]
    assert lay.rtl_text
    r = parse_pages_deterministic(buf.getvalue())[0]
    assert r.status == "layout_unsupported" and "从右向左" in r.reason
    assert parse_pages_local(buf.getvalue(), EchoStructurer())[0].status == "no_text_layer"


def test_layout_defaults_keep_old_constructors_working() -> None:
    lay = PageLayout(1, 600, 800, [], "")
    assert lay.rotation == 0 and lay.rtl_text is False


def test_typeset_does_not_emit_rotated_words_as_a_trailing_paragraph() -> None:
    rot = Word("TIALNEDIFNOC", 10, 20, 100, 300, False, 10.0, False)
    words = [Word("plain", 60, 90, 100, 110, True, 10.0, False), rot]
    (res,) = typeset_document([PageLayout(1, 600, 800, words, "plain\nTIALNEDIFNOC")])
    assert res.status == "layout_unsupported" and res.markdown == "" and "旋转" in res.reason


# ---------- I2 ----------
def _line(y: float, toks: list[str]) -> list[Word]:
    out, x = [], 60.0
    for t in toks:
        w = 10.0 * len(t)
        out.append(Word(t, x, x + w, y, y + 10, True, 10.0, False))
        x += w + 6
    return out


def test_a_fullwidth_sign_word_swap_cannot_pass_the_checks() -> None:
    filler = [
        _line(100 + 14 * i, ["Revenue", "grew", "strongly", "during", "the", "period", "and"])
        for i in range(4)
    ]
    a = _line(200, ["利润变动", "－", "５．０", "亿元"])
    b = _line(214, ["收入变动", "１", "－５．０", "亿元"])
    words = [w for ln in [*filler, a, b] for w in ln]
    text = "\n".join(" ".join(w.text for w in ln) for ln in [*filler, a, b])
    (r,) = parse_layouts_deterministic([PageLayout(1, 600, 800, words, text)])
    if r.status == "ok":
        assert ordered_numbers(r.markdown) == ordered_numbers(text)
    else:
        assert r.status == "closure_failed"


@pytest.mark.parametrize(
    ("a", "b"),
    [("－", "５"), ("５", "．"), ("亿", "－"), ("％", "亿"), ("（", "３"), ("元", "，")],
)
def test_no_space_is_never_dropped_next_to_number_syntax(a: str, b: str) -> None:
    expect = a in "－５．％（３，" or b in "－５．％（３，"
    if expect:
        # 逗号只有紧邻数字才算数字语法
        if "，" in (a, b) and not any(ch in "５３" for ch in (a, b)):
            return
        assert not _no_space_between(a, b)


def test_plain_cjk_characters_still_join_without_a_space() -> None:
    assert _no_space_between("字", "符")


def test_an_order_swap_of_numbers_in_the_output_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = _build(1, lambda c, p: None)
    good = parse_pages_deterministic(pdf)[0]
    first, last = BODY % 0, BODY % 5

    def fake(pages: Sequence[object]) -> list[TypesetResult]:
        md = good.markdown.replace(first, "@@").replace(last, first).replace("@@", last)
        return [TypesetResult("ok", md, "", [])]

    monkeypatch.setattr(pipeline, "typeset_document", fake)
    r = parse_pages_deterministic(pdf)[0]
    assert r.status == "closure_failed" and "顺序" in r.reason


# ---------- I3 ----------
def _stripped(pdf: bytes, page_idx: int) -> str:
    return parse_pages_deterministic(pdf)[page_idx].stripped_text


def test_lone_integers_that_are_not_page_numbers_are_kept() -> None:
    def extra(c: canvas.Canvas, p: int) -> None:
        c.drawString(60, 800 - 720, "Total backlog value (RMB million):")
        c.drawString(60, 800 - 745, str(4810 + 7 * p))

    pdf = _build(4, extra)
    rs = parse_pages_deterministic(pdf)
    for p, r in enumerate(rs):
        assert str(4810 + 7 * p) in r.markdown and r.stripped_text == ""


def test_real_page_numbers_with_a_constant_offset_are_still_stripped() -> None:
    def extra(c: canvas.Canvas, p: int) -> None:
        c.drawString(290, 800 - 780, str(p + 3))  # 封面不编号：偏移恒定

    for p, r in enumerate(parse_pages_deterministic(_build(5, extra))):
        assert r.stripped_text == str(p + 3)


def test_inline_ratio_tokens_that_change_per_page_are_data() -> None:
    vals = ["3/4", "7/9", "1/2", "5/8"]

    def extra(c: canvas.Canvas, p: int) -> None:
        c.drawString(60, 800 - 40, f"Board approval ratio {vals[p]} shares voted")

    for p, r in enumerate(parse_pages_deterministic(_build(4, extra))):
        assert vals[p] in r.markdown and r.stripped_text == ""


def test_a_slash_page_token_equal_to_the_page_index_is_still_a_page_number() -> None:
    def extra(c: canvas.Canvas, p: int) -> None:
        c.drawString(60, 800 - 40, f"ACME report {p + 1}/4")

    for p, r in enumerate(parse_pages_deterministic(_build(4, extra))):
        assert r.stripped_text == f"ACME report {p + 1}/4"


def test_a_repeated_two_column_table_header_keeps_its_unit() -> None:
    def extra(c: canvas.Canvas, p: int) -> None:
        c.drawString(60, 800 - 40, "Segment")
        c.drawString(300, 800 - 40, "Amount (RMB 100 million)")
        c.drawString(60, 800 - 90, "Chips")
        c.drawString(300, 800 - 90, f"{12 + p}.3")

    for r in parse_pages_deterministic(_build(4, extra)):
        assert "100 million" in r.markdown and r.stripped_text == ""
