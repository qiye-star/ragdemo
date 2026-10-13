from __future__ import annotations

from decimal import Decimal

from ragdemo.tables.context import (
    UNIT_SCALE,
    TableContext,
    bind_context,
    column_units,
    find_currency,
    find_unit,
    textin_caption,
    textin_preceding_texts,
)
from ragdemo.tables.grid import GridCell, TableGrid
from ragdemo.tables.headers import infer_headers


def _grid(*texts_by_row: tuple[str, ...]) -> TableGrid:
    cells = tuple(
        GridCell(r, c, 1, 1, t) for r, row in enumerate(texts_by_row) for c, t in enumerate(row)
    )
    return infer_headers(TableGrid(cells=cells, source_engine="x"))


def test_scales_are_exact() -> None:
    assert UNIT_SCALE["万元"] == Decimal(10000)
    assert UNIT_SCALE["亿元"] == Decimal(100000000)


def test_find_unit_and_currency() -> None:
    assert find_unit("单位：元 币种：人民币") == "元"
    assert find_unit("（单位：人民币万元）") == "万元"
    assert find_unit("本期金额") is None
    assert find_currency("单位：元 币种：人民币") == "CNY"


def test_unit_in_table_beats_preceding_block() -> None:
    g = _grid(("项目", "单位：千元"), ("营业收入", "1,000"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：元"])
    assert (ctx.unit, ctx.unit_source) == ("千元", "in_table")


def test_unit_from_nearest_preceding_block() -> None:
    g = _grid(("项目", "本期"), ("营业收入", "1,000"))
    ctx = bind_context(
        g, caption="合并利润表", preceding_texts=["单位：万元 币种：人民币", "单位：元"]
    )
    assert ctx == TableContext(
        caption="合并利润表", unit="万元", unit_source="preceding_block", currency="CNY"
    )


def test_no_unit_found() -> None:
    g = _grid(("项目", "本期"), ("营业收入", "1,000"))
    assert bind_context(g, caption=None, preceding_texts=["公司概况"]).unit_source == "none"


def test_column_unit_overrides_table_unit() -> None:
    g = _grid(("项目", "金额（万元）", "占比（%）"), ("营业收入", "1,000", "50"))
    assert column_units(g) == ((1, "万元"), (2, "%"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：元"])
    assert ctx.unit_for_col(1) == "万元" and ctx.unit_for_col(0) == "元"


def test_textin_caption_and_preceding() -> None:
    detail = [
        {
            "type": "paragraph",
            "page_id": 1,
            "paragraph_id": 12,
            "text": "单位：元 币种：人民币",
            "content": 0,
        },
        {
            "type": "paragraph",
            "page_id": 1,
            "paragraph_id": 13,
            "text": "合并资产负债表",
            "content": 0,
        },
        {"type": "paragraph", "page_id": 1, "paragraph_id": 14, "text": "页眉", "content": 1},
        {
            "type": "table",
            "page_id": 1,
            "paragraph_id": 15,
            "caption_id": {"page_id": 1, "paragraph_id": 13},
            "cells": [],
        },
    ]
    assert textin_caption(detail, detail[3]) == "合并资产负债表"
    assert textin_preceding_texts(detail, 3) == ["合并资产负债表", "单位：元 币种：人民币"]


# ---- 保守性：含糊 / 冲突 / 非人民币 / 非金额单位 → 不猜 ----


def test_non_money_unit_declaration_stops_search() -> None:
    """最近的块声明"单位：股"：不能越过它去用更远处的"单位：元"。"""
    g = _grid(("股东", "持股数量"), ("张三", "1,000"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：股", "单位：元 币种：人民币"])
    assert (ctx.unit, ctx.unit_source) == (None, "none")


def test_company_name_after_bianzhi_danwei_is_not_a_unit() -> None:
    assert find_unit("编制单位：元宇宙科技股份有限公司") is None
    assert find_unit("编制单位:海光信息技术股份有限公司") is None


def test_conflicting_units_in_one_block_is_unknown() -> None:
    g = _grid(("项目", "本期"), ("营业收入", "1,000"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：元；其中研发（单位：万元）"])
    assert (ctx.unit, ctx.unit_source) == (None, "none")


def test_conflicting_units_in_table_is_unknown() -> None:
    g = _grid(("项目", "单位：千元"), ("营业收入", "1,000"), ("注", "单位：元"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：万元"])
    assert (ctx.unit, ctx.unit_source) == (None, "none")


def test_currency_other_than_cny_is_reported() -> None:
    g = _grid(("项目", "本期"), ("营业收入", "1,000"))
    assert bind_context(g, caption=None, preceding_texts=["单位：元 币种：美元"]).currency == "USD"
    assert find_currency("单位：港元") == "HKD"
    assert find_currency("单位：万美元") == "USD"
    assert find_unit("单位：万美元") is None  # 不把"万美元"当"万元"
    assert find_currency("单位：元") is None  # 没写币种就是没写，不默认人民币


def test_unit_without_space_before_currency() -> None:
    assert find_unit("单位:元币种:人民币") == "元"
    assert find_currency("单位:元币种:人民币") == "CNY"
    assert find_currency("币种人民币") == "CNY"


def test_column_unit_conflict_is_unknown_for_that_column() -> None:
    g = _grid(("项目", "金额（万元）"), ("", "（元）"), ("营业收入", "1,000"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：千元"])
    assert ctx.unit_for_col(1) is None
    assert ctx.unit_for_col(0) == "千元"


def test_inline_unit_overrides() -> None:
    ctx = TableContext(None, "万元", "preceding_block", "CNY", ((2, "%"),))
    assert ctx.unit_for_cell(1, "元") == "元"
    assert ctx.unit_for_cell(1, None) == "万元"


def test_full_width_title_not_a_column_unit() -> None:
    cells = (
        GridCell(0, 0, 1, 3, "营业收入（万元）"),
        GridCell(1, 0, 1, 1, "项目"),
        GridCell(1, 1, 1, 1, "本期"),
        GridCell(1, 2, 1, 1, "上期"),
        GridCell(2, 0, 1, 1, "营业收入"),
        GridCell(2, 1, 1, 1, "1,000"),
        GridCell(2, 2, 1, 1, "900"),
    )
    g = infer_headers(TableGrid(cells=cells, source_engine="x"))
    assert column_units(g) == ()
