from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from reportlab.pdfgen import canvas

from ragdemo.parse.local.decor import MIN_DOC_PAGES, strip_decor
from ragdemo.parse.local.layout import PageLayout, Word, extract_pages
from ragdemo.parse.local.pipeline import parse_pages_local
from ragdemo.parse.local.structure import ClosureFailed, LlmStructurer

H = 800.0
W = 600.0
BODY = "Body paragraph with revenue 91.2 million and profit 19.3 million this period"


def _line(text: str, y: float, x: float = 60.0, *, upright: bool = True) -> list[Word]:
    """一行：按空格切词，词宽 6/字符，词间距 4（正文句子型，不会被当成表格行）。"""
    out: list[Word] = []
    for t in text.split():
        out.append(Word(t, x, x + 6 * len(t), y, y + 10, upright))
        x += 6 * len(t) + 4
    return out


def _page(n: int, lines: Sequence[tuple[str, float]], extra: Sequence[Word] = ()) -> PageLayout:
    words = [w for text, y in lines for w in _line(text, y)] + list(extra)
    return PageLayout(n, W, H, words, "\n".join(t for t, _ in lines))


def _doc(n: int = 6) -> list[PageLayout]:
    return [
        _page(i, [("ACME 688041 Dec 2025", 20), (f"{BODY} {i}", 400), (f"{i} / {n}", 770)])
        for i in range(1, n + 1)
    ]


def _content(text: str) -> Counter[str]:
    return Counter(re.sub(r"\s+", "", text))


def test_repeated_header_and_page_number_are_stripped_and_recorded() -> None:
    res = strip_decor(_doc())
    for sp in res:
        assert "688041" not in sp.layout.text
        assert "688041" in sp.stripped_text and "2025" in sp.stripped_text
        assert "91.2" in sp.layout.text  # 正文保留
        assert not any(w.text == "688041" for w in sp.layout.words)
    assert "3 / 6" in res[2].stripped_text and "3 / 6" not in res[2].layout.text
    assert res[0].stripped_chars == len("ACME688041Dec2025") + len("1/6")


def test_stripping_is_a_lossless_split_of_the_text_layer() -> None:
    pages = _doc()
    for before, sp in zip(pages, strip_decor(pages), strict=True):
        assert _content(sp.stripped_text) + _content(sp.layout.text) == _content(before.text)


def test_documents_shorter_than_the_minimum_strip_nothing() -> None:
    pages = _doc(MIN_DOC_PAGES - 1)
    for before, sp in zip(pages, strip_decor(pages), strict=True):
        assert sp.layout is before and sp.stripped_text == ""
    rotated = [_page(i, [(BODY, 400)], _line("WATERMARK", 300, upright=False)) for i in range(1, 4)]
    assert all(sp.stripped_text == "" for sp in strip_decor(rotated))


def test_the_same_text_in_the_body_area_is_not_stripped() -> None:
    pages = [
        _page(i, [("ACME 688041 Dec 2025", 20), ("ACME 688041 Dec 2025", 400), (BODY, 450)])
        for i in range(1, 7)
    ]
    for sp in strip_decor(pages):
        assert sp.layout.text.count("ACME 688041 Dec 2025") == 1  # 顶带那行被剥，正文区的留下
        assert any(w.text == "688041" and w.top == 400 for w in sp.layout.words)


def test_rows_inside_a_table_region_are_never_stripped() -> None:
    def table_rows() -> list[Word]:
        rows: list[Word] = []
        for r, y in enumerate((10, 24, 38)):  # 全部落在顶带内（页高 800 的 8% = 64）
            for c, x in enumerate((60, 260, 460)):
                rows.append(Word(f"R{r}C{c}", x, x + 30, y, y + 10))
        return rows

    pages = [_page(i, [(BODY, 400)], table_rows()) for i in range(1, 7)]
    for sp in strip_decor(pages):
        assert sp.stripped_text == ""
        assert sum(1 for w in sp.layout.words if w.text.startswith("R")) == 9


