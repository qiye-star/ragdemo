"""单元格引用 `[block_id | page | r{row}c{col}]` 的解析、按 as_of 取格、数值佐证判定（T9）。

与验证器（docs/07 §5.1，尚未实现）的对接：
1. "数值出现在 citation 指向的原文里" → `cell_supports_value`；
2. "引用存在" → `resolve_cell(...).status == "ok"`；
3. "known_at <= as_of" → `resolve_cell` 只经 `asof.doc_table`，
   as_of 之后才可知的表不可见（`no_grid`）。

佐证判定刻意偏保守（宁可不佐证，也不假佐证）：
- 只认 `check == "match"` 的格（T7：与 PDF 文字层逐格比对通过）。`not_checked` /
  `no_text_layer` / `no_numbers` / `mismatch` 的格只能以 `推断` / 未校验 引用。
- `page` 或 `bbox` 为 None 的格（跨页表缺 page_id）不可验证。
- 整格只陈述一个数才算（`cell_value`，含格内行内单位）；含糊 → 不佐证。
- 单位：格内行内单位 > 列单位 > 表单位（`resolve_cell` 经 `unit_for_cell` 算好放进 `unit`）。
  不在 `UNIT_SCALE` 里的单位（万美元、股、%、人…）不换算 → 不佐证；币种不在 (None, "CNY") → 拒绝。
  单位未知（None，含行标签单位冲突）时只比较格内裸数字本身，不做任何换算。
- `display_text` 只在 `can_show_raw` 为真时给出原文（ADR-0014）；校验用的 `cell` 始终保留。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal

import psycopg

from ragdemo.dataapi.contract import require_as_of
from ragdemo.tables.context import UNIT_SCALE, TableContext
from ragdemo.tables.grid import GridCell
from ragdemo.tables.headers import col_header_path, row_label
from ragdemo.tables.numbers import cell_value
from ragdemo.tables.store import load_table

CELL_CITATION: Final = re.compile(r"\[(\d+)\s*\|\s*(\d+|—)\s*\|\s*r(\d+)c(\d+)\]")
REL_TOL: Final = Decimal("0.005")  # docs/07 §5.2 的 ±0.5%
_EPS: Final = Decimal("1e-9")

ResolveStatus = Literal["ok", "no_grid", "no_cell", "page_mismatch"]


@dataclass(frozen=True)
class CellRef:
    block_id: int
    page: int | None
    row: int
    col: int


@dataclass(frozen=True)
class CellResolution:
    ref: CellRef
    status: ResolveStatus
    cell: GridCell | None
    unit: str | None
    row_label: str
    col_header: tuple[str, ...]
    verify_verdict: str | None
    display_text: str | None
    currency: str | None = None  # 表级币种；None = 未声明（不等于人民币）


def parse_cell_citations(text: str) -> list[CellRef]:
    """文本里所有单元格级引用；块级 `[block_id | page]` 不匹配。"""
    return [
        CellRef(
            int(m.group(1)),
            None if m.group(2) == "—" else int(m.group(2)),
            int(m.group(3)),
            int(m.group(4)),
        )
        for m in CELL_CITATION.finditer(text)
    ]


def _empty(ref: CellRef, status: ResolveStatus) -> CellResolution:
    return CellResolution(ref, status, None, None, "", (), None, None)


def resolve_cell(
    conn: psycopg.Connection[Any],
    ref: CellRef,
    *,
    as_of: datetime,
    tenant: str | None = None,
    user: str | None = None,
) -> CellResolution:
    """按 as_of 取被引用的格（只读 `asof.doc_table`，私有表按 tenant/user 隔离）。

    `no_grid`：块没有网格，或网格在 as_of 时点不可知 / 已被取代；`no_cell`：坐标越界或落在空洞；
    `page_mismatch`：引用页码与格自己的页码不一致（含"引用写了页码但格未定位"与反过来）。
    """
    require_as_of(as_of)
    stored = load_table(conn, ref.block_id, as_of=as_of, tenant=tenant, user=user)
    if stored is None:
        return _empty(ref, "no_grid")
    cell = stored.grid.cell_at(ref.row, ref.col)
    if cell is None:
        return _empty(ref, "no_cell")
    if cell.page != ref.page:
        return _empty(ref, "page_mismatch")
    ctx = TableContext(
        caption=stored.caption,
        unit=stored.unit,
        unit_source="none",
        currency=stored.currency,
        column_units=stored.column_units,
        row_units=stored.row_units,
    )
    num = cell_value(cell.text)
    unit = ctx.unit_for_cell(cell.row, cell.col, num.inline_unit if num else None)
    header = col_header_path(stored.grid, cell.col, row=cell.row)
    return CellResolution(
        ref=ref,
        status="ok",
        cell=cell,
        unit=unit,
        row_label=row_label(stored.grid, cell.row),
        col_header=header or (),
        verify_verdict=stored.verify_verdict,
        display_text=cell.text if stored.can_show_raw else None,
        currency=stored.currency,
    )


def cell_supports_value(res: CellResolution, value: Decimal, *, rel_tol: Decimal = REL_TOL) -> bool:
    """被引用的格是否佐证 `value`（已归一化到元 / 小数比例）。见模块文档的保守规则。"""
    cell = res.cell
    if res.status != "ok" or cell is None:
        return False
    if cell.check != "match" or cell.page is None or cell.bbox is None:
        return False
    if res.currency not in (None, "CNY"):
        return False
    num = cell_value(cell.text)
    if num is None:
        return False
    if num.kind == "percent":
        candidate = num.value / 100
    else:
        unit = num.inline_unit or res.unit
        if unit is None:
            candidate = num.value
        else:
            scale = UNIT_SCALE.get(unit)
            if scale is None:
                return False
            candidate = num.value * scale
    return abs(candidate - value) <= rel_tol * max(abs(value), _EPS)


__all__ = [
    "CELL_CITATION",
    "REL_TOL",
    "CellRef",
    "CellResolution",
    "ResolveStatus",
    "cell_supports_value",
    "parse_cell_citations",
    "resolve_cell",
]
