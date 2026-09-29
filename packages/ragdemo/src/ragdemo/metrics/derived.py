"""派生值计算（CLAUDE.md §1.2：数值一律由代码算，模型只决定算什么）。

累计同比（v1）与环比、TTM（v1.2，阶段 2.5）。A 股定期报告的利润表是年初至今累计口径
（2024Q3 = 1–9 月合计），累计对累计的同比是标准算法；环比与 TTM 需要先把累计值拆成单季。

算不出来时 `value = None` 并在 `note` 里写明原因，不编一个数：
上年同期缺失、为 0、为负（亏损转盈利的"同比"会误导），或两期单位不一致。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from ragdemo.dataapi.contract import DerivedKind, DerivedValue, FactValue

_PERIOD = re.compile(r"^(\d{4})(Q[1-4]|FY)$")


def prior_year_period(period: str) -> str:
    """`2024Q3` → `2023Q3`，`2024FY` → `2023FY`。"""
    match = _PERIOD.match(period)
    if not match:
        raise ValueError(f"无法识别的报告期标签 {period!r}（应为 YYYYQ1–Q4 或 YYYYFY）")
    return f"{int(match.group(1)) - 1}{match.group(2)}"


def yoy(current: FactValue, base: FactValue | None) -> DerivedValue:
    """累计同比 = 本期 / 上年同期 − 1。"""

    def result(
        value: Decimal | None, note: str | None, inputs: tuple[FactValue, ...]
    ) -> DerivedValue:
        return DerivedValue(
            entity_id=current.entity_id,
            metric_id=current.metric_id,
            kind="yoy",
            period=current.period,
            value=value,
            note=note,
            inputs=inputs,
        )

    if base is None:
        return result(None, f"缺少上年同期（{prior_year_period(current.period)}）数据", (current,))
    if base.period != prior_year_period(current.period):
        raise ValueError(f"{base.period} 不是 {current.period} 的上年同期")
    inputs = (current, base)
    if base.unit != current.unit:
        return result(None, f"两期单位不一致（{current.unit} vs {base.unit}），不做同比", inputs)
    if base.value == 0:
        return result(None, "上年同期为 0，同比无定义", inputs)
    if base.value < 0:
        return result(None, "上年同期为负，同比没有意义（如亏损转盈利）", inputs)
    return result(current.value / base.value - 1, None, inputs)


# --- 环比 / TTM（阶段 2.5）----------------------------------------------------------
#
# fin_fact 的利润表、现金流量表科目存**年初至今累计**值：2025Q2 = 1–6 月合计，
# 2025FY = 全年。要算环比与 TTM 必须先把累计值拆成单季。资产负债表科目（存货、合同负债、
# 预付款项）是期末时点值，没有累计口径，环比直接用期末值，没有 TTM。

# 期末时点值的指标（没有累计口径）。与 Wind 适配器里 `flow=False` 的科目一一对应，
# 由 tests/metrics/test_derived.py 守着两处一致。
BALANCE_SHEET_METRICS: frozenset[str] = frozenset({"inventory", "contract_liability", "prepayment"})

_QUARTER_OF = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4}


def is_flow_metric(metric_id: str) -> bool:
    """利润表 / 现金流量表科目（累计口径）为 True；资产负债表期末值为 False。"""
    return metric_id not in BALANCE_SHEET_METRICS


def _split_period(period: str) -> tuple[int, int]:
    match = _PERIOD.match(period)
    if not match:
        raise ValueError(f"无法识别的报告期标签 {period!r}（应为 YYYYQ1–Q4 或 YYYYFY）")
    return int(match.group(1)), _QUARTER_OF[match.group(2)]


def _label(year: int, quarter: int) -> str:
    return f"{year}FY" if quarter == 4 else f"{year}Q{quarter}"


def _get(facts: Mapping[str, FactValue], year: int, quarter: int) -> FactValue | None:
    found = facts.get(_label(year, quarter))
    if found is None and quarter == 4:
        found = facts.get(f"{year}Q4")
    return found


def _previous_quarter(year: int, quarter: int) -> tuple[int, int]:
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


@dataclass(frozen=True)
class _Number:
    value: Decimal | None
    note: str | None
    inputs: tuple[FactValue, ...]


def _single_quarter(facts: Mapping[str, FactValue], year: int, quarter: int) -> _Number:
    """累计值拆单季：Q1 = 累计；其余 = 本期累计 − 上一期累计。"""
    current = _get(facts, year, quarter)
    if current is None:
        return _Number(None, f"缺少 {_label(year, quarter)} 累计数据", ())
    if quarter == 1:
        return _Number(current.value, None, (current,))
    prev = _get(facts, year, quarter - 1)
    if prev is None:
        return _Number(
            None,
            f"缺少 {_label(year, quarter - 1)}，无法把 {_label(year, quarter)} 拆成单季",
            (current,),
        )
    if prev.unit != current.unit:
        return _Number(None, f"两期单位不一致（{current.unit} vs {prev.unit}）", (current, prev))
    return _Number(current.value - prev.value, None, (current, prev))


def _dedupe(*groups: tuple[FactValue, ...]) -> tuple[FactValue, ...]:
    seen: dict[str, FactValue] = {}
    for group in groups:
        for fact in group:
            seen.setdefault(fact.period, fact)
    return tuple(seen.values())


def _derived(
    current: FactValue, kind: DerivedKind, number: _Number, extra: tuple[FactValue, ...] = ()
) -> DerivedValue:
    return DerivedValue(
        entity_id=current.entity_id,
        metric_id=current.metric_id,
        kind=kind,
        period=current.period,
        value=number.value,
        note=number.note,
        inputs=_dedupe((current,), number.inputs, extra),
    )


def qoq(facts: Mapping[str, FactValue], period: str, *, flow: bool) -> DerivedValue | None:
    """环比 = 本季 / 上一季 − 1。利润表 / 现金流量表科目用**单季值**（累计拆出来），
    资产负债表科目用期末值。上一季 ≤ 0 时环比没有意义，不给数。本期没有数据返回 None。"""
    year, quarter = _split_period(period)
    current = _get(facts, year, quarter)
    if current is None:
        return None
    prev_year, prev_quarter = _previous_quarter(year, quarter)
    if flow:
        now = _single_quarter(facts, year, quarter)
        before = _single_quarter(facts, prev_year, prev_quarter)
        inputs = _dedupe(now.inputs, before.inputs)
        missing = now.note or before.note
        if missing or now.value is None or before.value is None:
            return _derived(current, "qoq", _Number(None, missing, inputs))
        cur_value, base_value = now.value, before.value
    else:
        base = _get(facts, prev_year, prev_quarter)
        if base is None:
            note = f"缺少上一季（{_label(prev_year, prev_quarter)}）数据"
            return _derived(current, "qoq", _Number(None, note, (current,)))
        if base.unit != current.unit:
            note = f"两期单位不一致（{current.unit} vs {base.unit}）"
            return _derived(current, "qoq", _Number(None, note, (current, base)))
        inputs = (current, base)
        cur_value, base_value = current.value, base.value
    if base_value == 0:
        return _derived(current, "qoq", _Number(None, "上一季为 0，环比无定义", inputs))
    if base_value < 0:
        return _derived(current, "qoq", _Number(None, "上一季为负，环比没有意义", inputs))
    return _derived(current, "qoq", _Number(cur_value / base_value - 1, None, inputs))


def ttm(facts: Mapping[str, FactValue], period: str, *, flow: bool) -> DerivedValue | None:
    """滚动 12 个月：FY = 全年；其余 = 上年全年 + 本期累计 − 上年同期累计。
    只对累计口径的科目有意义；期末时点值没有 TTM。本期没有数据返回 None。"""
    year, quarter = _split_period(period)
    current = _get(facts, year, quarter)
    if current is None:
        return None
    if not flow:
        return _derived(current, "ttm", _Number(None, "期末时点指标没有 TTM", (current,)))
    if quarter == 4:
        return _derived(current, "ttm", _Number(current.value, None, (current,)))
    last_fy = _get(facts, year - 1, 4)
    same_last_year = _get(facts, year - 1, quarter)
    missing = [
        _label(year - 1, q) for f, q in ((last_fy, 4), (same_last_year, quarter)) if f is None
    ]
    if last_fy is None or same_last_year is None:
        note = f"缺少 {'、'.join(missing)}，无法算 TTM"
        return _derived(
            current, "ttm", _Number(None, note, tuple(f for f in (last_fy, same_last_year) if f))
        )
    inputs = (current, last_fy, same_last_year)
    if len({f.unit for f in inputs}) != 1:
        return _derived(current, "ttm", _Number(None, "各期单位不一致，不算 TTM", inputs))
    return _derived(
        current, "ttm", _Number(last_fy.value + current.value - same_last_year.value, None, inputs)
    )


def derive(
    kind: DerivedKind, facts: Mapping[str, FactValue], period: str, *, metric_id: str
) -> DerivedValue | None:
    """三种派生值的统一入口。`facts` 是该实体该指标在 `as_of` 时刻已知的全部期别（键为期别）。"""
    flow = is_flow_metric(metric_id)
    if kind == "yoy":
        year, quarter = _split_period(period)
        current = _get(facts, year, quarter)
        if current is None:
            return None
        return yoy(current, facts.get(prior_year_period(period)))
    if kind == "qoq":
        return qoq(facts, period, flow=flow)
    if kind == "ttm":
        return ttm(facts, period, flow=flow)
    raise ValueError(f"未知的派生类型 {kind!r}")
