from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.local.typeset import body_font_size, typeset_document

W, H = 600.0, 800.0
LEAD = 14.0  # 正文行距（中点到中点）


def line(
    text: str,
    y: float,
    *,
    size: float = 10.0,
    bold: bool = False,
    x: float = 60.0,
    cjk: bool = False,
) -> list[Word]:
    """一行：拉丁文按空格切词（词宽 0.5*size，词距 0.4*size）；中文整行一词（宽 size）。"""
    tokens = [text] if cjk else text.split()
    out: list[Word] = []
    for t in tokens:
        w = size * len(t) * (1.0 if cjk else 0.5)
        out.append(Word(t, x, x + w, y - size / 2, y + size / 2, True, size, bold))
        x += w + 0.4 * size
    return out


def page(*lines: list[Word], n: int = 1) -> PageLayout:
    words = [w for ln in lines for w in ln]
    text = "\n".join(" ".join(w.text for w in ln) for ln in lines)
    return PageLayout(n, W, H, words, text)


def md_of(*lines: list[Word]) -> str:
    (res,) = typeset_document([page(*lines)])
    assert res.status == "ok", res.reason
    return res.markdown


def body_lines(start: float, n: int, *, cjk: bool = False) -> list[list[Word]]:
    if cjk:
        return [
            line("这是一段正文内容用来撑满版面" + "甲乙丙丁戊"[i % 5], start + i * LEAD, cjk=True)
            for i in range(n)
        ]
    return [
        line(f"plain body words number {i} of the paragraph", start + i * LEAD) for i in range(n)
    ]


def test_body_font_size_is_the_character_weighted_mode_rounded_to_half_points() -> None:
    p = page(
        line("a" * 100, 100, size=10.2),
        line("b" * 20, 120, size=18.0),
        line("c" * 60, 140, size=10.3),
    )
    assert body_font_size([p]) == 10.0  # 10.2 → 10.0（100 字符）；10.3 → 10.5（60 字符）
    q = page(line("a" * 50, 100, size=10.3), line("b" * 20, 120, size=18.0))
    assert body_font_size([q]) == 10.5
    assert body_font_size([page(line("x", 10, size=0.0))]) == 0.0


def test_a_larger_font_line_becomes_a_heading_with_only_a_hash_prefix() -> None:
    md = md_of(line("Company Overview", 100, size=18), *body_lines(130, 3))
    assert md.splitlines()[0] == "# Company Overview"
    assert "plain body words number 0 of the paragraph" in md


def test_heading_levels_follow_distinct_sizes_and_cap_at_three() -> None:
    md = md_of(
        line("Top", 60, size=24),
        line("Second", 100, size=20),
        line("Third", 140, size=16),
        line("Fourth", 180, size=14),
        *body_lines(210, 30),
    )
    heads = [ln for ln in md.splitlines() if ln.startswith("#")]
    assert heads == ["# Top", "## Second", "### Third", "### Fourth"]


def test_big_text_ending_in_sentence_punctuation_or_too_long_is_not_a_heading() -> None:
    md = md_of(
        line("本期收入增长明显。", 60, size=18, cjk=True),
        line("x" * 61, 100, size=18, cjk=True),
        line("Short title", 140, size=18),
        *body_lines(170, 30),
    )
    heads = [ln for ln in md.splitlines() if ln.startswith("#")]
    assert heads == ["# Short title"]


def test_a_bold_short_line_with_extra_spacing_is_a_level_three_heading() -> None:
    md = md_of(
        *body_lines(100, 3),
        line("Risk Factors", 100 + 3 * LEAD + 30, bold=True),
        line("plain body after the heading line", 100 + 3 * LEAD + 60),
        *body_lines(100 + 3 * LEAD + 74, 25),
    )
    assert "### Risk Factors" in md.splitlines()


def test_a_bold_line_in_normal_spacing_or_ending_with_a_period_is_not_a_heading() -> None:
    md = md_of(
        *body_lines(100, 2),
        line("Bold in flow", 100 + 2 * LEAD, bold=True),
        *body_lines(100 + 3 * LEAD, 25),
        line("Bold sentence.", 100 + 28 * LEAD + 40, bold=True),
        line("tail text of body", 100 + 28 * LEAD + 80),
    )
    assert not any(ln.startswith("#") for ln in md.splitlines())


def test_unknown_font_sizes_never_produce_headings() -> None:
    md = md_of(line("Big Looking", 60, size=0.0), *body_lines(100, 3))
    assert not md.startswith("#")


