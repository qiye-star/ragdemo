"""单元格引用解析、按 as_of 取格、数值佐证判定（T9）。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest

from ragdemo.tables.citation import (
    CellRef,
    CellResolution,
    cell_supports_value,
    parse_cell_citations,
    resolve_cell,
)
from ragdemo.tables.context import bind_context
from ragdemo.tables.grid import BBox, CellCheck, GridCell, TableGrid
from ragdemo.tables.statements import statement_kind
from ragdemo.tables.store import TableRecord, insert_table
from ragdemo.tables.verify_cells import VERIFY_CELLS_VERSION, summarize_grid

BOX: BBox = (0.1, 0.1, 0.5, 0.2)


def test_parse_only_cell_citations() -> None:
    text = "经营活动现金流量净额 3.2 亿元[1885 | 9 | r12c1]，另见[1885 | 9]。"
    assert parse_cell_citations(text) == [CellRef(1885, 9, 12, 1)]


def test_parse_dash_page_and_loose_spacing() -> None:
    assert parse_cell_citations("[7|—|r0c0] [8 |  3|r1c2]") == [
        CellRef(7, None, 0, 0),
        CellRef(8, 3, 1, 2),
    ]


def _res(
    text: str,
    unit: str | None,
    check: CellCheck = "match",
    *,
    page: int | None = 9,
    bbox: BBox | None = BOX,
    status: str = "ok",
    currency: str | None = None,
) -> CellResolution:
    cell = GridCell(12, 1, 1, 1, text, page=page, bbox=bbox, check=check)
    return CellResolution(
        CellRef(1885, 9, 12, 1),
        status,  # type: ignore[arg-type]
        cell,
        unit,
        "营业收入",
        ("本期",),
        "text_layer_cells_ok",
        text,
        currency=currency,
    )


def test_supports_value_with_unit_scale_and_half_percent_tolerance() -> None:
    assert cell_supports_value(_res("1,234.50", "万元"), Decimal("12345000"))
    assert cell_supports_value(_res("1,234.50", "万元"), Decimal("12400000"))  # 0.45% < 0.5%
    assert not cell_supports_value(_res("1,234.50", "万元"), Decimal("12500000"))
    assert cell_supports_value(_res("(3.1)", None), Decimal("-3.1"))
    assert cell_supports_value(_res("12.5%", None), Decimal("0.125"))


def test_only_match_supports() -> None:
    checks: tuple[CellCheck, ...] = ("mismatch", "not_checked", "no_text_layer", "no_numbers")
    for check in checks:
        res = _res("1,234.50", "元", check=check)
        assert not cell_supports_value(res, Decimal("1234.5")), check
    assert cell_supports_value(_res("1,234.50", "元", check="match"), Decimal("1234.5"))


def test_unresolved_never_supports() -> None:
    res = CellResolution(CellRef(1, 1, 0, 0), "no_grid", None, None, "", (), None, None)
    assert not cell_supports_value(res, Decimal("1"))
    assert not cell_supports_value(_res("5", "元", status="page_mismatch"), Decimal("5"))


def test_unlocated_cell_is_unverifiable() -> None:
    assert not cell_supports_value(_res("5", "元", page=None, bbox=None), Decimal("5"))
    assert not cell_supports_value(_res("5", "元", bbox=None), Decimal("5"))


def test_inline_unit_overrides_table_unit() -> None:
    assert cell_supports_value(_res("3.2 亿元", "万元"), Decimal("320000000"))
    assert not cell_supports_value(_res("3.2 亿元", "万元"), Decimal("32000"))


def test_non_cny_inline_or_table_currency_refused() -> None:
    assert not cell_supports_value(_res("3.2 万美元", "万元"), Decimal("32000"))
    assert not cell_supports_value(_res("3.2 万美元", "万元"), Decimal("320000"))
    assert not cell_supports_value(_res("10", "万元", currency="USD"), Decimal("100000"))
    assert not cell_supports_value(_res("10", "万元", currency="OTHER"), Decimal("100000"))
    assert cell_supports_value(_res("10", "万元", currency="CNY"), Decimal("100000"))
    assert cell_supports_value(_res("10", "万元", currency=None), Decimal("100000"))


def test_unknown_unit_does_not_support_amounts() -> None:
    assert not cell_supports_value(_res("1,000", "股"), Decimal("1000"))
    assert not cell_supports_value(_res("1,000", "%"), Decimal("1000"))
    # 百分号格与单位无关
    assert cell_supports_value(_res("12.5%", "股"), Decimal("0.125"))


def test_cell_value_none_is_not_supported() -> None:
    assert not cell_supports_value(_res("增加 16 . 70 个百分点", "元"), Decimal("16.7"))
    assert not cell_supports_value(_res("2023 年 1-3 月", "元"), Decimal("2023"))
    assert not cell_supports_value(_res("", "元"), Decimal("0"))


def test_zero_and_tolerance_edges() -> None:
    assert cell_supports_value(_res("0", "元"), Decimal("0"))
    assert not cell_supports_value(_res("0", "元"), Decimal("1"))


# --- db ---------------------------------------------------------------------------


def _grid() -> TableGrid:
    return TableGrid(
        cells=(
            GridCell(0, 0, 1, 1, "业务分部", page=1, bbox=BOX, is_header=True),
            GridCell(0, 1, 1, 1, "收入(万元)", page=1, bbox=BOX, is_header=True),
            GridCell(1, 0, 1, 1, "智能计算", page=1, bbox=BOX),
            GridCell(1, 1, 1, 1, "12,340", page=1, bbox=BOX, check="match"),
            GridCell(2, 0, 1, 1, "未定位", page=1, bbox=BOX),
            GridCell(2, 1, 1, 1, "55", page=None, bbox=None),
        ),
        source_engine="textin:t",
        provider_ref="p1",
        header_rows=1,
        header_cols=1,
    )


def _stored(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT block_id FROM core.doc_block WHERE block_type = 'table' AND doc_id = 1"
    ).fetchone()
    assert row is not None
    block_id = int(row[0])
    g = _grid()
    insert_table(
        conn,
        TableRecord(
            block_id=block_id,
            grid=g,
            context=bind_context(g, caption="分部收入", preceding_texts=["单位：万元"]),
            summary=summarize_grid(g),
            statement_kind=statement_kind(g),
            verify_version=VERIFY_CELLS_VERSION,
            desc_model=None,
            desc_prompt_version=None,
        ),
        ingest_run_id="r9",
    )
    conn.commit()
    return block_id


@pytest.mark.db
def test_resolve_ok_and_supports(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    b = _stored(corpus)
    res = resolve_cell(corpus, CellRef(b, 1, 1, 1), as_of=as_of_2024)
    assert res.status == "ok"
    assert res.cell is not None and res.cell.text == "12,340"
    assert res.row_label == "智能计算"
    assert res.col_header == ("收入(万元)",)
    assert res.unit == "万元"
    assert res.display_text == "12,340"
    assert res.verify_verdict is not None
    assert cell_supports_value(res, Decimal("123400000"))


@pytest.mark.db
def test_resolve_no_cell_and_page_mismatch(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    b = _stored(corpus)
    assert resolve_cell(corpus, CellRef(b, 1, 9, 9), as_of=as_of_2024).status == "no_cell"
    wrong = resolve_cell(corpus, CellRef(b, 2, 1, 1), as_of=as_of_2024)
    assert wrong.status == "page_mismatch"
    assert not cell_supports_value(wrong, Decimal("123400000"))
    # 引用写了页码，但该格无页码 → 不能确认
    assert resolve_cell(corpus, CellRef(b, 1, 2, 1), as_of=as_of_2024).status == "page_mismatch"
    # 格无页码时只有 "—" 引用才对得上，且仍然不可佐证
    unl = resolve_cell(corpus, CellRef(b, None, 2, 1), as_of=as_of_2024)
    assert unl.status == "ok"
    assert not cell_supports_value(unl, Decimal("550000"))


@pytest.mark.db
def test_resolve_before_known_at_is_no_grid(corpus: psycopg.Connection) -> None:
    b = _stored(corpus)
    res = resolve_cell(corpus, CellRef(b, 1, 1, 1), as_of=datetime(2024, 10, 1, tzinfo=UTC))
    assert res.status == "no_grid" and res.cell is None
    assert not cell_supports_value(res, Decimal("1"))


@pytest.mark.db
def test_can_show_raw_false_hides_display_text(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    b = _stored(corpus)
    corpus.execute("UPDATE core.doc_table SET can_show_raw = false WHERE block_id = %s", (b,))
    corpus.commit()
    res = resolve_cell(corpus, CellRef(b, 1, 1, 1), as_of=as_of_2024)
    assert res.status == "ok"
    assert res.display_text is None
    assert res.cell is not None and res.cell.text == "12,340"
    assert cell_supports_value(res, Decimal("123400000"))


@pytest.mark.db
def test_resolve_requires_tz_aware_as_of(corpus: psycopg.Connection) -> None:
    b = _stored(corpus)
    with pytest.raises(ValueError):
        resolve_cell(corpus, CellRef(b, 1, 1, 1), as_of=datetime(2024, 12, 31))
