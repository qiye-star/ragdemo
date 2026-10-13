from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from ragdemo.tables.fact_check import (
    EXPECTED_BASIS,
    METRIC_KIND,
    FactCheckConfig,
    FactRow,
    TableView,
    cell_value_in_unit,
    match_fact,
    tolerance_for,
)
from ragdemo.tables.grid import BBox, CellCheck, GridCell, TableGrid
from ragdemo.tables.headers import infer_headers

T0 = datetime(2025, 10, 28, 10, tzinfo=UTC)
BOX: BBox = (0.1, 0.1, 0.5, 0.2)


def _table(
    header: tuple[str, ...],
    rows: list[tuple[str, ...]],
    *,
    unit: str | None = "元",
    doc_period: str | None = "2025Q3",
    check: CellCheck = "match",
    unit_source: str = "in_table",
    kind: str = "income",
    currency: str | None = "CNY",
    caption: str | None = None,
    table_id: int = 1,
    calendar_fy: bool | None = True,
    bbox: bool = True,
    column_units: tuple[tuple[int, str], ...] = (),
) -> TableView:
    cells = [GridCell(0, c, 1, 1, t, page=3) for c, t in enumerate(header)]
    for r, row in enumerate(rows, start=1):
        cells += [
            GridCell(
                r,
                c,
                1,
                1,
                t,
                page=3,
                bbox=BOX if bbox else None,
                check=check if c else "no_numbers",
            )
            for c, t in enumerate(row)
        ]
    g = infer_headers(TableGrid(cells=tuple(cells), source_engine="textin:t"))
    return TableView(
        table_id,
        100 + table_id,
        doc_period,
        g,
        unit,
        unit_source,
        column_units,
        (),
        currency,
        kind,
        T0,
        caption=caption,
        calendar_fy=calendar_fy,
    )


def _fact(metric: str, period: str, value: str) -> FactRow:
    return FactRow(9, "CN.688041", metric, period, Decimal(value), "CNY", T0, date(2025, 9, 30))


TABLE = _table(
    ("项目", "2025年1-9月", "2024年1-9月"),
    [("营业收入", "1,234,567.89", "1,000,000.00"), ("研发费用", "-", "-")],
)


def test_value_conversion_is_python_arithmetic() -> None:
    assert cell_value_in_unit("1,234.56", "万元") == (Decimal("12345600.00"), Decimal(10000))
    assert cell_value_in_unit("(3,000)", "元") == (Decimal("-3000"), Decimal(1))
    assert cell_value_in_unit("12.5%", "元") is None
    assert cell_value_in_unit("-", "元") is None
    assert cell_value_in_unit("1,234", None) is None


def test_inline_unit_overrides_and_foreign_or_unknown_units_refuse() -> None:
    assert cell_value_in_unit("5 万元", "元") == (Decimal(50000), Decimal(10000))
    assert cell_value_in_unit("5 万美元", "元") is None
    assert cell_value_in_unit("5 股", "元") is None
    assert cell_value_in_unit("5", "股") is None
    assert cell_value_in_unit("5", "万亿元") is None


def test_tolerance() -> None:
    assert tolerance_for("1,234.5", "万元") == Decimal(6000)
    assert tolerance_for("12.34", "亿元") == Decimal("500000.00")


def test_kinds_and_expected_basis() -> None:
    assert METRIC_KIND["revenue_total"] == "flow"
    assert METRIC_KIND["inventory"] == "point"
    assert dict(EXPECTED_BASIS) == {"flow": "ytd", "point": "point"}


def test_match_uses_the_right_period_column() -> None:
    m = match_fact(_fact("revenue_total", "2025Q3", "1234567.89"), [TABLE])
    assert (m.verdict, m.row, m.col, m.period_label) == ("match", 1, 1, "2025年1-9月")
    assert (m.page, m.block_id, m.table_id) == (3, 101, 1)
    prev = match_fact(_fact("revenue_total", "2024Q3", "1000000"), [TABLE])
    assert (prev.verdict, prev.col) == ("match", 2)


