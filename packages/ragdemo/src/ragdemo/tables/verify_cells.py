"""逐单元格文字层校验（表格处理改进计划 T7）。

每个含数字的单元格，其数字多重集必须与该格 bbox 内 PDF 文字层词里的数字多重集**相等**
（不是子集：框内多出的数字——脚注号、相邻格、过大的框——一律 mismatch）。
校验是单页的：不做 ±1 页放宽，相邻页偏移记为 mismatch（虚警可接受，假通过不可接受）。
框内有游离的 '-' / '%' / 括号词而格文本里没有，也记 mismatch。
扫描页（无文字层）、旋转页、无坐标的格各自是独立状态，永远不记为 match。

表级判定见 `summarize_grid`。`tier` 参数必须由调用方在 `verify_dual`（两路表格逐行逐格含表头
完全相等）通过后才传 "L2"，不要把裸层级字符串当作证据；即便如此，只要有文字层逐格
证据（match / mismatch / no_text_layer）就不授予 dual_agree。

已接受的已知局限（不在此处修）：分词器把 "2025-5" 里的 "-5" 当作数字；格的页码错而另一页
同位置恰有同一数字时会 match；单位 / 量级不比较（由 T6 单位绑定负责）；
summarize_grid 不能重新校验，信任格上已存的 check（但 no_numbers 会按文本重算）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.parse.local.number_closure import fold_text, numbers_in
from ragdemo.parse.tiered.types import Tier
from ragdemo.tables.grid import BBox, CellCheck, GridCell, TableGrid

VERIFY_CELLS_VERSION: Final = "cellverify:v2"
BBOX_PAD_PT: Final = 0.0  # b3 探针：>0 时相邻格的词会被框进来；语料上 0 / 0.5 / 1.5 结果相同
_SIGN_ONLY: Final = re.compile(r"[-%()\s]+")

TableVerdict = Literal[
    "text_layer_cells_ok",
    "text_layer_cells_partial",
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
    words = words_in_bbox(pw, cell.bbox)
    cell_folded = fold_text(cell.text)
    for w in words:  # 游离符号词：格文本里没有就不能算同一个数
        wf = fold_text(w.text)
        if _SIGN_ONLY.fullmatch(wf) and any(
            ch not in cell_folded for ch in set(wf) if not ch.isspace()
        ):
            return "mismatch"
    have = numbers_in(" ".join(w.text for w in words))
    return "match" if have == need else "mismatch"


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
    """表级结论。数字格一律按 numbers_in(text) 重算（过期的 no_numbers 视为未校验）。

    顺序（写死）：mismatch -> L2 且无文字层逐格证据（全部数字格 not_checked，含零数字格）
    -> dual_agree；无文字层 -> scan_single_unverified；全部数字格 match -> ok；
    无数字格 -> not_checked（没有校验过任何东西，不是 ok）；数字格全部未校验 -> L1 为
    text_layer_page_closed、否则 not_checked；其余（部分 match、部分未校验）-> partial。
    """
    match = mismatch = no_layer = unchecked = 0
    for c in grid.cells:
        if not numbers_in(c.text):
            continue
        if c.check == "match":
            match += 1
        elif c.check == "mismatch":
            mismatch += 1
        elif c.check == "no_text_layer":
            no_layer += 1
        else:  # not_checked，或文本有数字但存的是 no_numbers（过期）
            unchecked += 1
    numeric = match + mismatch + no_layer + unchecked
    verdict: TableVerdict
    if mismatch > 0:
        verdict = "text_layer_cells_mismatch"
    elif tier == "L2" and unchecked == numeric:
        verdict = "dual_agree"
    elif no_layer > 0:
        verdict = "scan_single_unverified"
    elif numeric == 0:
        verdict = "not_checked"
    elif match == numeric:
        verdict = "text_layer_cells_ok"
    elif match == 0 and tier == "L1":
        verdict = "text_layer_page_closed"
    elif match == 0:
        verdict = "not_checked"
    else:
        verdict = "text_layer_cells_partial"
    return GridVerifySummary(numeric, match, mismatch, no_layer + unchecked, verdict)
