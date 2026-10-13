"""表头推断（结果是"推断"，不是供应商给出的事实；TextIn 的 cells 不带表头标记）。"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Final

from ragdemo.tables.grid import GridCell, TableGrid

_VALUE: Final = re.compile(r"^[(（]?[-−]?\d[\d,，]*(?:\.\d+)?%?[)）]?$")


def is_value_text(text: str) -> bool:
    return bool(_VALUE.match(re.sub(r"\s+", "", text)))


def infer_headers(grid: TableGrid) -> TableGrid:
    n_rows, n_cols = grid.n_rows, grid.n_cols
    k = 0
    if n_rows >= 2:
        k = 1
        while k < n_rows - 1:
            spanning = [c for c in grid.cells if c.row < k < c.row + c.row_span]
            if spanning:
                k = max(c.row + c.row_span for c in spanning)
                continue
            row_k = [c for c in grid.cells if c.row == k]
            if any(c.col_span > 1 for c in row_k) and not any(is_value_text(c.text) for c in row_k):
                k += 1
                continue
            break
        k = min(k, n_rows - 1)
    h = 0
    if n_cols >= 2:
        h = 1
        while h < n_cols - 1:
            spanning = [c for c in grid.cells if c.row >= k and c.col < h < c.col + c.col_span]
            if not spanning:
                break
            h = max(c.col + c.col_span for c in spanning)
        h = min(h, n_cols - 1)
    cells = tuple(replace(c, is_header=(c.row < k or c.col < h)) for c in grid.cells)
    return replace(grid, cells=cells, header_rows=k, header_cols=h)


def _distinct_texts(cells: list[GridCell | None]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[tuple[int, int]] = set()
    for cell in cells:
        if cell is None or (cell.row, cell.col) in seen:
            continue
        seen.add((cell.row, cell.col))
        text = " ".join(cell.text.split())
        if text:
            out.append(text)
    return tuple(out)


def col_header_path(grid: TableGrid, col: int) -> tuple[str, ...]:
    return _distinct_texts([grid.cell_at(r, col) for r in range(grid.header_rows)])


def row_label(grid: TableGrid, row: int) -> str:
    return " / ".join(_distinct_texts([grid.cell_at(row, c) for c in range(grid.header_cols)]))