def test_page_number_rows_that_carry_their_own_page_index_are_stripped_without_repetition() -> None:
    # 修复轮 1：只有"第 N 页 / N/M 且 N 等于本页页码"才无需重复；孤立整数需要"数值 - 页序号"恒定
    for form in ["第 3 页", "3 / 12", "第 3 页 共 12 页"]:
        pages = [_page(i, [(BODY, 400)]) for i in range(1, 6)]
        pages[2] = _page(3, [(BODY, 400), (form, 770)])
        res = strip_decor(pages)
        assert res[2].stripped_text == form, form
        assert form not in res[2].layout.text.split("\n")
        assert res[2].layout.text.endswith("this period")
        assert all(r.stripped_text == "" for k, r in enumerate(res) if k != 2)


def test_a_lone_integer_in_the_footer_is_a_page_number_only_if_it_tracks_the_page_index() -> None:
    for fmt in ["{}", "- {} -"]:
        tracked = [_page(i, [(BODY, 400), (fmt.format(i + 2), 770)]) for i in range(1, 7)]
        assert [sp.stripped_text for sp in strip_decor(tracked)] == [
            fmt.format(i + 2) for i in range(1, 7)
        ]
        lone = [_page(i, [(BODY, 400)]) for i in range(1, 7)]
        lone[2] = _page(3, [(BODY, 400), (fmt.format(3), 770)])
        assert strip_decor(lone)[2].stripped_text == ""  # 只出现一页：不是页码，保留


def test_a_page_token_with_the_wrong_page_index_is_data() -> None:
    pages = [_page(i, [(BODY, 400)]) for i in range(1, 6)]
    pages[2] = _page(3, [(BODY, 400), ("5 / 12", 770)])
    assert strip_decor(pages)[2].stripped_text == ""


def test_a_number_row_that_is_not_a_page_number_is_kept() -> None:
    pages = [_page(i, [(BODY, 400)]) for i in range(1, 6)]
    pages[2] = _page(3, [(BODY, 400), ("12.5 3.1", 770)])
    assert strip_decor(pages)[2].stripped_text == ""


def test_a_header_row_whose_numbers_change_per_page_is_data_not_decor() -> None:
    pages = [_page(i, [(f"Close price {10 + i}.5 CNY", 20), (BODY, 400)]) for i in range(1, 7)]
    for sp in strip_decor(pages):
        assert sp.stripped_text == "" and "Close price" in sp.layout.text


def test_header_with_an_inline_page_number_is_still_recognised_as_repeated() -> None:
    pages = [_page(i, [(f"ACME 688041 第 {i} 页", 20), (BODY, 400)]) for i in range(1, 7)]
    for i, sp in enumerate(strip_decor(pages), start=1):
        assert sp.stripped_text == f"ACME 688041 第 {i} 页"


def test_a_line_repeated_on_too_few_pages_is_kept() -> None:
    pages = [_page(i, [(BODY, 400)]) for i in range(1, 9)]
    for i in (1, 2):  # 8 页需要 >= 4 页；只重复 2 页
        pages[i] = _page(i + 1, [("ACME 688041", 20), (BODY, 400)])
    assert all(sp.stripped_text == "" for sp in strip_decor(pages))


def test_rotated_words_are_stripped_with_reason_rotated() -> None:
    pages = [_page(i, [(BODY, 400)], _line("WATERMARK", 300, upright=False)) for i in range(1, 7)]
    res = strip_decor(pages)
    assert res[0].items[0][0] == "rotated"
    assert "WATERMARK" in res[0].stripped_text and "WATERMARK" not in res[0].layout.text
    assert all(w.upright for w in res[0].layout.words)


def test_a_page_that_is_mostly_rotated_is_body_not_watermark() -> None:
    pages = [
        _page(i, [], _line("Rotated landscape body text 91.2", 300, upright=False))
        for i in range(1, 7)
    ]
    for sp in strip_decor(pages):
        assert sp.stripped_text == "" and len(sp.layout.words) == 5


# ---- 合成 PDF：整条链路 ----


class Echo:
    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
        return page_text + "\n\n" + "\n\n".join(table_markdowns)