def test_consecutive_lines_merge_into_a_paragraph_without_spaces_between_cjk() -> None:
    md = md_of(*body_lines(100, 3, cjk=True))
    unit = "这是一段正文内容用来撑满版面"
    assert md == unit + "甲" + unit + "乙" + unit + "丙"


def test_latin_lines_join_with_one_space() -> None:
    md = md_of(*body_lines(100, 2))
    assert (
        md
        == "plain body words number 0 of the paragraph plain body words number 1 of the paragraph"
    )


def test_a_large_vertical_gap_starts_a_new_paragraph() -> None:
    top = body_lines(100, 3)
    bottom = [line("next paragraph starts here", 100 + 2 * LEAD + 4 * LEAD)]
    md = md_of(*top, *bottom, *body_lines(300, 20))
    assert md.split("\n\n")[1] == "next paragraph starts here"


def test_a_first_line_indent_starts_a_new_paragraph() -> None:
    md = md_of(
        *body_lines(100, 3),
        line("indented first line of a new paragraph", 100 + 3 * LEAD, x=60 + 25),
        line("plain body words continuing it", 100 + 4 * LEAD),
    )
    paras = md.split("\n\n")
    assert len(paras) == 2 and paras[1].startswith("indented first line")


def test_a_hanging_indent_continuation_stays_in_its_list_item() -> None:
    md = md_of(
        *body_lines(100, 3),
        line("1. item that wraps onto", 100 + 3 * LEAD),
        line("a second indented line", 100 + 4 * LEAD, x=60 + 25),
    )
    assert md.split("\n\n")[-1] == "1. item that wraps onto a second indented line"


def test_a_symbol_font_bullet_is_a_list_marker_and_its_hanging_indent_stays_in_the_item() -> None:
    bullet = chr(0xF06C)  # Wingdings / Symbol 字体里的圆点，pdfminer 抽出来是私有区字符
    md = md_of(
        *body_lines(100, 3),
        line(f"{bullet} first bullet item that wraps", 100 + 3 * LEAD),
        line("onto an indented line", 100 + 4 * LEAD, x=60 + 25),
        line(f"{bullet} second bullet", 100 + 5 * LEAD),
    )
    paras = md.split("\n\n")
    assert paras[1] == f"{bullet} first bullet item that wraps onto an indented line"
    assert paras[2] == f"{bullet} second bullet"


def test_a_short_last_line_ending_a_sentence_starts_a_new_paragraph() -> None:
    wide = [line("w " * 80, 100 + i * LEAD) for i in range(3)]
    short = line("Short end.", 100 + 3 * LEAD)
    nxt = line("fresh start of text", 100 + 4 * LEAD)
    assert md_of(*wide, short, nxt).split("\n\n")[-1] == "fresh start of text"


def test_list_markers_start_new_paragraphs_and_are_kept_verbatim() -> None:
    marks = ["1.", "（1）", "一、", "•", "(2)", "二、"]
    items = [line(f"{m} item text {i}", 100 + i * LEAD) for i, m in enumerate(marks)]
    md = md_of(*body_lines(100 - 3 * LEAD, 3), *items)
    paras = md.split("\n\n")
    assert paras[1:] == [f"{m} item text {i}" for i, m in enumerate(marks)]


def test_a_number_with_a_decimal_point_at_line_start_is_not_a_list_marker() -> None:
    md = md_of(line("revenue was", 100), line("91.2 million this year", 100 + LEAD))
    assert md == "revenue was 91.2 million this year"


def _table_rows(y0: float) -> list[list[Word]]:
    data = [("Item", "2024", "2023"), ("Revenue", "91.2", "60.1"), ("Profit", "19.3", "12.7")]
    rows: list[list[Word]] = []
    for i, (a, b, c) in enumerate(data):
        y = y0 + i * LEAD
        rows.append(line(a, y, x=60) + line(b, y, x=260) + line(c, y, x=420))
    return rows


def test_a_table_is_inserted_at_its_position_and_its_words_stay_out_of_paragraphs() -> None:
    md = md_of(*body_lines(100, 2), *_table_rows(200), *body_lines(300, 2))
    paras = md.split("\n\n")
    assert len(paras) == 3
    assert paras[1].splitlines()[0] == "| Item | 2024 | 2023 |"
    assert "| Revenue | 91.2 | 60.1 |" in paras[1]
    assert "Revenue" not in paras[0] and "Revenue" not in paras[2]


