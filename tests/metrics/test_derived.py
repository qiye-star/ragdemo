"""派生值计算（CLAUDE.md §1.2：同比由代码算，模型只决定算什么）。

v1 只有累计同比：本期累计值 / 上年同期累计值 − 1。A 股定期报告的利润表是年初至今
累计口径，累计对累计的同比是标准算法，不需要先拆成单季。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ragdemo.dataapi.contract import FactCitation, FactValue
from ragdemo.metrics.derived import prior_year_period, yoy


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
