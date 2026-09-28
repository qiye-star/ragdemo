"""派生值计算（CLAUDE.md §1.2：数值一律由代码算，模型只决定算什么）。

v1 只有累计同比。A 股定期报告的利润表是年初至今累计口径（2024Q3 = 1–9 月合计），
累计对累计的同比是标准算法；环比与 TTM 需要先把累计值拆成单季，放在阶段 2.5。

算不出来时 `value = None` 并在 `note` 里写明原因，不编一个数：
上年同期缺失、为 0、为负（亏损转盈利的"同比"会误导），或两期单位不一致。
"""

from __future__ import annotations

import re
from decimal import Decimal

from ragdemo.dataapi.contract import DerivedValue, FactValue

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