def test_nearest_number_in_wrong_column_is_not_accepted() -> None:
    """旧 crosscheck 取"同行最接近的数"；这里 2025Q3 的事实即使等于上年列也必须判 mismatch。"""
    m = match_fact(_fact("revenue_total", "2025Q3", "1000000"), [TABLE])
    assert (m.verdict, m.col) == ("mismatch", 1)


def test_q3_single_quarter_column_is_not_used_for_ytd_flow() -> None:
    t = _table(("项目", "本报告期", "上年同期"), [("营业收入", "500.00", "400.00")])
    assert match_fact(_fact("revenue_total", "2025Q3", "500"), [t]).verdict == "no_period_column"


def test_other_verdicts() -> None:
    assert match_fact(_fact("inventory", "2025Q3", "1000000"), [TABLE]).verdict == "no_row"
    no_unit = _table(("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], unit=None)
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), [no_unit]).verdict == "no_unit"
    bad_cell = _table(("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], check="mismatch")
    assert (
        match_fact(_fact("revenue_total", "2025Q3", "1000000"), [bad_cell]).verdict
        == "no_period_column"
    )
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), []).verdict == "no_row"


def test_preceding_unit_can_be_disabled() -> None:
    t = _table(
        ("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], unit_source="preceding_block"
    )
    strict = FactCheckConfig(allow_preceding_unit=False)
    assert (
        match_fact(_fact("revenue_total", "2025Q3", "1000000"), [t], config=strict).verdict
        == "no_unit"
    )
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), [t]).verdict == "match"


# ---- 评审后约束 --------------------------------------------------------------------------


