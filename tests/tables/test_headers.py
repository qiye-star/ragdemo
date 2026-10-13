from __future__ import annotations

from ragdemo.tables.grid import GridCell, TableGrid
from ragdemo.tables.headers import (
    col_header_path,
    infer_headers,
    is_value_text,
    row_label,
    row_section,
)


def _g(*cells: GridCell) -> TableGrid:
    return TableGrid(cells=cells, source_engine="x")


def _t(*rows: tuple[int, int, int, int, str]) -> TableGrid:
    return infer_headers(_g(*(GridCell(*r) for r in rows)))


def test_value_text() -> None:
    for s in ("1,234.50", "(3,000)", "-12.5%", "（2.1）", "0", "−5", "１２３"):
        assert is_value_text(s), s
    # not values: header words, enumerators, spaced/odd digit shapes, non-ASCII digits
    for s in (
        "2025年1-9月", "项目", "", "-", "单位：元", "(1)", "5 . 83", "1,23", "- 5", "٣٤",
        "649,900,00<br>0",
    ):  # fmt: skip
        assert not is_value_text(s), s


def test_rowspan_extends_header_rows_and_paths() -> None:
    g = _t(
        (0, 0, 2, 1, "项目"),
        (0, 1, 1, 2, "2025年1-9月"),
        (0, 3, 2, 1, "备注"),
        (1, 1, 1, 1, "金额"),
        (1, 2, 1, 1, "占比"),
        (2, 0, 1, 1, "营业收入"),
        (2, 1, 1, 1, "1,234.50"),
        (2, 2, 1, 1, "50%"),
        (2, 3, 1, 1, ""),
    )
    assert (g.header_rows, g.header_cols) == (2, 1)
    assert col_header_path(g, 1) == ("2025年1-9月", "金额")
    assert col_header_path(g, 0) == ("项目",)
    assert row_label(g, 2) == "营业收入"
    assert {(c.row, c.col) for c in g.cells if c.is_header} == {
        (0, 0), (0, 1), (0, 3), (1, 1), (1, 2), (2, 0),
    }  # fmt: skip


def test_single_cell_table_has_no_headers() -> None:
    g = _t((0, 0, 1, 1, "本公司董事会保证……"))
    assert (g.header_rows, g.header_cols) == (0, 0)


def test_row0_is_data_means_no_header() -> None:
    g = _t(
        (0, 0, 1, 1, "营业收入"), (0, 1, 1, 1, "100"), (1, 0, 1, 1, "营业成本"), (1, 1, 1, 1, "60")
    )
    assert g.header_rows == 0
    assert col_header_path(g, 1) == ()
    assert row_label(g, 0) == "营业收入"
    assert not any(c.is_header and c.text in ("100", "60") for c in g.cells)


def test_two_row_header_without_spans() -> None:
    g = _t(
        (0, 0, 1, 1, "项目"), (0, 1, 1, 1, "2025年"), (0, 2, 1, 1, "2024年"),
        (1, 0, 1, 1, ""), (1, 1, 1, 1, "金额"), (1, 2, 1, 1, "金额"),
        (2, 0, 1, 1, "收入"), (2, 1, 1, 1, "10"), (2, 2, 1, 1, "9"),
    )  # fmt: skip
    assert g.header_rows == 2
    assert col_header_path(g, 2) == ("2024年", "金额")


def test_year_only_header_row_is_still_a_header() -> None:
    g = _t(
        (0, 0, 1, 1, "项目"), (0, 1, 1, 1, "2025"), (0, 2, 1, 1, "2024"),
        (1, 0, 1, 1, "收入"), (1, 1, 1, 1, "10"), (1, 2, 1, 1, "9"),
    )  # fmt: skip
    assert g.header_rows == 1
    assert col_header_path(g, 1) == ("2025",)
    assert [c.is_header for c in g.cells if c.text == "2025"] == [True]


def test_column0_is_data_means_no_header_cols() -> None:
    g = _t((0, 0, 1, 1, "2025年"), (0, 1, 1, 1, "2024年"), (1, 0, 1, 1, "10"), (1, 1, 1, 1, "9"))
    assert (g.header_rows, g.header_cols) == (1, 0)
    assert row_label(g, 1) == ""
    assert not any(c.is_header for c in g.cells if c.row == 1)


