"""引擎无关的表格网格（表格处理改进计划 T4）。Markdown/HTML 都由它派生。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import cached_property
from typing import TYPE_CHECKING, Any, Final, Literal, cast, get_args

if TYPE_CHECKING:  # T6 creates ragdemo.tables.context; drop the ignore then
    from ragdemo.tables.context import TableContext  # type: ignore[import-untyped, unused-ignore]

JSON_SCHEMA_VERSION: Final = 1
GRID_VERSION: Final = f"tablegrid:v{JSON_SCHEMA_VERSION}"
BBox = tuple[float, float, float, float]
CellCheck = Literal["not_checked", "match", "mismatch", "no_numbers", "no_text_layer"]


_CHECKS: Final = frozenset(get_args(CellCheck))


class GridError(ValueError):
    """网格结构不合法。"""


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class GridCell:
    row: int
    col: int
    row_span: int
    col_span: int
    text: str
    page: int | None = None
    bbox: BBox | None = None
    is_header: bool = False
    check: CellCheck = "not_checked"

    def __post_init__(self) -> None:
        if not all(_is_int(v) for v in (self.row, self.col, self.row_span, self.col_span)):
            raise GridError("行、列、跨度必须是 int")
        if not isinstance(self.text, str):
            raise GridError("text 必须是 str")
        if self.row < 0 or self.col < 0:
            raise GridError(f"行列必须 ≥ 0：({self.row}, {self.col})")
        if self.row_span < 1 or self.col_span < 1:
            raise GridError(f"跨度必须 ≥ 1：({self.row_span}, {self.col_span})")
        if self.page is not None and (not _is_int(self.page) or self.page < 1):
            raise GridError(f"页码必须 ≥ 1：{self.page!r}")
        if self.bbox is not None:
            if len(self.bbox) != 4 or not all(_is_num(v) for v in self.bbox):
                raise GridError(f"bbox 必须是 4 个有限数：{self.bbox!r}")
            x0, y0, x1, y1 = self.bbox
            if not (0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1):
                raise GridError(f"bbox 必须是归一化且 x0≤x1、y0≤y1：{self.bbox!r}")
        if not isinstance(self.is_header, bool):
            raise GridError("is_header 必须是 bool")
        if self.check not in _CHECKS:
            raise GridError(f"未知 check：{self.check!r}")

    def slots(self) -> list[tuple[int, int]]:
        return [
            (r, c)
            for r in range(self.row, self.row + self.row_span)
            for c in range(self.col, self.col + self.col_span)
        ]


@dataclass(frozen=True)
class TableGrid:
    cells: tuple[GridCell, ...]
    source_engine: str
    provider_ref: str | None = None
    header_rows: int = 0
    header_cols: int = 0

    def __post_init__(self) -> None:
        if not self.cells:
            raise GridError("网格没有任何单元格")
        if not (_is_int(self.header_rows) and _is_int(self.header_cols)):
            raise GridError("header_rows/header_cols 必须是 int")
        if not 0 <= self.header_rows <= self.n_rows:
            raise GridError(f"header_rows 越界：{self.header_rows}（行数 {self.n_rows}）")
        if not 0 <= self.header_cols <= self.n_cols:
            raise GridError(f"header_cols 越界：{self.header_cols}（列数 {self.n_cols}）")

    @property
    def n_rows(self) -> int:
        return max(c.row + c.row_span for c in self.cells)

    @property
    def n_cols(self) -> int:
        return max(c.col + c.col_span for c in self.cells)

    @property
    def pages(self) -> tuple[int, ...]:
        return tuple(sorted({c.page for c in self.cells if c.page is not None}))

    @cached_property
    def _slot_index(self) -> dict[tuple[int, int], GridCell]:
        index: dict[tuple[int, int], GridCell] = {}
        for cell in self.cells:  # 后出现者覆盖，与 table_markdown 一致
            for slot in cell.slots():
                index[slot] = cell
        return index

    def cell_at(self, row: int, col: int) -> GridCell | None:
        return self._slot_index.get((row, col))

    def holes(self) -> list[tuple[int, int]]:
        covered = {s for c in self.cells for s in c.slots()}
        return [
            (r, c) for r in range(self.n_rows) for c in range(self.n_cols) if (r, c) not in covered
        ]

    def overlaps(self) -> list[tuple[int, int]]:
        seen: set[tuple[int, int]] = set()
        dup: set[tuple[int, int]] = set()
        for cell in self.cells:
            for s in cell.slots():
                (dup if s in seen else seen).add(s)
        return sorted(dup)

    def as_cell_mappings(self) -> list[dict[str, Any]]:
        return [
            {
                "row": c.row,
                "col": c.col,
                "row_span": c.row_span,
                "col_span": c.col_span,
                "text": c.text,
            }
            for c in self.cells
        ]


def grid_to_json(grid: TableGrid) -> dict[str, Any]:
    return {
        "v": JSON_SCHEMA_VERSION,
        "engine": grid.source_engine,
        "ref": grid.provider_ref,
        "hr": grid.header_rows,
        "hc": grid.header_cols,
        "cells": [
            {
                "r": c.row,
                "c": c.col,
                "rs": c.row_span,
                "cs": c.col_span,
                "t": c.text,
                "p": c.page,
                "b": list(c.bbox) if c.bbox is not None else None,
                "h": c.is_header,
                "k": c.check,
            }
            for c in grid.cells
        ],
    }


def _req(d: Mapping[str, Any], key: str) -> object:
    if key not in d:
        raise GridError(f"网格 JSON 缺少键：{key!r}")
    return d[key]


def _int_field(d: Mapping[str, Any], key: str, default: int | None = None) -> int:
    v = d.get(key, default) if default is not None else _req(d, key)
    if not _is_int(v):
        raise GridError(f"网格 JSON 字段 {key!r} 必须是 int：{v!r}")
    return cast(int, v)


def _cell_from_json(c: object) -> GridCell:
    if not isinstance(c, Mapping):
        raise GridError("单元格 JSON 必须是对象")
    t = _req(c, "t")
    if not isinstance(t, str):
        raise GridError(f"字段 't' 必须是 str：{t!r}")
    page = c.get("p")
    if page is not None and not _is_int(page):
        raise GridError(f"字段 'p' 必须是 int 或 null：{page!r}")
    b = c.get("b")
    bbox: BBox | None = None
    if b is not None:
        if not isinstance(b, (list, tuple)) or len(b) != 4 or not all(_is_num(x) for x in b):
            raise GridError(f"字段 'b' 必须是 4 个数：{b!r}")
        bbox = cast(BBox, tuple(float(x) for x in b))
    h = c.get("h", False)
    if not isinstance(h, bool):
        raise GridError(f"字段 'h' 必须是 bool：{h!r}")
    k = c.get("k", "not_checked")
    if not isinstance(k, str) or k not in _CHECKS:
        raise GridError(f"字段 'k' 非法：{k!r}")
    return GridCell(
        _int_field(c, "r"),
        _int_field(c, "c"),
        _int_field(c, "rs"),
        _int_field(c, "cs"),
        t,
        page=cast("int | None", page),
        bbox=bbox,
        is_header=h,
        check=cast(CellCheck, k),
    )


def grid_from_json(data: Mapping[str, Any]) -> TableGrid:
    v = data.get("v")
    if not _is_int(v) or v != JSON_SCHEMA_VERSION:
        raise GridError(f"未知网格 JSON 版本：{v!r}")
    engine = _req(data, "engine")
    if not isinstance(engine, str):
        raise GridError(f"字段 'engine' 必须是 str：{engine!r}")
    ref = data.get("ref")
    if ref is not None and not isinstance(ref, str):
        raise GridError(f"字段 'ref' 必须是 str 或 null：{ref!r}")
    raw = _req(data, "cells")
    if not isinstance(raw, (list, tuple)):
        raise GridError("字段 'cells' 必须是数组")
    return TableGrid(
        cells=tuple(_cell_from_json(c) for c in raw),
        source_engine=engine,
        provider_ref=ref,
        header_rows=_int_field(data, "hr", 0),
        header_cols=_int_field(data, "hc", 0),
    )


def remap_grid_pages(grid: TableGrid, mapping: Mapping[int, int]) -> TableGrid:
    missing = sorted({p for p in grid.pages if p not in mapping})
    if missing:
        raise GridError(f"页码映射缺少：{missing}")
    return replace(
        grid,
        cells=tuple(
            replace(c, page=None if c.page is None else mapping[c.page]) for c in grid.cells
        ),
    )


@dataclass(frozen=True)
class TableAttachment:
    """随表格块流向 Chunk 的结构化附件（网格 + 可选上下文，上下文由 T6 填充）。"""

    grid: TableGrid
    context: TableContext | None = None
