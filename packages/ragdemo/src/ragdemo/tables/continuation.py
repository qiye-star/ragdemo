"""非 L3 表格的跨页续表**链接**（表格处理改进计划 T15）：只链接不合并。

相邻两张表 A、B（ordinal 相邻）满足全部条件才链接：B.page = A.page + 1；A 是其页最后一个块、
B 是其页第一个块；列数相等；两者都不是 L3（TextIn 已 apply_merge）；且（B 首行逐格等于 A
表头行，或 B 首行含数值格）。全空的表头行不算"重复表头"。
已知局限：夹页脚/图、列数因合并不同、L3 与 L1 交界——漏链；新表恰好以数值行开头——误链。
链接只写 `continues_from_block_id`，不改任何单元格或数值。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from ragdemo.tables.headers import is_value_text


@dataclass(frozen=True)
class TableSlot:
    ordinal: int
    page: int
    n_cols: int
    header_row: tuple[str, ...]
    first_row: tuple[str, ...]
    first_on_page: bool
    last_on_page: bool
    tier: str | None


def _norm(row: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(" ".join(c.split()) for c in row)


def _links(a: TableSlot, b: TableSlot) -> bool:
    if a.tier == "L3" or b.tier == "L3":
        return False
    if b.page != a.page + 1 or not a.last_on_page or not b.first_on_page:
        return False
    if a.n_cols != b.n_cols:
        return False
    head = _norm(a.header_row)
    first = _norm(b.first_row)
    repeated = any(head) and first == head
    return repeated or any(is_value_text(c) for c in first)


def continuation_links(slots: Sequence[TableSlot]) -> dict[int, int]:
    """续表 ordinal → 前表 ordinal。"""
    ordered = sorted(slots, key=lambda s: s.ordinal)
    return {
        b.ordinal: a.ordinal
        for a, b in pairwise(ordered)
        if b.ordinal == a.ordinal + 1 and _links(a, b)
    }