def _make_pdf(n: int = 6) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    for i in range(1, n + 1):
        c.setFont("Helvetica", 10)
        c.drawString(60, 842 - 30, "ACME Holdings 688041 Dec 2025")
        c.drawString(285, 25, f"{i} / {n}")
        c.setFont("Helvetica", 11)
        c.drawString(60, 600, f"Revenue grew steadily during the reporting period number {i}.")
        c.drawString(60, 580, "Management expects stable margins over the coming quarters.")
        for r, (label, a, b) in enumerate(
            [("Item", "2024", "2023"), ("Revenue", "91.2", "60.1"), ("Net profit", "19.3", "12.7")]
        ):
            y = 500 - 20 * r
            c.drawString(60, y, label)
            c.drawString(260, y, a)
            c.drawString(360, y, b)
        if i == 3:
            c.saveState()
            c.translate(560, 300)
            c.rotate(90)
            c.setFont("Helvetica", 30)
            c.drawString(0, 0, "CONFIDENTIAL")
            c.restoreState()
        c.showPage()
    c.save()
    return buf.getvalue()


def test_layout_marks_rotated_words_as_not_upright() -> None:
    pages = extract_pages(_make_pdf())
    assert any(not w.upright for w in pages[2].words)
    assert all(w.upright for w in pages[0].words)


def test_strip_decor_on_a_synthetic_pdf() -> None:
    pages = extract_pages(_make_pdf())
    res = strip_decor(pages)
    for before, sp in zip(pages, res, strict=True):
        assert "688041" not in sp.layout.text and "688041" in sp.stripped_text
        assert "Revenue" in sp.layout.text and "91.2" in sp.layout.text
        assert _content(sp.stripped_text) + _content(sp.layout.text) == _content(before.text)
    (rotated,) = [t for reason, t in res[2].items if reason == "rotated"]
    assert _content(rotated) == _content("CONFIDENTIAL")  # pdfplumber 可能给出倒序，字符一个不少
    assert "CONFIDENTIAL" not in res[2].layout.text and "LAITNEDIFNOC" not in res[2].layout.text


def test_parse_pages_local_output_has_no_header_footer_or_watermark() -> None:
    results = parse_pages_local(_make_pdf(), Echo())
    assert [r.status for r in results] == ["ok"] * 6
    for r in results:
        assert "688041" not in r.markdown and "2025" not in r.markdown
        assert "/ 6" not in r.markdown and "CONFIDENTIAL" not in r.markdown
        assert "| Revenue | 91.2 | 60.1 |" in r.markdown and "19.3" in r.markdown
        assert "688041" in r.stripped_text
    assert _content("CONFIDENTIAL") <= _content(results[2].stripped_text)


class _FakeHttp:
    """假 HTTP：回显 prompt 里的正文，并把 91.2 改成 91.3（模拟模型改数字）。"""

    def post_json(self, path: str, body: dict[str, Any]) -> SimpleNamespace:
        prompt = body["messages"][0]["content"]
        reply = prompt.replace("91.2", "91.3")
        return SimpleNamespace(
            payload={"choices": [{"finish_reason": "stop", "message": {"content": reply}}]}
        )


def test_a_tampered_body_number_still_fails_closure_with_stripping_on() -> None:
    structurer = LlmStructurer(_FakeHttp(), model="m")  # type: ignore[arg-type]
    results = parse_pages_local(_make_pdf(), structurer)
    assert all(r.status == "closure_failed" for r in results)
    assert "91.3" in results[0].violations
    assert results[0].markdown == ""
    assert "688041" in results[0].stripped_text


def test_a_page_whose_body_is_too_short_after_stripping_is_still_handed_back() -> None:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    for i in range(1, 7):
        c.setFont("Helvetica", 10)
        c.drawString(60, 842 - 30, "ACME Holdings 688041 Dec 2025 annual report header text")
        c.drawString(285, 25, f"{i} / 6")
        c.drawString(60, 600, "tiny body")
        c.showPage()
    c.save()
    results = parse_pages_local(buf.getvalue(), Echo())
    assert [r.status for r in results] == ["no_text_layer"] * 6
    assert "688041" in results[0].stripped_text


def test_closure_failure_is_still_reported_with_stripping_on() -> None:
    class Inventing:
        def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
            raise ClosureFailed(["999.9"])

    results = parse_pages_local(_make_pdf(), Inventing())
    assert all(r.status == "closure_failed" and r.stripped_text for r in results)
