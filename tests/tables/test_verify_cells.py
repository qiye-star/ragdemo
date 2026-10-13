from __future__ import annotations

from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.tables import verify_cells as vc
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
    assert summarize_grid(scan, tier="L2").verdict == "scan_single_unverified"  # I2
    nocoord = TableGrid(cells=(GridCell(0, 0, 1, 1, "7", page=2),), source_engine="html")
    assert summarize_grid(verify_grid(nocoord, PAGES), tier="L2").verdict == "dual_agree"
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


def _pw(
    *words: tuple[str, float, float, float, float], rot: int = 0, page: int = 1
) -> dict[int, PageWords]:
    return {page: PageWords(page, 100.0, 100.0, tuple(Word(*w) for w in words), rot)}


def _c(
    text: str, bbox: tuple[float, float, float, float] = (0.1, 0.1, 0.3, 0.2), **kw: object
) -> GridCell:
    return GridCell(0, 0, 1, 1, text, page=1, bbox=bbox, **kw)  # type: ignore[arg-type]


def test_version_and_verdict_set() -> None:
    assert vc.VERIFY_CELLS_VERSION == "cellverify:v2"
    assert "text_layer_cells_partial" in vc.TableVerdict.__args__  # type: ignore[attr-defined]


def test_i1_partial_when_some_numeric_cells_unverified() -> None:
    pages = _pw(("100", 12, 28, 12, 18))
    g = TableGrid(
        cells=(
            _c("100"),
            GridCell(0, 1, 1, 1, "999", page=1, bbox=None),
            GridCell(0, 2, 1, 1, "888", page=None, bbox=(0.5, 0.1, 0.7, 0.2)),
        ),
        source_engine="x",
    )
    assert summarize_grid(verify_grid(g, pages)).verdict == "text_layer_cells_partial"
    rot = {**pages, 2: PageWords(2, 100.0, 100.0, (Word("5", 12, 28, 12, 18),), 90)}
    g2 = TableGrid(
        cells=(_c("100"), GridCell(1, 0, 1, 1, "5", page=2, bbox=(0.1, 0.1, 0.3, 0.2))),
        source_engine="x",
    )
    assert summarize_grid(verify_grid(g2, rot)).verdict == "text_layer_cells_partial"


def test_i2_dual_agree_only_without_text_layer_evidence() -> None:
    mm = verify_grid(TableGrid(cells=(_c("1"),), source_engine="x"), _pw(("2", 12, 28, 12, 18)))
    assert summarize_grid(mm, tier="L2").verdict == "text_layer_cells_mismatch"  # h2
    scan = verify_grid(
        TableGrid(
            cells=(_c("7", page=3),)
            if False
            else (GridCell(0, 0, 1, 1, "7", page=3, bbox=(0.1, 0.1, 0.3, 0.2)),),
            source_engine="x",
        ),
        _pw(("x", 1, 2, 1, 2)),
    )
    assert summarize_grid(scan, tier="L2").verdict == "scan_single_unverified"  # h1
    never = TableGrid(cells=(_c("1"),), source_engine="x")
    assert summarize_grid(never, tier="L2").verdict == "dual_agree"  # h3: no coordinates evidence
    ok = verify_grid(TableGrid(cells=(_c("2"),), source_engine="x"), _pw(("2", 12, 28, 12, 18)))
    assert summarize_grid(ok, tier="L2").verdict == "text_layer_cells_ok"  # evidence wins, never L2


def test_zero_numeric_cells_is_not_checked_not_ok() -> None:
    g = TableGrid(
        cells=(GridCell(0, 0, 1, 1, "项目", page=1, bbox=(0.1, 0.1, 0.3, 0.2)),), source_engine="x"
    )
    assert summarize_grid(verify_grid(g, PAGES)).verdict == "not_checked"
    assert summarize_grid(g, tier="L2").verdict == "dual_agree"


def test_stale_no_numbers_check_is_recounted() -> None:
    g = TableGrid(
        cells=(GridCell(0, 0, 1, 1, "999", page=1, check="no_numbers"),), source_engine="x"
    )
    s = summarize_grid(g)
    assert (s.numeric, s.unverifiable, s.verdict) == (1, 1, "not_checked")


