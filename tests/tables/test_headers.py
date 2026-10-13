from __future__ import annotations

from ragdemo.tables.grid import GridCell, TableGrid
from ragdemo.tables.headers import col_header_path, infer_headers, is_value_text, row_label


def _g(*cells: GridCell) -> TableGrid:
    return TableGrid(cells=cells, source_engine="x")


def test_value_text() -> None:
    for s in ("1,234.50", "(3,000)", "-12.5%", "（2.1）", "0"):
        assert is_value_text(s)
    for s in ("2025年1-9月", "项目", "", "-", "单位：元"):
        assert not is_value_text(s)


def test_rowspan_extends_header_rows_and_paths() -> None:
    g = infer_headers(
        _g(
            GridCell(0, 0, 2, 1, "项目"),
            GridCell(0, 1, 1, 2, "2025年1-9月"),
            GridCell(0, 3, 2, 1, "备注"),
            GridCell(1, 1, 1, 1, "金额"),
            GridCell(1, 2, 1, 1, "占比"),
            GridCell(2, 0, 1, 1, "营业收入"),
            GridCell(2, 1, 1, 1, "1,234.50"),
            GridCell(2, 2, 1, 1, "50%"),
            GridCell(2, 3, 1, 1, ""),
        )
    )
    assert (g.header_rows, g.header_cols) == (2, 1)
    assert col_header_path(g, 1) == ("2025年1-9月", "金额")
    assert col_header_path(g, 0) == ("项目",)
    assert row_label(g, 2) == "营业收入"
    assert {(c.row, c.col) for c in g.cells if c.is_header} == {
        (0, 0),
        (0, 1),
        (0, 3),
        (1, 1),
        (1, 2),
        (2, 0),
    }


def test_single_cell_table_has_no_headers() -> None:
    g = infer_headers(_g(GridCell(0, 0, 1, 1, "本公司董事会保证……")))
    assert (g.header_rows, g.header_cols) == (0, 0)


def test_spanning_title_row_without_values_joins_header() -> None:
    g = infer_headers(
        _g(
            GridCell(0, 0, 1, 1, "项目"),
            GridCell(0, 1, 1, 1, "本期"),
            GridCell(1, 0, 1, 2, "一、经营活动产生的现金流量"),
            GridCell(2, 0, 1, 1, "销售商品收到的现金"),
            GridCell(2, 1, 1, 1, "100"),
        )
    )
    assert g.header_rows == 2
