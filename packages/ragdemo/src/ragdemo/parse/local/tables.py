"""按词坐标还原表格（纯函数，不依赖任何 PDF 库）。

表格的单元格对齐信息只存在于坐标里——把页面压平成文字再交给大模型，它只能靠猜来分列，
数字可能被放进错误的列（这种错误"数字都出现过"的闭合校验抓不到）。所以先在本地按坐标
把表格还原好，再交给大模型只做排版。"""

from __future__ import annotations

import itertools
from collections.abc import Sequence

from ragdemo.parse.local.layout import Word


def cluster_rows(words: Sequence[Word], *, y_tol: float = 3.0) -> list[list[Word]]:
    """按纵向中点聚成行，行内按 x0 升序；行按纵向位置升序。"""
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.mid_y, w.x0)):
        if rows and abs(rows[-1][-1].mid_y - w.mid_y) <= y_tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w.x0) for r in rows]


def column_edges(rows: Sequence[Sequence[Word]], *, gap: float = 8.0) -> list[tuple[float, float]]:
    """把所有行的词投影到横轴，空隙 >= gap 处切列；返回每列 (左, 右)。"""
    spans = sorted((w.x0, w.x1) for r in rows for w in r)
    cols: list[tuple[float, float]] = []
    for x0, x1 in spans:
        if cols and x0 - cols[-1][1] < gap:
            cols[-1] = (cols[-1][0], max(cols[-1][1], x1))
        else:
            cols.append((x0, x1))
    return cols


def rows_to_grid(
    rows: Sequence[Sequence[Word]], cols: Sequence[tuple[float, float]]
) -> list[list[str]]:
    """将行和列信息转换为字符串网格。"""
    grid: list[list[str]] = []
    for r in rows:
        cells = [""] * len(cols)
        for w in r:
            idx = next(i for i, (a, b) in enumerate(cols) if w.x0 >= a - 0.5 and w.x1 <= b + 0.5)
            cells[idx] = f"{cells[idx]} {w.text}".strip()
        grid.append(cells)
    return grid


def grid_to_markdown(grid: Sequence[Sequence[str]]) -> str:
    """将网格转换为 Markdown 表格。"""
    if not grid:
        return ""
    header, *body = grid
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def split_segments(row: Sequence[Word], gap: float) -> list[list[Word]]:
    """把一行（已按 x0 升序）在 >= gap 的横向空隙处切成段。"""
    segs: list[list[Word]] = []
    for w in row:
        if segs and w.x0 - segs[-1][-1].x1 < gap:
            segs[-1].append(w)
        else:
            segs.append([w])
    return segs


def _segments(row: Sequence[Word], gap: float) -> int:
    """一行里被 >= gap 的横向空隙隔开的段数；正文句子里词间空隙很小，只有 1 段。"""
    count = 1
    for a, b in itertools.pairwise(row):
        if b.x0 - a.x1 >= gap:
            count += 1
    return count


def find_table_regions(
    rows: Sequence[Sequence[Word]], *, gap: float = 8.0, min_rows: int = 3, min_cols: int = 3
) -> list[list[list[Word]]]:
    """表格区域 = 连续的、每行都至少有 2 段的行；区域 >= min_rows 行且列数 >= min_cols 才算。

    必须先在行级别圈出区域、再只对区域算列：整页一起投影会被正文句子桥接，
    把不同列并成一列（混排页——正文加一张表——是最常见的形态）。"""
    regions: list[list[list[Word]]] = []
    run: list[list[Word]] = []
    for row in [*rows, []]:  # 末尾补一个空行，冲刷最后一段
        if row and _segments(row, gap) >= 2:
            run.append(list(row))
            continue
        if len(run) >= min_rows and len(column_edges(run, gap=gap)) >= min_cols:
            regions.append(run)
        run = []
    return regions