def test_i3_match_requires_equal_multiset() -> None:
    assert (
        check_cell(_c("5"), _pw(("5", 12, 14, 12, 18), ("12.5", 15, 28, 12, 18))) == "mismatch"
    )  # e3
    assert (
        check_cell(_c("2024"), _pw(("2024年", 12, 20, 12, 18), ("12.5", 21, 28, 12, 18)))
        == "mismatch"
    )  # e4
    assert (
        check_cell(_c("12.5"), _pw(("12.5", 12, 20, 12, 18), ("1", 21, 22, 12, 18))) == "mismatch"
    )  # e2
    assert check_cell(_c("12.5"), _pw(("12.5", 12, 28, 12, 18))) == "match"


def test_i3_oversized_bbox_and_whole_page_bbox() -> None:
    pages = _pw(("100", 10, 20, 10, 15), ("200", 40, 50, 10, 15), ("300", 70, 80, 10, 15))
    assert check_cell(_c("300", bbox=(0, 0, 1, 1)), pages) == "mismatch"
    assert (
        check_cell(
            _c("12.5", bbox=(0.1, 0.1, 0.3, 0.2)),
            _pw(("99.0", 12, 20, 12, 18), ("12.5", 22, 28, 12, 18)),
        )
        == "mismatch"
    )


def test_pad_boundary() -> None:
    inside = _pw(("7", 12, 14, 12, 18))
    assert check_cell(_c("7", bbox=(0.1, 0.1, 0.3, 0.2)), inside) == "match"
    # 词中心恰在 bbox 外 pad 之内 / 之外
    c = (0.1, 0.1, 0.3, 0.2)
    w_in = PageWords(
        1,
        100.0,
        100.0,
        (Word("7", 30.0 + vc.BBOX_PAD_PT - 0.4, 30.0 + vc.BBOX_PAD_PT + 0.4, 12, 18),),
    )
    w_out = PageWords(
        1,
        100.0,
        100.0,
        (Word("7", 30.0 + vc.BBOX_PAD_PT + 0.2, 30.0 + vc.BBOX_PAD_PT + 1.0, 12, 18),),
    )
    assert len(words_in_bbox(w_in, c)) == 1
    assert words_in_bbox(w_out, c) == []


def test_m1_detached_sign_percent_paren_is_mismatch() -> None:
    assert (
        check_cell(_c("12.5"), _pw(("-", 12, 14, 12, 18), ("12.5", 15, 28, 12, 18))) == "mismatch"
    )  # c5
    assert (
        check_cell(_c("-12.5"), _pw(("-", 12, 14, 12, 18), ("12.5", 15, 28, 12, 18))) == "mismatch"
    )  # c5b: split sign never joined (false alarm)
    assert (
        check_cell(_c("12.5"), _pw(("12.5", 12, 22, 12, 18), ("%", 22.5, 25, 12, 18))) == "mismatch"
    )  # f3
    assert (
        check_cell(_c("12.5%"), _pw(("12.5", 12, 22, 12, 18), ("%", 22.5, 25, 12, 18)))
        == "mismatch"
    )  # f3b: split % never joined (false alarm)
    assert (
        check_cell(
            _c("12.5"), _pw(("(", 11, 12, 12, 18), ("12.5", 12, 22, 12, 18), (")", 22, 23, 12, 18))
        )
        == "mismatch"
    )


def test_non_square_page_and_swapped_dims() -> None:
    words = (Word("150", 150.0, 170.0, 20.0, 30.0),)
    good = PageWords(1, 200.0, 100.0, words)
    swapped = PageWords(1, 100.0, 200.0, words)
    cell = GridCell(0, 0, 1, 1, "150", page=1, bbox=(0.7, 0.15, 0.9, 0.35))
    assert check_cell(cell, {1: good}) == "match"
    assert check_cell(cell, {1: swapped}) == "mismatch"


def test_words_in_bbox_sort_order() -> None:
    pw = PageWords(
        1,
        100.0,
        100.0,
        (Word("c", 50, 55, 20, 25), Word("a", 10, 15, 10, 15), Word("b", 30, 35, 10, 15)),
    )
    assert [w.text for w in words_in_bbox(pw, (0, 0, 1, 1))] == ["a", "b", "c"]


def test_b3_neighbour_word_just_outside_bbox_is_not_pulled_in() -> None:
    pages = _pw(("7", 30.5, 31.5, 12, 18))  # 词中心 31.0，bbox 右缘 30.0
    assert check_cell(_c("7"), pages) == "mismatch"
