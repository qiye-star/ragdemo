# tests/parse/local/test_l0.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragdemo.parse.local import l0 as l0_mod
from ragdemo.parse.local.l0 import (
    REASON_ROTATED,
    L0Page,
    page_kinds,
    parse_layouts_l0,
    parse_pdf_l0,
    tablelike_run,
)
from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.parse.local.samples import layout_from_fixture
from ragdemo.parse.local.typeset import TypesetResult
from tests.parse.local.test_chart import TABLE, bar_chart, bar_page, body, pdf_page, plain_table
from tests.parse.local.test_typeset import LEAD, H, W, body_lines, line, page

RAW_RESEARCH = Path("docs/rawdata/688041-hygon/research")
LOCAL_FIXTURES = Path("data/experiments/l0v2/fixtures")  # 研报脱敏夹具只在本机（不入库）


def _plain(n: int) -> PageLayout:
    return page(*body_lines(100, 12), n=n)


def _table(n: int) -> PageLayout:
    rows = [
        line(a, 300 + i * LEAD, x=60)
        + line(b, 300 + i * LEAD, x=260)
        + line(c, 300 + i * LEAD, x=420)
        for i, (a, b, c) in enumerate(
            [
                ("Item", "2024", "2023"),
                ("Revenue", "91.2", "60.1"),
                ("Cost", "40.5", "30.2"),
                ("Profit", "50.7", "29.9"),
            ]
        )
    ]
    return page(*body_lines(100, 6), *rows, n=n)


def _two_columns(n: int, *, numbered: bool = False) -> PageLayout:
    # numbered=False：行内没有数字，只可能是多栏；
    # numbered=True：每行两栏各带一个数字，同时满足"疑似表格行"
    tag = (lambda i: str(i)) if numbered else (lambda i: "abcdefghijkl"[i])
    rows = [
        line(f"left column text row {tag(i)} here", 100 + i * LEAD, x=60)
        + line(f"right column text row {tag(i)} there", 100 + i * LEAD, x=330)
        for i in range(12)
    ]
    return page(*rows, n=n)


def _statement(n: int) -> PageLayout:
    rows = [line("合并资产负债表", 100, cjk=True)]
    rows += [line(f"item{i} {i},123.45 {i},678.90", 120 + i * LEAD) for i in range(12)]
    return page(*rows, n=n)


def _chart(n: int) -> PageLayout:
    return page(
        *body_lines(100, 6),
        line("40%", 300),
        line("20%", 300 + LEAD),
        line("0%", 300 + 2 * LEAD),
        n=n,
    )


def _rotated(n: int) -> PageLayout:
    p = _plain(n)
    rot = Word("TIALNEDIFNOC", 10, 20, 100, 300, False, 10.0, False)
    return PageLayout(n, W, H, [*p.words, rot], p.text + "\nTIALNEDIFNOC")


def test_page_kinds_route_only_plain_text_pages_through_the_typesetter() -> None:
    # _chart 只有碎片行、没有任何图形对象：检测器找不到区域 → 检测失败 → chart_unresolved 降级
    pages = [
        _plain(1),
        _table(2),
        _two_columns(3),
        _statement(4),
        PageLayout(5, W, H, [], ""),
        _chart(6),
    ]
    out = parse_layouts_l0(pages)
    assert [p.kind for p in out] == [
        "plain_text",
        "table",
        "multicolumn",
        "financial_statement",
        "no_text_layer",
        "chart_unresolved",
    ]
    assert [p.accepted for p in out] == [True, False, False, False, False, False]
    assert [p.result.status for p in out] == [
        "ok",
        "layout_unsupported",
        "layout_unsupported",
        "layout_unsupported",
        "no_text_layer",
        "layout_unsupported",
    ]
    assert out[0].result.markdown and all(not p.result.markdown for p in out[1:])


def test_rotated_words_left_by_decor_stripping_make_the_page_unsupported() -> None:
    # < 4 页的文档 strip_decor 不剥旋转字（>= 4 页时旋转字被剥进 stripped_text，不会留到这里）
    (p,) = parse_layouts_l0([_rotated(1)])
    assert p.kind == "multicolumn" and p.result.reason.endswith(REASON_ROTATED) and not p.accepted


def test_two_consecutive_gapped_rows_with_numbers_make_a_table_page() -> None:
    rows = [
        line("营业收入", 300 + i * LEAD, cjk=True) + line("91.2 60.1", 300 + i * LEAD, x=300)
        for i in range(2)
    ]
    assert tablelike_run(rows, 10.0) == 2
    (p,) = parse_layouts_l0([page(*body_lines(100, 6), *rows)])
    assert p.kind == "table" and "疑似表格行" in p.result.reason


