"""报表类型识别（表格处理改进计划 T6）：只看行标签，按固定顺序判定，判不出就是 other。"""

from __future__ import annotations

import re
from typing import Final, Literal

from ragdemo.ingest.stage2_crosscheck import FACT_LABELS, normalise_name
from ragdemo.tables.grid import TableGrid
from ragdemo.tables.headers import row_label

StatementKind = Literal["balance_sheet", "income", "cash_flow", "key_figures", "other"]

# "一、营业总收入""减：营业成本""其中：…""（1）…"这类序号/前缀不影响科目名。
_PREFIX: Final = re.compile(
    r"^(?:(?:[一二三四五六七八九十]+|[0-9]+)[、.．]|[（(](?:[一二三四五六七八九十]+|[0-9]+)[)）]"
    r"|(?:加|减|其中)[:：])+"
)


def _key(label: str) -> str:
    return _PREFIX.sub("", normalise_name(label).replace("：", ":"))


_BALANCE: Final = frozenset(
    _key(s) for s in ("流动资产合计", "资产总计", "负债合计", "所有者权益合计")
)
_INCOME: Final = frozenset(
    _key(s) for s in ("营业收入", "营业总收入", "营业成本", "净利润", "利润总额")
)
_CASH: Final = frozenset(
    _key(s)
    for s in (
        "经营活动产生的现金流量净额",
        "投资活动产生的现金流量净额",
        "筹资活动产生的现金流量净额",
    )
)
_FACT: Final = frozenset(_key(s) for labels in FACT_LABELS.values() for s in labels)


def statement_kind(grid: TableGrid) -> StatementKind:
    keys: set[str] = set()
    for r in range(grid.header_rows, grid.n_rows):
        for part in row_label(grid, r).split(" / "):
            if part:
                keys.add(_key(part))
    if len(keys & _BALANCE) >= 2:
        return "balance_sheet"
    if len(keys & _INCOME) >= 2:
        return "income"
    if len(keys & _CASH) >= 2:
        return "cash_flow"
    if keys & _FACT:
        return "key_figures"
    return "other"
