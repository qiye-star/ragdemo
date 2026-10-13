"""引擎无关的表格网格（表格处理改进计划 T4）。Markdown/HTML 都由它派生。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, cast

GRID_VERSION: Final = "tablegrid:v1"
BBox = tuple[float, float, float, float]
CellCheck = Literal["not_checked", "match", "mismatch", "no_numbers", "no_text_layer"]


class GridError(ValueError):
    """网格结构不合法。"""


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
        if self.row < 0 or self.col < 0:
            raise GridError(f"行列必须 ≥ 0：({self.row}, {self.col})")
        if self.row_span < 1 or self.col_span < 1:
            raise GridError(f"跨度必须 ≥ 1：({self.row_span}, {self.col_span})")

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

    @property
    def n_rows(self) -> int:
        return max(c.row + c.row_span for c in self.cells)

    @property
    def n_cols(self) -> int:
        return max(c.col + c.col_span for c in self.cells)

    @property
    def pages(self) -> tuple[int, ...]:
        return tuple(sorted({c.page for c in self.cells if c.page is not None}))

    def cell_at(self, row: int, col: int) -> GridCell | None:
        found: GridCell | None = None
        for cell in self.cells:
            if (row, col) in cell.slots():
                found = cell
        return found

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
        "v": 1,
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


def grid_from_json(data: Mapping[str, Any]) -> TableGrid:
    if int(data.get("v", 0)) != 1:
        raise GridError(f"未知网格 JSON 版本：{data.get('v')!r}")
    cells = tuple(
        GridCell(
            int(c["r"]),
            int(c["c"]),
            int(c["rs"]),
            int(c["cs"]),
            str(c["t"]),
            page=None if c.get("p") is None else int(c["p"]),
            bbox=None if c.get("b") is None else cast(BBox, tuple(float(x) for x in c["b"])),
            is_header=bool(c.get("h", False)),
            check=cast(CellCheck, str(c.get("k", "not_checked"))),
        )
        for c in data["cells"]
    )
    return TableGrid(
        cells=cells,
        source_engine=str(data["engine"]),
        provider_ref=None if data.get("ref") is None else str(data["ref"]),
        header_rows=int(data.get("hr", 0)),
        header_cols=int(data.get("hc", 0)),
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