def test_mid_body_subtotal_colspan_does_not_move_header_cols() -> None:
    g = _t(
        (0, 0, 1, 1, "项目"), (0, 1, 1, 1, "本期"), (0, 2, 1, 1, "上期"),
        (1, 0, 1, 1, "收入"), (1, 1, 1, 1, "10"), (1, 2, 1, 1, "9"),
        (2, 0, 1, 2, "小计"), (2, 2, 1, 1, "19"),
        (3, 0, 1, 1, "成本"), (3, 1, 1, 1, "5"), (3, 2, 1, 1, "4"),
    )  # fmt: skip
    assert (g.header_rows, g.header_cols) == (1, 1)
    assert row_label(g, 3) == "成本"
    assert not any(c.is_header and c.text in ("10", "5", "19") for c in g.cells)


def test_index_column_is_never_a_row_label() -> None:
    g = _t(
        (0, 0, 1, 1, "序号"), (0, 1, 1, 1, "项目"), (0, 2, 1, 1, "2025年1-9月"),
        (1, 0, 1, 1, "1"), (1, 1, 1, 1, "营业收入"), (1, 2, 1, 1, "100"),
        (2, 0, 1, 1, "2"), (2, 1, 1, 1, "营业成本"), (2, 2, 1, 1, "60"),
    )  # fmt: skip
    assert g.header_cols == 2
    assert row_label(g, 1) == "营业收入"  # documented fallback: value-like cells skipped
    assert not any(c.is_header and c.text in ("1", "2") for c in g.cells)


def test_section_row_is_a_label_not_a_column_header() -> None:
    g = _t(
        (0, 0, 1, 1, "项目"), (0, 1, 1, 1, "2025年9月30日"), (0, 2, 1, 1, "2024年12月31日"),
        (1, 0, 1, 3, "流动资产："),
        (2, 0, 1, 1, "货币资金"), (2, 1, 1, 1, "10"), (2, 2, 1, 1, "9"),
        (3, 0, 1, 3, "非流动资产："),
        (4, 0, 1, 1, "固定资产"), (4, 1, 1, 1, "5"), (4, 2, 1, 1, "4"),
    )  # fmt: skip
    assert (g.header_rows, g.header_cols) == (1, 1)
    assert col_header_path(g, 1) == ("2025年9月30日",)
    assert row_section(g, 2) == "流动资产："
    assert row_section(g, 4) == "非流动资产："
    assert row_section(g, 0) is None


def test_title_row_zero_is_not_in_column_paths() -> None:
    g = _t(
        (0, 0, 1, 3, "合并资产负债表"),
        (1, 0, 1, 1, "项目"), (1, 1, 1, 1, "本期"), (1, 2, 1, 1, "上期"),
        (2, 0, 1, 1, "货币资金"), (2, 1, 1, 1, "10"), (2, 2, 1, 1, "9"),
    )  # fmt: skip
    assert g.header_rows == 2
    assert col_header_path(g, 1) == ("本期",)


def test_repeated_subheader_makes_paths_below_unknown() -> None:
    g = _t(
        (0, 0, 1, 1, "项目"), (0, 1, 1, 1, "营业收入"), (0, 2, 1, 1, "毛利率"),
        (1, 0, 1, 1, "分行业"), (1, 1, 1, 1, "10"), (1, 2, 1, 1, "5%"),
        (2, 0, 1, 1, "分产品"), (2, 1, 1, 1, "营业成本"), (2, 2, 1, 1, "收入占比"),
        (3, 0, 1, 1, "甲产品"), (3, 1, 1, 1, "6"), (3, 2, 1, 1, "7%"),
    )  # fmt: skip
    assert col_header_path(g, 1) == ("营业收入",)
    assert col_header_path(g, 1, row=1) == ("营业收入",)
    assert col_header_path(g, 1, row=3) is None
    assert col_header_path(g, 1, row=2) is None


def test_header_texts_are_br_normalised() -> None:
    g = _t(
        (0, 0, 1, 1, "项目"), (0, 1, 1, 1, "毛利率<br>(%)"),
        (1, 0, 1, 1, "收入"), (1, 1, 1, 1, "10"),
    )  # fmt: skip
    assert col_header_path(g, 1) == ("毛利率 (%)",)
