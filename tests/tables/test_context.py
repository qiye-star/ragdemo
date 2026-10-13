from __future__ import annotations

from decimal import Decimal
from typing import Any

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
    assert ctx.unit_for_cell(0, 1, "元") == "元"
    assert ctx.unit_for_cell(0, 1, None) == "万元"
    assert ctx.unit_for_cell(0, 2, None) == "%"  # 列单位 > 表单位
    assert ctx.unit_for_cell(0, 2, "元") == "元"  # 行内单位 > 列单位


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


# ---- T6 评审修复波 ----


def _det(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return list(items)


def _p(pid: int, text: str, page: int = 1, content: int = 0) -> dict[str, Any]:
    return {
        "type": "paragraph",
        "page_id": page,
        "paragraph_id": pid,
        "text": text,
        "content": content,
    }


def _t(pid: int, page: int = 1, **kw: object) -> dict[str, Any]:
    return {"type": "table", "page_id": page, "paragraph_id": pid, "cells": [], "content": 0, **kw}


def test_unit_does_not_leak_across_an_intervening_table() -> None:
    d = _det(_p(1, "单位：万元"), _t(2), _t(3))
    assert textin_preceding_texts(d, 2) == []  # 第二张表前面最近的是表格：不越过
    d = _det(_p(1, "单位：万元"), _t(2), _p(3, "前十名股东持股情况"), _t(4))
    assert textin_preceding_texts(d, 3) == ["前十名股东持股情况"]  # 不越过表 T2 去拿"单位：万元"


def test_cross_page_continuation_may_cross_the_previous_pages_last_table() -> None:
    d = _det(
        _p(1, "单位：万元", page=1), _t(2, page=1), _p(3, "页脚", page=1, content=1), _t(4, page=2)
    )
    assert textin_preceding_texts(d, 3) == ["单位：万元"]
    # 前一页的表后面还有正文：不是同一张表的续表
    d = _det(_p(1, "单位：万元", page=1), _t(2, page=1), _p(3, "说明", page=1), _t(4, page=2))
    assert textin_preceding_texts(d, 3) == ["说明"]


def test_caption_after_the_table_is_rejected() -> None:
    d = _det(
        _t(1, caption_id={"page_id": 1, "paragraph_id": 2}),
        _p(2, "2.3 前 10 名股东持股情况表"),
    )
    assert textin_caption(d, d[0]) is None


def test_decl_block_length_boundary_80_81() -> None:
    g = _grid(("项目", "本期"), ("营业收入", "1,000"))
    base = "单位：万元"
    ok = base + "。" * (80 - len(base))
    too_long = base + "。" * (81 - len(base))
    assert bind_context(g, caption=None, preceding_texts=[ok]).unit == "万元"
    assert bind_context(g, caption=None, preceding_texts=[too_long]).unit is None


def test_currency_aliases_and_unknown_currency() -> None:
    for text, cur in [
        ("单位：元 币种：港币", "HKD"),
        ("单位：元 币种：美金", "USD"),
        ("单位：元 币种：USD", "USD"),
        ("单位：元 币种：HKD", "HKD"),
        ("单位：元 币种：欧元", "EUR"),
        ("单位：元 币种：日元", "JPY"),
        ("单位：元 US$", "USD"),
        ("单位：元 币种：RMB", "CNY"),
    ]:
        assert find_currency(text) == cur, text
    g = _grid(("项目", "本期"), ("营业收入", "1,000"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：元 币种：卢布"])
    assert ctx.unit is None and ctx.currency not in (None, "CNY")
    ctx = bind_context(g, caption=None, preceding_texts=["单位：元 币种：港币"])
    assert ctx.currency == "HKD"


def test_mixed_or_compound_unit_declarations_are_unknown() -> None:
    for text in ["单位：万元、%", "单位：万元/%", "单位：元，%", "单位：元/股", "单位：万元/吨"]:
        assert find_unit(text) is None, text
    assert find_unit("单位：万元，币种：人民币") == "万元"


def test_bare_percent_header_is_a_column_unit() -> None:
    g = _grid(("股东", "持股数量", "持股比例%"), ("张三", "1,000", "5.00"))
    assert column_units(g) == ((2, "%"),)
    g = _grid(("股东", "持股数量", "持股比例"), ("张三", "1,000", "5.00"))
    assert dict(column_units(g)).get(2) == "%"  # 比例/占比 等词 = 比率列，不是金额


def test_row_label_units_override_table_unit() -> None:
    g = _grid(
        ("项目", "本期", "上期"),
        ("营业收入", "1,000", "900"),
        ("基本每股收益（元/股）", "0.52", "0.40"),
        ("加权平均净资产收益率（%）", "12.5", "10.1"),
        ("公司研发人员的数量(人)", "1,283", "1,200"),
        ("研发人员数量占比", "90.42", "89.00"),
        ("其中:美元", "3,154,900.00", "6.9646"),
    )
    ctx = bind_context(g, caption=None, preceding_texts=["单位：万元"])
    assert ctx.unit_for_cell(1, 1, None) == "万元"
    for r in (2, 3, 4, 5, 6):
        assert ctx.unit_for_cell(r, 1, None) is None, r


def test_row_unit_agreeing_with_table_or_filling_a_gap() -> None:
    g = _grid(("项目", "本期"), ("净资产（万元）", "100"), ("研发投入（元）", "5"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：万元"])
    assert ctx.unit_for_cell(1, 1, None) == "万元"
    assert ctx.unit_for_cell(2, 1, None) is None  # 行说元、表说万元：冲突
    ctx = bind_context(g, caption=None, preceding_texts=[])
    assert ctx.unit_for_cell(2, 1, None) == "元"  # 表没声明，行声明了


def test_unit_in_one_row_label_is_not_promoted_to_the_table() -> None:
    g = _grid(("项目", "本期"), ("营业收入", "1,000"), ("投入环保资金(单位:万元)", "5"))
    ctx = bind_context(g, caption=None, preceding_texts=[])
    assert (ctx.unit, ctx.unit_source) == (None, "none")
    assert ctx.unit_for_cell(2, 1, None) == "万元"
    assert ctx.unit_for_cell(1, 1, None) is None


def test_column_unit_wins_but_row_conflict_still_blocks() -> None:
    g = _grid(("项目", "金额（万元）"), ("营业收入", "1,000"), ("毛利率（%）", "30"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：元"])
    assert ctx.unit_for_cell(1, 1, None) == "万元"
    assert ctx.unit_for_cell(2, 1, None) is None


# --- U5 单位核对表发现的缺陷（块 427 / 1529）----------------------------------


def test_ratio_vocabulary_covers_debt_ratio_and_coverage_multiple() -> None:
    from ragdemo.tables.context import row_units

    g = _grid(
        ("项目", "本报告期末", "上年末"),
        ("资产负债率", "80.60%", "71.49%"),
        ("EBITDA利息保障倍数", "4.10", "5.23"),
        ("销售费用率", "5.1", "4.9"),
        ("营业收入", "1,000", "900"),
    )
    assert dict(row_units(g)) == {1: "%", 2: "倍", 3: "%"}
    ctx = bind_context(g, caption=None, preceding_texts=["单位：万元"])
    assert ctx.unit_for_cell(1, 1, None) is None
    assert ctx.unit_for_cell(2, 1, None) is None
    assert ctx.unit_for_cell(4, 1, None) == "万元"


def test_table_of_only_ratios_and_multiples_does_not_inherit_preceding_unit() -> None:
    g = _grid(
        ("项目", "本报告期末", "上年末"),
        ("资产负债率", "80.60%", "71.49%"),
        ("EBITDA 利息保障倍数", "4.10", "5.23"),
    )
    ctx = bind_context(g, caption=None, preceding_texts=["单位：万元"])
    assert (ctx.unit, ctx.unit_source) == (None, "none")
    # 没有金额单位可继承；格级单位只剩比率标记（不可换算）
    assert ctx.unit_for_cell(1, 1, None) == "%" and ctx.unit_for_cell(2, 2, None) == "倍"


def test_only_percent_cells_also_drop_preceding_unit() -> None:
    g = _grid(("项目", "本期"), ("营业收入同比", "12.5%"), ("占比", "30.0%"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：元"])
    assert (ctx.unit, ctx.unit_source) == (None, "none")


def test_one_amount_cell_keeps_preceding_unit() -> None:
    g = _grid(
        ("项目", "本期"),
        ("资产负债率", "80.60%"),
        ("总资产", "1,000"),
    )
    ctx = bind_context(g, caption=None, preceding_texts=["单位：万元"])
    assert (ctx.unit, ctx.unit_source) == ("万元", "preceding_block")


def test_unparseable_numeric_cell_keeps_preceding_unit() -> None:
    # 无法判定这格是不是比率（区间）→ 保守，不丢单位
    g = _grid(("项目", "本期"), ("资产负债率", "80.60%"), ("某项目", "1,000-2,000"))
    ctx = bind_context(g, caption=None, preceding_texts=["单位：万元"])
    assert (ctx.unit, ctx.unit_source) == ("万元", "preceding_block")


def test_in_table_unit_is_kept_even_for_ratio_only_table() -> None:
    g = _grid(("项目", "本期"), ("资产负债率", "80.60%"), ("利息保障倍数", "4.10"))
    g_unit = _grid(("项目 单位：万元", "本期"), ("资产负债率", "80.60%"), ("利息保障倍数", "4.10"))
    assert bind_context(g, caption=None, preceding_texts=[]).unit is None
    ctx = bind_context(g_unit, caption=None, preceding_texts=[])
    assert (ctx.unit, ctx.unit_source) == ("万元", "in_table")


def test_percentage_point_change_cells_count_as_ratio() -> None:
    g = _grid(
        ("项目", "本期", "增减"),
        ("加权平均净资产收益率", "3.88", "增加4.45个百分点"),
        ("资产负债率", "80.60%", "增加1.2个百分点"),
    )
    ctx = bind_context(g, caption=None, preceding_texts=["单位：万元"])
    assert (ctx.unit, ctx.unit_source) == (None, "none")


def test_bracket_unit_tolerates_whitespace_and_br() -> None:
    from ragdemo.tables.context import row_units

    g = _grid(
        ("项目", "本期"),
        ("基本每股收益(元/ 股)", "0.29"),
        ("稀释每股收益(元/<br>股)", "0.29"),
        ("稀释每股收益（元 / 股）", "0.29"),
        ("净利润( 万 元 )", "5"),
    )
    assert dict(row_units(g)) == {1: "元/股", 2: "元/股", 3: "元/股", 4: "万元"}
    ctx = bind_context(g, caption=None, preceding_texts=["单位：元"])
    assert ctx.unit_for_cell(1, 1, None) is None  # 元/股 与表单位元冲突，不可换算
