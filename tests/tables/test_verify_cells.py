from __future__ import annotations

from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.tables.grid import GridCell, TableGrid
from ragdemo.tables.verify_cells import (
    PageWords,
    check_cell,
    page_words_from_layouts,
    summarize_grid,
    verify_grid,
    words_in_bbox,
)

PAGE = PageWords(
    page=1,
    width=100.0,
    height=100.0,
    words=(Word("1,234.50", 12.0, 30.0, 12.0, 18.0), Word("营业收入", 60.0, 80.0, 12.0, 18.0)),
)
PAGES = {1: PAGE, 2: PageWords(page=2, width=100.0, height=100.0, words=())}


def test_words_in_bbox_uses_word_center() -> None:
    assert [w.text for w in words_in_bbox(PAGE, (0.1, 0.1, 0.5, 0.2))] == ["1,234.50"]


def test_cell_checks() -> None:
    box = (0.1, 0.1, 0.5, 0.2)
    assert check_cell(GridCell(1, 1, 1, 1, "1,234.50", page=1, bbox=box), PAGES) == "match"
    assert check_cell(GridCell(1, 1, 1, 1, "1,284.50", page=1, bbox=box), PAGES) == "mismatch"
    assert check_cell(GridCell(1, 0, 1, 1, "营业收入", page=1, bbox=box), PAGES) == "no_numbers"
    assert check_cell(GridCell(1, 1, 1, 1, "7", page=2, bbox=box), PAGES) == "no_text_layer"
    assert check_cell(GridCell(1, 1, 1, 1, "7", page=1, bbox=None), PAGES) == "not_checked"


def test_column_swap_is_caught_that_page_multiset_misses() -> None:
    """P3 的页级多重集对"同行跨列互换"无能为力；按格 bbox 校验能抓到。"""
    page = PageWords(
        page=1,
        width=100.0,
        height=100.0,
        words=(Word("100", 40.0, 50.0, 10.0, 20.0), Word("200", 70.0, 80.0, 10.0, 20.0)),
    )
    swapped = TableGrid(
        cells=(
            GridCell(0, 0, 1, 1, "200", page=1, bbox=(0.35, 0.05, 0.55, 0.25)),
            GridCell(0, 1, 1, 1, "100", page=1, bbox=(0.65, 0.05, 0.85, 0.25)),
        ),
        source_engine="x",
    )
    s = summarize_grid(verify_grid(swapped, {1: page}))
    assert (s.numeric, s.mismatch, s.verdict) == (2, 2, "text_layer_cells_mismatch")


def test_table_verdicts() -> None:
    box = (0.1, 0.1, 0.5, 0.2)
    ok = verify_grid(
        TableGrid(cells=(GridCell(0, 0, 1, 1, "1,234.50", page=1, bbox=box),), source_engine="x"),
        PAGES,
    )
    assert summarize_grid(ok).verdict == "text_layer_cells_ok"
    scan = verify_grid(
        TableGrid(cells=(GridCell(0, 0, 1, 1, "7", page=2, bbox=box),), source_engine="x"), PAGES
    )
    assert summarize_grid(scan).verdict == "scan_single_unverified"
    assert summarize_grid(scan, tier="L2").verdict == "dual_agree"
    l1 = TableGrid(cells=(GridCell(0, 0, 1, 1, "7", page=1),), source_engine="paddle:3")
    assert summarize_grid(verify_grid(l1, PAGES), tier="L1").verdict == "text_layer_page_closed"
    assert summarize_grid(verify_grid(l1, PAGES)).verdict == "not_checked"


def test_rotated_page_is_not_checked_never_verified() -> None:
    rot = PageWords(page=3, width=100.0, height=100.0, words=PAGE.words, rotation=90)
    cell = GridCell(0, 0, 1, 1, "1,234.50", page=3, bbox=(0.1, 0.1, 0.5, 0.2))
    assert check_cell(cell, {3: rot}) == "not_checked"


def test_missing_page_is_no_text_layer() -> None:
    cell = GridCell(0, 0, 1, 1, "7", page=9, bbox=(0.1, 0.1, 0.5, 0.2))
    assert check_cell(cell, PAGES) == "no_text_layer"


def test_adjacent_page_is_not_widened() -> None:
    """数字只在相邻页：本页框内没有 -> mismatch（不做 ±1 页放宽）。"""
    p2 = PageWords(page=2, width=100.0, height=100.0, words=(Word("999", 12.0, 30.0, 12.0, 18.0),))
    cell = GridCell(0, 0, 1, 1, "999", page=1, bbox=(0.1, 0.1, 0.5, 0.2))
    assert check_cell(cell, {1: PAGE, 2: p2}) == "mismatch"


def test_multiset_not_set_and_spaced_decimal_is_mismatch() -> None:
    cell = GridCell(0, 0, 1, 1, "1,234.50 1,234.50", page=1, bbox=(0.1, 0.1, 0.5, 0.2))
    assert check_cell(cell, PAGES) == "mismatch"  # 框内只有一个
    spaced = GridCell(0, 0, 1, 1, "1,234 . 50", page=1, bbox=(0.1, 0.1, 0.5, 0.2))
    assert check_cell(spaced, PAGES) == "mismatch"  # 空白缺陷：虚警，不是假通过


def test_summary_counts_and_unverified_grid() -> None:
    box = (0.1, 0.1, 0.5, 0.2)
    g = TableGrid(
        cells=(
            GridCell(0, 0, 1, 1, "项目", page=1, bbox=box),
            GridCell(0, 1, 1, 1, "1,234.50", page=1, bbox=box),
            GridCell(0, 2, 1, 1, "7", page=2, bbox=box),
            GridCell(0, 3, 1, 1, "8", page=1),
        ),
        source_engine="x",
    )
    s = summarize_grid(verify_grid(g, PAGES))
    assert (s.numeric, s.match, s.mismatch, s.unverifiable) == (3, 1, 0, 2)
    assert s.verdict == "scan_single_unverified"
    # 从未校验过的网格：数字格按文本计数，全部 not_checked
    s0 = summarize_grid(g)
    assert (s0.numeric, s0.match, s0.unverifiable, s0.verdict) == (3, 0, 3, "not_checked")


def test_mismatch_beats_scan_and_l1() -> None:
    box = (0.1, 0.1, 0.5, 0.2)
    g = TableGrid(
        cells=(
            GridCell(0, 0, 1, 1, "1,284.50", page=1, bbox=box),
            GridCell(0, 1, 1, 1, "7", page=2, bbox=box),
        ),
        source_engine="x",
    )
    assert summarize_grid(verify_grid(g, PAGES), tier="L1").verdict == "text_layer_cells_mismatch"


def test_page_words_from_layouts() -> None:
    lay = PageLayout(page=4, width=50.0, height=60.0, words=[PAGE.words[0]], text="x", rotation=90)
    pw = page_words_from_layouts([lay])[4]
    assert (pw.page, pw.width, pw.height, pw.rotation, pw.words) == (
        4,
        50.0,
        60.0,
        90,
        (PAGE.words[0],),
    )