def test_only_verified_located_cells_support() -> None:
    for check in ("not_checked", "no_text_layer", "no_numbers", "mismatch"):
        t = _table(("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], check=check)
        assert (
            match_fact(_fact("revenue_total", "2025Q3", "1000000"), [t]).verdict
            == "no_period_column"
        )
    no_box = _table(("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], bbox=False)
    assert (
        match_fact(_fact("revenue_total", "2025Q3", "1000000"), [no_box]).verdict
        == "no_period_column"
    )


def test_duplicate_period_columns_are_ambiguous_never_matched() -> None:
    t = _table(
        ("项目", "2025年1-9月", "2025年1-9月"), [("营业收入", "1,000,000.00", "1,000,000.00")]
    )
    m = match_fact(_fact("revenue_total", "2025Q3", "1000000"), [t])
    assert m.verdict == "ambiguous"
    assert m.reason == "duplicate_period_column"


def _two_level(
    top: list[tuple[str, int]], sub: list[str], row: tuple[str, ...], *, calendar_fy: bool = True
) -> TableView:
    cells = [GridCell(0, 0, 1, 1, "项目")]
    col = 1
    for text, span in top:
        cells.append(GridCell(0, col, 1, span, text))
        col += span
    cells.append(GridCell(1, 0, 1, 1, ""))
    cells += [GridCell(1, c, 1, 1, t) for c, t in enumerate(sub, start=1)]
    cells.append(GridCell(2, 0, 1, 1, row[0]))
    cells += [
        GridCell(2, c, 1, 1, t, page=3, bbox=BOX, check="match") for c, t in enumerate(row[1:], 1)
    ]
    g = infer_headers(TableGrid(cells=tuple(cells), source_engine="textin:t"))
    assert g.header_rows == 2
    return TableView(1, 101, "2025Q3", g, "元", "in_table", (), (), "CNY", "income", T0,
                     calendar_fy=calendar_fy)  # fmt: skip


def test_adjusted_before_after_columns_are_ambiguous() -> None:
    t = _two_level(
        [("2024年1-9月", 2)], ["调整前", "调整后"], ("营业收入", "1,000,000.00", "2,000,000.00")
    )
    m = match_fact(_fact("revenue_total", "2024Q3", "1000000"), [t])
    assert (m.verdict, m.reason) == ("ambiguous", "duplicate_period_column")
    only_adj = _two_level([("2024年1-9月", 1)], ["调整后"], ("营业收入", "1,000,000.00"))
    m = match_fact(_fact("revenue_total", "2024Q3", "1000000"), [only_adj])
    assert (m.verdict, m.reason) == ("ambiguous", "qualified_period_column")


def test_cash_flow_single_quarter_label_is_not_trusted() -> None:
    cf = _table(("项目", "本期金额"), [("经营活动产生的现金流量净额", "100.00")], kind="cash_flow")
    m = match_fact(_fact("operating_cash_flow", "2025Q3", "100"), [cf])
    assert m.verdict == "no_period_column"
    kf = _table(("项目", "本报告期"), [("营业收入", "100.00")], kind="key_figures")
    # 主要财务数据表里的"本报告期"是单季度：仍然不是年初至报告期末
    assert match_fact(_fact("revenue_total", "2025Q3", "100"), [kf]).verdict == "no_period_column"


def test_parent_company_table_never_supports_consolidated_fact() -> None:
    parent = _table(
        ("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], caption="母公司利润表", table_id=2
    )
    m = match_fact(_fact("revenue_total", "2025Q3", "1000000"), [parent])
    assert m.verdict == "no_row"
    consolidated = _table(
        ("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], caption="合并利润表", table_id=3
    )
    both = match_fact(_fact("revenue_total", "2025Q3", "1000000"), [parent, consolidated])
    assert (both.verdict, both.table_id) == ("match", 3)


def test_parent_company_column_is_excluded_from_duplicate_detection() -> None:
    cells = [
        GridCell(0, 0, 1, 1, "项目"),
        GridCell(0, 1, 1, 1, "合并"),
        GridCell(0, 2, 1, 1, "母公司"),
    ]
    cells += [GridCell(1, 0, 1, 1, ""), GridCell(1, 1, 1, 1, "2025年1-9月")]
    cells += [GridCell(1, 2, 1, 1, "2025年1-9月"), GridCell(2, 0, 1, 1, "营业收入")]
    cells += [GridCell(2, 1, 1, 1, "1,000,000.00", page=3, bbox=BOX, check="match")]
    cells += [GridCell(2, 2, 1, 1, "9,000,000.00", page=3, bbox=BOX, check="match")]
    g = infer_headers(TableGrid(cells=tuple(cells), source_engine="textin:t"))
    assert g.header_rows == 2
    t = TableView(
        1, 101, "2025Q3", g, "元", "in_table", (), (), "CNY", "income", T0, calendar_fy=True
    )
    m = match_fact(_fact("revenue_total", "2025Q3", "1000000"), [t])
    assert (m.verdict, m.col) == ("match", 1)


def test_non_cny_table_and_non_cny_fact_refuse() -> None:
    t = _table(("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], currency="USD")
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), [t]).verdict == "not_cny"
    usd = FactRow(9, "CN.1", "revenue_total", "2025Q3", Decimal(1), "USD", T0, date(2025, 9, 30))
    assert match_fact(usd, [TABLE]).verdict == "not_cny"
    undeclared = _table(("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], currency=None)
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), [undeclared]).verdict == "match"


def test_unit_outside_scale_refuses_and_inline_unit_wins() -> None:
    t = _table(("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], unit="股")
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), [t]).verdict == "no_unit"
    inline = _table(("项目", "2025年1-9月"), [("营业收入", "100.00 万元")], unit="元")
    m = match_fact(_fact("revenue_total", "2025Q3", "1000000"), [inline])
    assert (m.verdict, m.unit_scale) == ("match", Decimal(10000))
    col_unit = _table(
        ("项目", "2025年1-9月"), [("营业收入", "100.00")], unit="元", column_units=((1, "万元"),)
    )
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), [col_unit]).verdict == "match"


def test_q1_requires_calendar_fiscal_year_flag() -> None:
    t = _table(
        ("项目", "2025年1-3月"),
        [("营业收入", "1,000,000.00")],
        doc_period="2025Q1",
        calendar_fy=None,
    )
    m = match_fact(_fact("revenue_total", "2025Q1", "1000000"), [t])
    assert (m.verdict, m.reason) == ("ambiguous", "fiscal_year_flag_missing")
    t2 = _table(
        ("项目", "2025年1-3月"),
        [("营业收入", "1,000,000.00")],
        doc_period="2025Q1",
        calendar_fy=True,
    )
    assert match_fact(_fact("revenue_total", "2025Q1", "1000000"), [t2]).verdict == "match"


def test_point_metric_uses_point_column_and_q2_maps_to_h1() -> None:
    t = _table(
        ("项目", "2025年6月30日", "2024年12月31日"),
        [("存货", "5,000,000.00", "7,000,000.00")],
        kind="balance_sheet",
        doc_period="2025H1",
    )
    assert match_fact(_fact("inventory", "2025Q2", "5000000"), [t]).verdict == "match"
    assert match_fact(_fact("inventory", "2024FY", "7000000"), [t]).verdict == "match"


def test_comparative_column_match_counts_and_own_document_is_preferred() -> None:
    own = _table(("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], table_id=5)
    other = _table(("项目", "2024年1-9月"), [("营业收入", "1,000,000.00")], table_id=6)
    m = match_fact(_fact("revenue_total", "2024Q3", "1000000"), [own, other])
    assert (m.verdict, m.table_id) == ("match", 6)


def test_mismatch_only_in_comparative_column_is_ambiguous_not_mismatch() -> None:
    other = _table(("项目", "2024年1-9月"), [("营业收入", "100,000.00")], table_id=6)
    m = match_fact(_fact("revenue_total", "2024Q3", "1000000"), [other])
    assert (m.verdict, m.reason) == ("ambiguous", "comparative_mismatch_possible_restatement")
    own = _table(
        ("项目", "2024年1-9月"), [("营业收入", "100,000.00")], doc_period="2024Q3", table_id=7
    )
    assert (
        match_fact(_fact("revenue_total", "2024Q3", "1000000"), [other, own]).verdict == "mismatch"
    )


def test_prefix_decorated_labels_match_via_statement_key() -> None:
    t = _table(("项目", "2025年1-9月"), [("一、营业收入", "1,000,000.00")])
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), [t]).verdict == "match"


# ---- 评审修复波（C1/C2/I1/I2/I4）---------------------------------------------------------


def _row_groups(
    parent: str, rows: list[tuple[str, str]], *, header: str = "2025年1-9月"
) -> TableView:
    """首两列是行标签（父标签竖向合并），第三列是数值。"""
    cells = [GridCell(0, 0, 1, 2, "项目", page=3), GridCell(0, 2, 1, 1, header, page=3)]
    cells.append(GridCell(1, 0, len(rows), 1, parent, page=3, bbox=BOX, check="no_numbers"))
    for r, (label, value) in enumerate(rows, 1):
        cells.append(GridCell(r, 1, 1, 1, label, page=3, bbox=BOX, check="no_numbers"))
        cells.append(GridCell(r, 2, 1, 1, value, page=3, bbox=BOX, check="match"))
    g = infer_headers(TableGrid(cells=tuple(cells), source_engine="textin:t"))
    assert g.header_cols == 2
    return TableView(1, 101, "2025Q3", g, "元", "in_table", (), (), "CNY", "income", T0,
                     calendar_fy=True)  # fmt: skip


def test_c1_child_row_under_matching_parent_is_not_the_metric_row() -> None:
    t = _row_groups("营业总收入", [("营业收入", "900,000.00"), ("利息收入", "100,000.00")])
    assert match_fact(_fact("revenue_total", "2025Q3", "900000"), [t]).verdict == "match"
    m = match_fact(_fact("revenue_total", "2025Q3", "100000"), [t])
    assert m.verdict != "match"
    t2 = _row_groups("营业收入", [("利息收入", "100,000.00")])
    assert match_fact(_fact("revenue_total", "2025Q3", "100000"), [t2]).verdict == "no_row"


def test_c2_qualified_rows_are_ambiguous() -> None:
    t = _row_groups(
        "归属于上市公司股东的净利润", [("调整前", "500,000.00"), ("调整后", "520,000.00")]
    )
    m = match_fact(_fact("net_profit_attributable", "2025Q3", "500000"), [t])
    assert (m.verdict, m.reason) == ("ambiguous", "qualified_row_label")
    t2 = _table(
        ("项目", "2025年1-9月"),
        [("归属于上市公司股东的净利润（调整前）", "500,000.00")],
    )
    m = match_fact(_fact("net_profit_attributable", "2025Q3", "500000"), [t2])
    assert (m.verdict, m.reason) == ("ambiguous", "qualified_row_label")


def test_c2_duplicate_label_rows_are_ambiguous() -> None:
    t = _table(
        ("项目", "2025年1-9月"), [("营业收入", "1,000,000.00"), ("营业收入", "1,500,000.00")]
    )
    m = match_fact(_fact("revenue_total", "2025Q3", "1500000"), [t])
    assert (m.verdict, m.reason) == ("ambiguous", "multiple_label_rows")


def test_i1_own_report_mismatch_beats_a_comparative_match() -> None:
    own = _table(("项目", "2025年1-9月"), [("营业收入", "1,000,000.00")], doc_period="2025Q3")
    later = _table(
        ("项目", "2025年1-9月"), [("营业收入", "1,200,000.00")], doc_period="2026Q3", table_id=2
    )
    m = match_fact(_fact("revenue_total", "2025Q3", "1200000"), [own, later])
    assert (m.verdict, m.reason) == ("ambiguous", "own_report_mismatch_comparative_match")
    # 反过来：本期报告 match，比较列不一致 → 仍是 match
    m = match_fact(_fact("revenue_total", "2025Q3", "1000000"), [own, later])
    assert m.verdict == "match"


def test_i2_total_revenue_row_is_not_the_revenue_row() -> None:
    only_total = _table(("项目", "2025年1-9月"), [("一、营业总收入", "1,000,000.00")])
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), [only_total]).verdict == "no_row"
    both_differ = _table(
        ("项目", "2025年1-9月"),
        [("一、营业总收入", "1,200,000.00"), ("其中：营业收入", "1,000,000.00")],
    )
    m = match_fact(_fact("revenue_total", "2025Q3", "1000000"), [both_differ])
    assert (m.verdict, m.reason) == ("ambiguous", "sibling_label_differs")
    both_equal = _table(
        ("项目", "2025年1-9月"),
        [("一、营业总收入", "1,000,000.00"), ("其中：营业收入", "1,000,000.00")],
    )
    assert match_fact(_fact("revenue_total", "2025Q3", "1000000"), [both_equal]).verdict == "match"


def test_i4_insufficient_precision_or_tiny_values_never_match() -> None:
    t = _table(("项目", "2025年1-9月"), [("营业收入", "12")], unit="亿元")
    m = match_fact(_fact("revenue_total", "2025Q3", "1249000000"), [t])
    assert (m.verdict, m.reason) == ("ambiguous", "insufficient_precision")
    ok = _table(("项目", "2025年1-9月"), [("营业收入", "12.34")], unit="亿元")
    assert match_fact(_fact("revenue_total", "2025Q3", "1234000000"), [ok]).verdict == "match"
    tiny = _table(("项目", "2025年1-9月"), [("营业收入", "3,000.00")])
    m = match_fact(_fact("revenue_total", "2025Q3", "8999"), [tiny])
    assert (m.verdict, m.reason) == ("ambiguous", "value_below_resolution")