def test_a_large_font_row_inside_a_table_is_not_a_heading() -> None:
    rows = _table_rows(200)
    rows[0] = [
        Word(w.text, w.x0, w.x1, w.top - 4, w.bottom + 4, True, 18.0, False) for w in rows[0]
    ]
    md = md_of(*body_lines(100, 3), *rows, *body_lines(300, 20))
    assert not any(ln.startswith("#") for ln in md.splitlines())


def _two_columns(n_rows: int = 12) -> list[list[Word]]:
    return [
        line(f"left column text row {i} here", 100 + i * LEAD, x=60)
        + line(f"right column text row {i} there", 100 + i * LEAD, x=330)
        for i in range(n_rows)
    ]


def test_a_two_column_page_is_layout_unsupported_and_has_no_markdown() -> None:
    (res,) = typeset_document([page(*_two_columns())])
    assert res.status == "layout_unsupported" and res.markdown == ""
    assert "多栏" in res.reason


def test_a_single_column_page_with_a_few_gapped_rows_is_not_multi_column() -> None:
    gapped = [
        line(f"left {i}", 100 + i * LEAD, x=60) + line("right", 100 + i * LEAD, x=330)
        for i in range(2)
    ]
    res = typeset_document([page(*gapped, *body_lines(130, 12))])
    assert res[0].status == "ok"


def test_a_right_aligned_gap_outside_the_middle_of_the_page_is_not_a_gutter() -> None:
    rows = [
        line("Title text", 100 + i * LEAD, x=60) + line("2025", 100 + i * LEAD, x=540)
        for i in range(6)
    ]
    assert typeset_document([page(*rows, *body_lines(250, 6))])[0].status == "ok"


def test_rotated_words_are_kept_as_their_own_trailing_paragraph() -> None:
    rot = Word("TIALNEDIFNOC", 10, 20, 100, 300, False, 10.0, False)
    p = page(*body_lines(100, 2))
    p = PageLayout(1, W, H, [*p.words, rot], p.text + "\nTIALNEDIFNOC")
    (res,) = typeset_document([p])
    assert res.markdown.split("\n\n")[-1] == "TIALNEDIFNOC"


def _content(text: str) -> Counter[str]:
    return Counter(ch for ch in text if ch.isalnum())


def _page_chars(pages: Sequence[PageLayout]) -> Counter[str]:
    return _content("".join(w.text for p in pages for w in p.words))


def test_provenance_content_characters_and_numbers_equal_the_text_layer() -> None:
    p = page(
        line("年度报告摘要", 60, size=20, cjk=True),
        *body_lines(100, 3, cjk=True),
        *_table_rows(200),
        line("1. first item 3.5% up", 260),
        line("（2）second item -4.2 亿元", 260 + LEAD),
        line("Risk Factors", 320, bold=True),
        *body_lines(350, 20),
    )
    (res,) = typeset_document([p])
    assert res.status == "ok"
    assert _content(res.markdown) == _page_chars([p])
    assert numbers_in(res.markdown) == numbers_in(" ".join(w.text for w in p.words))


def test_headings_only_gain_hash_marks() -> None:
    p = page(line("Alpha Beta 12", 60, size=20), *body_lines(100, 20))
    (res,) = typeset_document([p])
    assert re.sub(r"^#+ ", "", res.markdown.splitlines()[0]) == "Alpha Beta 12"


def test_document_level_heading_sizes_are_shared_across_pages() -> None:
    p1 = page(line("Chapter", 60, size=24), *body_lines(100, 20), n=1)
    p2 = page(line("Section", 60, size=18), *body_lines(100, 20), n=2)
    r1, r2 = typeset_document([p1, p2])
    assert r1.markdown.startswith("# Chapter") and r2.markdown.startswith("## Section")


def test_an_empty_page_gives_empty_ok_markdown() -> None:
    (res,) = typeset_document([PageLayout(1, W, H, [], "")])
    assert res.status == "ok" and res.markdown == "" and res.tables == []


def test_two_consecutive_heading_lines_stay_separate_heading_lines() -> None:
    md = md_of(
        line("Company Name", 60, size=18),
        line("Annual Report", 60 + 25, size=18),
        *body_lines(120, 20),
    )
    assert [ln for ln in md.splitlines() if ln][:2] == ["# Company Name", "# Annual Report"]


def test_curly_quotes_next_to_cjk_do_not_get_a_space() -> None:
    md = md_of(
        line("简称“海光信息”或", 100, cjk=True),
        line("“公司”预计增长", 100 + LEAD, cjk=True),
    )
    assert md == "简称“海光信息”或“公司”预计增长"
