"""表头推断（结果是"推断"，不是供应商给出的事实；TextIn 的 cells 不带表头标记）。

约定（T4 评审后）：
- 表头行 = 第 0 行（若它不含数据值）+ 被跨行单元格贯穿的行 + 首列为空的无数值续行。
  行 0 含数据值（年份除外）→ 没有表头（header_rows = 0）。
- 整行通栏的单元格（col_span = 列数）是**标题/分节标签**，不是列表头：
  行 0 的通栏行算表头块但不进列路径；表体里的通栏行是分节，用 `row_section` 取。
- 表头列 = 表体中值得当行标签的前导列；表体里的 colspan（小计行等）**不**推动 header_cols。
  序号列（表头为"序号/编号"或整列是纯整数）计入表头列，但 `row_label` 跳过数值文本。
- 表体中重复出现的子表头行（无数值、≥2 个非空格）会让其下方的列路径不可信：
  `col_header_path(grid, col, row=r)` 返回 None（宁可没有，不给错的）。
- 任何数值文本（年份在表头行内除外）都不会被标成 is_header。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Final

from ragdemo.parse.local.number_closure import fold_text
from ragdemo.tables.grid import GridCell, TableGrid
from ragdemo.tables.numbers import cell_value

_YEAR: Final = re.compile(r"^(?:19|20)[0-9]{2}$")
_BR: Final = re.compile(r"<br\s*/?>", re.IGNORECASE)
_INDEX_WORDS: Final = frozenset({"序号", "编号", "序", "no", "no."})


def is_value_text(text: str) -> bool:
    """整格就是一个数（与 numbers.cell_value 同一口径：ASCII 数字、无空格拆分、无序号括号）。"""
    return cell_value(text) is not None


def _norm(text: str) -> str:
    return " ".join(_BR.sub(" ", text).split())


def _is_year(text: str) -> bool:
    return bool(_YEAR.match(fold_text(_norm(text))))


_NUMERIC_SHAPE: Final = re.compile(r"[0-9,.%()\s<>/br-]+")


def _numeric_shape(text: str) -> bool:
    """只由数字与数字标点组成（含被 <br> 拆开的残缺数字）：不是值，但肯定也不是文字标签。"""
    t = fold_text(text)
    return any(ch.isdigit() for ch in t) and bool(_NUMERIC_SHAPE.fullmatch(t))


def _is_data_text(text: str) -> bool:
    return (is_value_text(text) or _numeric_shape(text)) and not _is_year(text)


def _is_full_width(grid: TableGrid, cell: GridCell) -> bool:
    return grid.n_cols >= 2 and cell.col == 0 and cell.col_span >= grid.n_cols


def _row_cells(grid: TableGrid, r: int) -> list[GridCell]:
    return [c for c in grid.cells if c.row == r]


def _row_has_data(grid: TableGrid, r: int) -> bool:
    return any(_is_data_text(c.text) for c in _row_cells(grid, r))


def _is_label_row(grid: TableGrid, r: int) -> bool:
    cells = _row_cells(grid, r)
    return (
        bool(cells)
        and all(_is_full_width(grid, c) for c in cells)
        and any(_norm(c.text) for c in cells)
    )


def _header_rows(grid: TableGrid) -> int:
    n = grid.n_rows
    if n < 2 or _row_has_data(grid, 0):
        return 0
    k = 1
    if (
        _is_label_row(grid, 0)
        and n >= 3
        and not _row_has_data(grid, 1)
        and not _is_label_row(grid, 1)
    ):
        k = 2  # 行 0 是标题，行 1 才是列表头
    while k < n - 1:
        crossing = [c for c in grid.cells if c.row < k < c.row + c.row_span]
        if crossing:
            new_k = max(c.row + c.row_span for c in crossing)
            if any(_row_has_data(grid, r) for r in range(k, new_k)):
                break  # 竖向合并贯穿了数据行：不当表头
            k = new_k
            continue
        first = grid.cell_at(k, 0)
        if (
            not _row_has_data(grid, k)
            and not _is_label_row(grid, k)
            and (first is None or not _norm(first.text))
            and any(_norm(c.text) for c in _row_cells(grid, k))
        ):
            k += 1
            continue
        break
    return min(k, n - 1)


def _col_is_labels(grid: TableGrid, col: int, k: int) -> bool:
    body = [
        _norm(c.text)
        for c in grid.cells
        if c.col == col and c.row >= k and c.col_span == 1 and _norm(c.text)
    ]
    if not body:
        return True
    values = sum(1 for t in body if _is_data_text(t))
    return values * 2 < len(body)


def _is_index_col(grid: TableGrid, col: int, k: int) -> bool:
    head = [_norm(c.text).lower() for c in grid.cells if c.col == col and c.row < k]
    if any(t in _INDEX_WORDS for t in head):
        return True
    body = [_norm(c.text) for c in grid.cells if c.col == col and c.row >= k and _norm(c.text)]
    return len(body) >= 2 and all(re.fullmatch(r"[0-9]{1,3}", fold_text(t)) for t in body)


def _header_cols(grid: TableGrid, k: int) -> int:
    n = grid.n_cols
    if n < 2:
        return 0
    first_is_index = _is_index_col(grid, 0, k)
    if not first_is_index and not _col_is_labels(grid, 0, k):
        return 0
    h = 1
    while h < n - 1:
        prev_is_index = _is_index_col(grid, h - 1, k)
        # 只有"行分组"（上一列在表体里有竖向合并）或序号列之后才把下一列也当行标签
        grouped = any(c.col == h - 1 and c.row >= k and c.row_span > 1 for c in grid.cells)
        if (prev_is_index or grouped) and _col_is_labels(grid, h, k):
            h += 1
            continue
        break
    # 只有完全落在表头块内的跨列单元格才能推动 header_cols（表体 colspan 一律忽略；通栏标题忽略）
    changed = True
    while changed:
        changed = False
        for c in grid.cells:
            if (
                c.row + c.row_span <= k
                and not _is_full_width(grid, c)
                and c.col < h < c.col + c.col_span
                and min(c.col + c.col_span, n - 1) > h
            ):
                h = min(c.col + c.col_span, n - 1)
                changed = True
    return h


def infer_headers(grid: TableGrid) -> TableGrid:
    k = _header_rows(grid)
    h = _header_cols(grid, k)
    cells = []
    for c in grid.cells:
        in_header_rows = c.row < k
        in_header_cols = c.col < h
        marked = (in_header_rows or in_header_cols) and not _is_data_text(c.text)
        if not in_header_rows and is_value_text(c.text):
            marked = False  # 表体里的数值（含年份）永远不是表头
        cells.append(replace(c, is_header=marked))
    return replace(grid, cells=tuple(cells), header_rows=k, header_cols=h)


def _distinct_texts(cells: list[GridCell | None], grid: TableGrid) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[tuple[int, int]] = set()
    for cell in cells:
        if cell is None or (cell.row, cell.col) in seen:
            continue
        seen.add((cell.row, cell.col))
        if _is_full_width(grid, cell):
            continue  # 通栏标题/分节不是列表头
        text = _norm(cell.text)
        if text:
            out.append(text)
    return tuple(out)


def subheader_rows(grid: TableGrid) -> list[int]:
    """表体里疑似重复/替换表头的行（无数值、≥2 个非空格、非通栏）。只在表体有数值时才有意义。"""
    k = grid.header_rows
    if not any(_is_data_text(c.text) for c in grid.cells if c.row >= k):
        return []
    out: list[int] = []
    for r in range(k, grid.n_rows):
        cells = _row_cells(grid, r)
        texts = [_norm(c.text) for c in cells if _norm(c.text)]
        if (
            len(texts) >= 2
            and not any(_is_data_text(t) for t in texts)
            and not _is_label_row(grid, r)
        ):
            out.append(r)
    return out


def col_header_path(grid: TableGrid, col: int, row: int | None = None) -> tuple[str, ...] | None:
    """列的表头路径。给出 `row` 时，若该行位于某个子表头行（含）之下 → None（路径不可信）。"""
    if row is not None and any(s <= row for s in subheader_rows(grid)):
        return None
    return _distinct_texts([grid.cell_at(r, col) for r in range(grid.header_rows)], grid)


def row_label(grid: TableGrid, row: int) -> str:
    """行标签：表头列里的非数值文本。序号等数值文本被跳过；没有可用标签时返回 ""。"""
    parts = _distinct_texts([grid.cell_at(row, c) for c in range(grid.header_cols)], grid)
    return " / ".join(p for p in parts if not _is_data_text(p) and not is_value_text(p))


def row_section(grid: TableGrid, row: int) -> str | None:
    """该行所在的分节（最近一个在它之前、位于表体内的通栏标签行）；没有 → None。"""
    for r in range(row - 1, grid.header_rows - 1, -1):
        if _is_label_row(grid, r):
            cell = grid.cell_at(r, 0)
            return _norm(cell.text) if cell is not None else None
    return None
