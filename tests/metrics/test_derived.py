"""派生值计算（CLAUDE.md §1.2：同比由代码算，模型只决定算什么）。

v1 只有累计同比：本期累计值 / 上年同期累计值 − 1。A 股定期报告的利润表是年初至今
累计口径，累计对累计的同比是标准算法，不需要先拆成单季。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ragdemo.dataapi.contract import FactCitation, FactValue
from ragdemo.metrics.derived import (
    BALANCE_SHEET_METRICS,
    derive,
    is_flow_metric,
    prior_year_period,
    qoq,
    ttm,
    yoy,
)


def _fact(period: str, value: str, *, unit: str = "CNY") -> FactValue:
    year = int(period[:4])
    return FactValue(
        entity_id="CN.688041",
        metric_id="revenue_total",
        period=period,
        period_end=date(year, 9, 30),
        value=Decimal(value),
        unit=unit,
        currency="CNY",
        citation=FactCitation("revenue_total", "tushare", datetime(year, 10, 25, tzinfo=UTC), 1),
    )


@pytest.mark.parametrize(
    ("period", "expected"),
    [("2024Q3", "2023Q3"), ("2024Q1", "2023Q1"), ("2024FY", "2023FY"), ("2000Q4", "1999Q4")],
)
def test_prior_year_period(period: str, expected: str) -> None:
    assert prior_year_period(period) == expected


@pytest.mark.parametrize("bad", ["2024", "2024Q5", "24Q3", "2024H1", ""])
def test_prior_year_period_rejects_unknown_labels(bad: str) -> None:
    with pytest.raises(ValueError):
        prior_year_period(bad)


def test_yoy_growth() -> None:
    result = yoy(_fact("2024Q3", "120"), _fact("2023Q3", "100"))
    assert result.value == Decimal("0.2")
    assert result.note is None
    assert [f.period for f in result.inputs] == ["2024Q3", "2023Q3"], "两个输入都要带溯源"


def test_yoy_decline() -> None:
    assert yoy(_fact("2024Q3", "75"), _fact("2023Q3", "100")).value == Decimal("-0.25")


def test_yoy_is_exact_decimal_not_float() -> None:
    result = yoy(_fact("2024Q3", "1.1"), _fact("2023Q3", "3"))
    assert isinstance(result.value, Decimal)
    assert result.value == Decimal("1.1") / Decimal("3") - 1


def test_missing_base_gives_no_value_and_says_why() -> None:
    result = yoy(_fact("2024Q3", "120"), None)
    assert result.value is None and result.note is not None and "上年同期" in result.note
    assert [f.period for f in result.inputs] == ["2024Q3"]


def test_zero_base_gives_no_value() -> None:
    result = yoy(_fact("2024Q3", "120"), _fact("2023Q3", "0"))
    assert result.value is None and result.note is not None and "0" in result.note


def test_negative_base_gives_no_value() -> None:
    """亏损转盈利之类的"同比"没有意义：-100 → 50 算出 -150% 会误导读者。"""
    result = yoy(_fact("2024Q3", "50"), _fact("2023Q3", "-100"))
    assert result.value is None and result.note is not None and "负" in result.note


def test_unit_mismatch_gives_no_value() -> None:
    result = yoy(_fact("2024Q3", "120"), _fact("2023Q3", "100", unit="CNY_10K"))
    assert result.value is None and result.note is not None and "单位" in result.note


def test_mismatched_periods_are_rejected() -> None:
    with pytest.raises(ValueError, match="上年同期"):
        yoy(_fact("2024Q3", "120"), _fact("2023Q2", "100"))


# --- 环比 / TTM（阶段 2.5）----------------------------------------------------------


def _cum(period: str, value: str, *, unit: str = "CNY", metric: str = "revenue_total") -> FactValue:
    base = _fact(period, value, unit=unit)
    return FactValue(
        base.entity_id,
        metric,
        period,
        base.period_end,
        base.value,
        unit,
        base.currency,
        base.citation,
    )


# 累计口径：2024 Q1=100, H1=250, 9M=450, FY=700；2023 Q1=80, H1=200, 9M=360, FY=600。
# 单季：2024 Q1=100 Q2=150 Q3=200 Q4=250；2023 Q1=80 Q2=120 Q3=160 Q4=240。
CUM = {
    p: _cum(p, v)
    for p, v in {
        "2023Q1": "80",
        "2023Q2": "200",
        "2023Q3": "360",
        "2023FY": "600",
        "2024Q1": "100",
        "2024Q2": "250",
        "2024Q3": "450",
        "2024FY": "700",
    }.items()
}


def test_flow_qoq_uses_single_quarter_values_not_cumulative() -> None:
    d = qoq(CUM, "2024Q3", flow=True)
    assert d is not None and d.kind == "qoq" and d.note is None
    assert d.value == Decimal("200") / Decimal("150") - 1  # Q3 单季 200 / Q2 单季 150 − 1
    assert {f.period for f in d.inputs} == {"2024Q3", "2024Q2", "2024Q1"}  # 三个累计值都被引用


def test_qoq_of_q1_compares_against_last_years_fourth_quarter() -> None:
    d = qoq(CUM, "2024Q1", flow=True)
    assert d is not None
    assert d.value == Decimal("100") / Decimal("240") - 1  # 上年 Q4 单季 = FY 600 − 9M 360
    assert {f.period for f in d.inputs} == {"2024Q1", "2023FY", "2023Q3"}


def test_qoq_of_full_year_uses_the_fourth_quarter() -> None:
    d = qoq(CUM, "2024FY", flow=True)
    assert d is not None and d.value == Decimal("250") / Decimal("200") - 1  # Q4 250 / Q3 200


def test_qoq_missing_previous_cumulative_gives_no_number() -> None:
    partial = {k: v for k, v in CUM.items() if k != "2024Q2"}
    d = qoq(partial, "2024Q3", flow=True)
    assert d is not None and d.value is None and "2024Q2" in (d.note or "")


def test_qoq_on_a_non_positive_base_gives_no_number() -> None:
    facts = dict(CUM, **{"2024Q2": _cum("2024Q2", "100")})  # Q2 单季 = 100 − 100 = 0
    d = qoq(facts, "2024Q3", flow=True)
    assert d is not None and d.value is None and "为 0" in (d.note or "")
    facts = dict(CUM, **{"2024Q2": _cum("2024Q2", "50")})  # Q2 单季 = −50
    d = qoq(facts, "2024Q3", flow=True)
    assert d is not None and d.value is None and "为负" in (d.note or "")


def test_qoq_unit_mismatch_gives_no_number() -> None:
    facts = dict(CUM, **{"2024Q2": _cum("2024Q2", "250", unit="USD")})
    d = qoq(facts, "2024Q3", flow=True)
    assert d is not None and d.value is None and "单位" in (d.note or "")


def test_balance_sheet_qoq_uses_period_end_values() -> None:
    inv = {p: _cum(p, v, metric="inventory") for p, v in {"2024Q2": "100", "2024Q3": "120"}.items()}
    d = qoq(inv, "2024Q3", flow=False)
    assert d is not None and d.value == Decimal("0.2")
    assert {f.period for f in d.inputs} == {"2024Q3", "2024Q2"}
    q1 = {p: _cum(p, v, metric="inventory") for p, v in {"2023FY": "50", "2024Q1": "60"}.items()}
    d = qoq(q1, "2024Q1", flow=False)
    assert d is not None and d.value == Decimal("0.2")  # 上一季 = 上年年末


def test_ttm_adds_last_full_year_and_this_years_cumulative_minus_the_same_period_last_year() -> (
    None
):
    d = ttm(CUM, "2024Q3", flow=True)
    assert d is not None and d.value == Decimal("600") + Decimal("450") - Decimal("360")  # 690
    assert {f.period for f in d.inputs} == {"2024Q3", "2023FY", "2023Q3"}


def test_ttm_of_full_year_is_the_full_year() -> None:
    d = ttm(CUM, "2024FY", flow=True)
    assert d is not None and d.value == Decimal("700")


def test_ttm_missing_inputs_and_balance_sheet_metrics_give_no_number() -> None:
    d = ttm({k: v for k, v in CUM.items() if k != "2023FY"}, "2024Q3", flow=True)
    assert d is not None and d.value is None and "2023FY" in (d.note or "")
    inv = {"2024Q3": _cum("2024Q3", "120", metric="inventory")}
    d = ttm(inv, "2024Q3", flow=False)
    assert d is not None and d.value is None and "没有 TTM" in (d.note or "")


def test_derivations_return_none_when_the_period_itself_is_unknown() -> None:
    assert qoq(CUM, "2025Q1", flow=True) is None
    assert ttm(CUM, "2025Q1", flow=True) is None
    assert derive("yoy", CUM, "2025Q1", metric_id="revenue_total") is None


def test_derive_dispatches_by_kind_and_knows_flow_from_the_metric() -> None:
    assert derive("yoy", CUM, "2024Q3", metric_id="revenue_total") == yoy(
        CUM["2024Q3"], CUM["2023Q3"]
    )
    assert derive("qoq", CUM, "2024Q3", metric_id="revenue_total") == qoq(CUM, "2024Q3", flow=True)
    assert derive("ttm", CUM, "2024Q3", metric_id="revenue_total") == ttm(CUM, "2024Q3", flow=True)
    inv = {p: _cum(p, v, metric="inventory") for p, v in {"2024Q2": "100", "2024Q3": "120"}.items()}
    assert derive("qoq", inv, "2024Q3", metric_id="inventory") == qoq(inv, "2024Q3", flow=False)
    with pytest.raises(ValueError, match="未知的派生类型"):
        derive("mom", CUM, "2024Q3", metric_id="revenue_total")  # type: ignore[arg-type]


def test_balance_sheet_metric_set_matches_the_wind_field_table_and_the_seed() -> None:
    """`BALANCE_SHEET_METRICS`（期末值）必须与 Wind 适配器里 `flow=False` 的科目对应的指标一致，
    否则环比 / TTM 会对存量指标误用累计拆分。"""
    import csv

    from ragdemo.adapters.wind import WIND_FIELDS

    with open("db/seed/metric_source_map.csv", encoding="utf-8") as fh:
        mapped = {
            r["provider_field"]: r["metric_id"]
            for r in csv.DictReader(fh)
            if r["provider"] == "wind"
        }
    stock = {mapped[name] for name, f in WIND_FIELDS.items() if not f.flow}
    assert stock == set(BALANCE_SHEET_METRICS)
    assert all(is_flow_metric(mapped[n]) for n, f in WIND_FIELDS.items() if f.flow)
