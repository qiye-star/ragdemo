"""scripts/l1_pilot.py 的纯函数单测（不碰 docker / 库 / blob）。"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("l1_pilot", Path("scripts/l1_pilot.py"))
assert _spec is not None and _spec.loader is not None
pilot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pilot)


def _cells(rows: list[list[str]]) -> list[dict[str, object]]:
    return [
        {"row": r, "col": c, "row_span": 1, "col_span": 1, "text": t}
        for r, row in enumerate(rows)
        for c, t in enumerate(row)
    ]


def test_wilson_matches_known_values() -> None:
    lo, hi = pilot.wilson(0, 10)
    assert lo == 0.0 and 0.25 < hi < 0.32
    lo, hi = pilot.wilson(10, 10)
    assert hi == 1.0 and 0.68 < lo < 0.75
    assert pilot.wilson(0, 0) == (0.0, 1.0)


def test_page_kind_label_priority() -> None:
    assert pilot.page_kind_label("合并现金流量表 资产负债表", 3, 9, 2) == "cash_flow"
    assert pilot.page_kind_label("合并资产负债表", 1, 9, 0) == "balance_sheet"
    assert pilot.page_kind_label("主要会计数据和财务指标", 1, 9, 0) == "key_figures"
    assert pilot.page_kind_label("x", 2, 9, 0) == "multi_table"
    assert pilot.page_kind_label("x", 1, 9, 2) == "multi_row_header"
    assert pilot.page_kind_label("x", 1, 4, 1) == "small_table"
    assert pilot.page_kind_label("x", 1, 12, 1) == "other_table"


def test_stratified_pick_is_deterministic_and_round_robin() -> None:
    cands = [("1", p, "key_figures") for p in range(10)] + [("2", p, "cash_flow") for p in range(3)]
    a = pilot.stratified_pick(cands, 6, 20261020)
    b = pilot.stratified_pick(list(reversed(cands)), 6, 20261020)
    assert a == b
    assert Counter(k for _, _, k in a) == {"cash_flow": 3, "key_figures": 3}
    assert len(pilot.stratified_pick(cands, 99, 1)) == len(cands)


def test_table_slices_and_cells() -> None:
    md = "x\n<table><tr><td>1</td></tr></table>\ntext\n<table><tr><td>2</td></tr></table>"
    assert len(pilot.table_slices(md)) == 2


def test_compare_pair_exact_and_shape_only_difference() -> None:
    ref = _cells([["项目", "2024"], ["收入", "1,234.5"]])
    assert pilot.compare_pair(ref, ref)["exact"] is True
    # 同样的单元格与数字，但多了一列 -> 形状不等
    eng = _cells([["项目", "2024", ""], ["收入", "1,234.5", ""]])
    r = pilot.compare_pair(ref, eng)
    assert r["shape_eq"] is False and r["num_eq"] is True and r["exact"] is False
    # 数字被改
    bad = _cells([["项目", "2024"], ["收入", "1,234.6"]])
    r = pilot.compare_pair(ref, bad)
    assert r["num_eq"] is False and r["text_eq"] is False


def test_compare_tables_order_and_count() -> None:
    t1 = _cells([["a", "1"]])
    t2 = _cells([["b", "2"]])
    swapped = pilot.compare_tables([t1, t2], [t2, t1])
    assert [p["exact"] for p in swapped["pairs"]] == [False, False]
    assert [p["any_order_exact"] for p in swapped["pairs"]] == [True, True]
    short = pilot.compare_tables([t1, t2], [t1])
    assert short["count_equal"] is False
    assert [p["exact"] for p in short["pairs"]] == [True, False]


def test_micro_counts_multiset() -> None:
    inter, ref, eng = pilot.micro_counts(Counter({"1": 2, "2": 1}), Counter({"1": 1, "3": 1}))
    assert (inter, ref, eng) == (1, 3, 2)


def test_table_number_counter_counts_per_cell() -> None:
    c = pilot.table_number_counter(_cells([["收入", "1,234.5"], ["同比", "-3.2%"]]))
    assert c["1234.5"] == 1


def test_classify_failure_labels() -> None:
    f = pilot.classify_failure
    assert f(
        error="EngineUnavailable: paddle 不可达",
        markdown="",
        verdict_ok=None,
        reasons=[],
        markup=[],
        grids_ok=False,
    ) == ["engine_timeout"]
    assert f(
        error=None, markdown="  ", verdict_ok=False, reasons=[], markup=[], grids_ok=False
    ) == ["empty_output"]
    tags = f(
        error=None,
        markdown="x",
        verdict_ok=False,
        reasons=["凭空数字：['1']", "丢失数字：['2']"],
        markup=[],
        grids_ok=False,
    )
    assert "invented_digits" in tags and "missing_digits" in tags
    assert f(error=None, markdown="x", verdict_ok=True, reasons=[], markup=[], grids_ok=False) == [
        "grid_conversion_failed"
    ]
    assert f(error=None, markdown="x", verdict_ok=True, reasons=[], markup=[], grids_ok=True) == []


def test_percentile_and_mem_parse() -> None:
    assert pilot.pct([1, 2, 3, 4, 5], 0.5) == 3
    assert pilot.pct([10], 0.95) == 10
    assert pilot.parse_mem_gib("12.3GiB / 13GiB") == pytest.approx(12.3)
    assert pilot.parse_mem_gib("512MiB / 13GiB") == pytest.approx(0.5)
    assert pilot.parse_mem_gib("garbage") is None


def test_blended_cost_matches_plan_formula() -> None:
    # 无 L1 命中：全部 TextIn
    assert pilot.blended_cost(scan_share=0.1, statement_share=0.1, l1_pass=0.0) == pytest.approx(
        0.1356
    )
    # L1 全过：只剩报表页与扫描页走 TextIn
    expected = (0.9 * 0.1 + 0.1) * 0.1356
    assert pilot.blended_cost(scan_share=0.1, statement_share=0.1, l1_pass=1.0) == pytest.approx(
        expected
    )
    # 盈亏点附近单调
    a = pilot.blended_cost(scan_share=0.16, statement_share=0.03, l1_pass=0.4)
    b = pilot.blended_cost(scan_share=0.16, statement_share=0.03, l1_pass=0.6)
    assert a > b


def test_census_shares() -> None:
    scan, stmt = pilot.census_shares(["announcement", "research"])
    assert scan == pytest.approx(800 / 4944)
    assert stmt == pytest.approx(130 / (4944 - 800))
