"""逐单元格文字层校验（表格处理改进计划 T7）。

每个含数字的单元格，其数字必须（多重集合意义上）出现在该格 bbox 内的 PDF 文字层词里。
校验是单页的：不做 ±1 页放宽，相邻页偏移记为 mismatch（虚警可接受，假通过不可接受）。
扫描页（无文字层）、旋转页、无坐标的格各自是独立状态，永远不记为 match。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.tiered.types import Tier
from ragdemo.tables.grid import BBox, CellCheck, GridCell, TableGrid

VERIFY_CELLS_VERSION: Final = "cellverify:v1"
BBOX_PAD_PT: Final = 1.5

TableVerdict = Literal[
    "text_layer_cells_ok",
    "text_layer_cells_mismatch",
    "text_layer_page_closed",
    "dual_agree",
    "scan_single_unverified",
    "not_checked",
]


@dataclass(frozen=True)
class PageWords:
    page: int
    width: float
    height: float
    words: tuple[Word, ...]
    rotation: int = 0


def page_words_from_layouts(layouts: Sequence[PageLayout]) -> dict[int, PageWords]:
    return {
        lay.page: PageWords(lay.page, lay.width, lay.height, tuple(lay.words), lay.rotation)
        for lay in layouts
    }


def words_in_bbox(pw: PageWords, bbox: BBox, *, pad: float = BBOX_PAD_PT) -> list[Word]:
    x0, y0, x1, y1 = (
        bbox[0] * pw.width,
        bbox[1] * pw.height,
        bbox[2] * pw.width,
        bbox[3] * pw.height,
    )
    hit = [
        w
        for w in pw.words
        if x0 - pad <= (w.x0 + w.x1) / 2 <= x1 + pad and y0 - pad <= w.mid_y <= y1 + pad
    ]
    return sorted(hit, key=lambda w: (w.top, w.x0))


def check_cell(cell: GridCell, pages: Mapping[int, PageWords]) -> CellCheck:
    need = numbers_in(cell.text)
    if not need:
        return "no_numbers"
    if cell.page is None or cell.bbox is None:
        return "not_checked"
    pw = pages.get(cell.page)
    if pw is not None and pw.rotation != 0:
        return "not_checked"
    if pw is None or not pw.words:
        return "no_text_layer"
    have = numbers_in(" ".join(w.text for w in words_in_bbox(pw, cell.bbox)))
    return "match" if all(have.get(tok, 0) >= n for tok, n in need.items()) else "mismatch"


def verify_grid(grid: TableGrid, pages: Mapping[int, PageWords]) -> TableGrid:
    return replace(grid, cells=tuple(replace(c, check=check_cell(c, pages)) for c in grid.cells))


@dataclass(frozen=True)
class GridVerifySummary:
    numeric: int
    match: int
    mismatch: int
    unverifiable: int  # no_text_layer + not_checked，只计数字格
    verdict: TableVerdict


def summarize_grid(grid: TableGrid, *, tier: Tier | None = None) -> GridVerifySummary:
    match = mismatch = no_layer = unchecked = 0
    for c in grid.cells:
        if c.check == "match":
            match += 1
        elif c.check == "mismatch":
            mismatch += 1
        elif c.check == "no_text_layer":
            no_layer += 1
        elif c.check == "not_checked" and numbers_in(c.text):
            unchecked += 1  # 尚未校验 / 无法校验的数字格
    numeric = match + mismatch + no_layer + unchecked
    verdict: TableVerdict
    if tier == "L2":
        verdict = "dual_agree"
    elif mismatch > 0:
        verdict = "text_layer_cells_mismatch"
    elif no_layer > 0:
        verdict = "scan_single_unverified"
    elif tier == "L1" and numeric > 0 and unchecked == numeric:
        verdict = "text_layer_page_closed"
    elif numeric > 0 and unchecked == numeric:
        verdict = "not_checked"
    else:
        verdict = "text_layer_cells_ok"
    return GridVerifySummary(numeric, match, mismatch, no_layer + unchecked, verdict)