def test_a_typesetter_bug_on_a_plain_page_is_closure_failed_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = l0_mod.typeset_document

    def corrupt(layouts: list[PageLayout]) -> list[TypesetResult]:
        return [
            TypesetResult(r.status, r.markdown.replace("3", "8"), r.reason, r.tables)
            for r in real(layouts)
        ]

    monkeypatch.setattr(l0_mod, "typeset_document", corrupt)
    (p,) = parse_layouts_l0([_plain(1)])
    assert p.kind == "plain_text" and p.result.status == "closure_failed" and not p.accepted


def test_accepted_markdown_keeps_every_content_character_of_the_text_layer() -> None:
    (p,) = parse_layouts_l0([_plain(1)])
    from collections import Counter

    content = Counter(ch for ch in p.result.markdown if ch.isalnum())
    assert content == Counter(ch for w in _plain(1).words for ch in w.text if ch.isalnum())


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("r_20250424_minsheng_p1", "multicolumn"),
        ("r_20240819_taipingyang_p1", "multicolumn"),
        ("r_20260415_shanxi_p1", "multicolumn"),
        ("r_20250807_kaiyuan_p1", "multicolumn"),
    ],
)
def test_masked_real_sidebar_pages_are_never_accepted(name: str, kind: str) -> None:
    path = LOCAL_FIXTURES / f"{name}.json"
    if not path.exists():
        pytest.skip(
            "研报脱敏夹具不入库；本机没有 data/experiments/l0v2/fixtures（先跑 l0v2.py fixtures）"
        )
    lay = layout_from_fixture(json.loads(path.read_text(encoding="utf-8")))
    (p,) = parse_layouts_l0([lay])
    # U-D3：侧栏页若同时有两段式"标签—数值"行，新次序下标签是 table；去向（不被接受）不变
    assert p.kind in (kind, "table") and not p.accepted


def test_a_plain_page_with_a_detected_chart_is_accepted_and_carries_the_figure() -> None:
    pdf = pdf_page(bar_page)
    (p,) = parse_pdf_l0(pdf)
    assert p.kind == "plain_text" and p.accepted
    (fig,) = p.figures
    assert fig.caption == "图1：营业收入与增速" and "图1：营业收入与增速" in p.result.markdown
    assert (
        "40%" not in p.result.markdown
        and "40%" in p.figure_text
        and "40%" in p.result.stripped_text
    )
    assert p.body is not None and all(w.text != "40%" for w in p.body.words)


def test_a_chart_directly_above_a_table_still_makes_a_table_page_with_every_table_word_kept() -> (
    None
):
    pdf = pdf_page(lambda c: (body(c, 80, 2), bar_chart(c, 80, 120), plain_table(c, 284)))
    (p,) = parse_pdf_l0(pdf)
    assert p.kind == "table" and not p.accepted and len(p.figures) == 1
    assert p.body is not None
    kept = {w.text for w in p.body.words}
    assert all(cell in kept for row in TABLE for cell in row)


def test_real_v1_closure_false_alarm_page_is_routed_away_before_typesetting() -> None:
    hits = sorted(RAW_RESEARCH.glob("*_AP202503051644062530.pdf"))
    if not hits:
        pytest.skip("原始研报不入库（docs/rawdata/.gitignore），本机没有这份文件")
    first: L0Page = parse_pdf_l0(hits[0].read_bytes())[0]
    assert first.kind != "plain_text" and first.result.status != "closure_failed"


def test_u_d3_table_like_rows_are_checked_before_multicolumn_and_old_order_is_kept() -> None:
    pages = [_two_columns(1, numbered=True)]
    assert [p.kind for p in parse_layouts_l0(pages)] == ["table"]  # 新次序（U-D3）
    assert page_kinds(pages, multicolumn_first=False) == ["table"]
    assert page_kinds(pages, multicolumn_first=True) == ["multicolumn"]  # 计划原文的旧次序
    # 次序只改标签、不改去向：两种次序下都不被接受
    assert not any(p.accepted for p in parse_layouts_l0(pages))
    # 没有数字的两栏页：两种次序都是 multicolumn
    plain_two = [_two_columns(1)]
    assert (
        page_kinds(plain_two, multicolumn_first=False)
        == page_kinds(plain_two, multicolumn_first=True)
        == ["multicolumn"]
    )


def test_short_numeric_cells_inside_a_table_are_not_residual_chart_fragments() -> None:
    # 裁决 2：三行以上的短数字单元格（表格行）不能把页判成 chart_unresolved
    rows = [line("项目", 300, x=60, cjk=True) + line("2024", 300, x=260) + line("2023", 300, x=420)]
    rows += [
        line(a, 300 + (i + 1) * LEAD, x=60)
        + line(b, 300 + (i + 1) * LEAD, x=260)
        + line(c, 300 + (i + 1) * LEAD, x=420)
        for i, (a, b, c) in enumerate(
            [("1", "9.1", "8.2"), ("2", "9.3", "8.4"), ("3", "9.5", "8.6")]
        )
    ]
    for first in (False, True):
        kinds = page_kinds([page(*body_lines(100, 6), *rows)], multicolumn_first=first)
        assert kinds == ["table"], first
