"""逐页本地解析：无文字层 → 退回；有表格 → 按坐标还原；整理 → 闭合校验。

实验阶段只产出逐页 markdown 与状态，不构造 NormalizedBlock、不接入入库链路。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ragdemo.parse.confidence import MIN_CHARS_PER_PAGE
from ragdemo.parse.local.layout import PageLayout, extract_pages
from ragdemo.parse.local.structure import ClosureFailed, StructureFailed
from ragdemo.parse.local.tables import (
    cluster_rows,
    column_edges,
    find_table_regions,
    grid_to_markdown,
    rows_to_grid,
)

Status = Literal["ok", "no_text_layer", "closure_failed", "structure_failed"]


class PageStructurer(Protocol):
    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str: ...


@dataclass(frozen=True)
class PageResult:
    page: int
    status: Status
    markdown: str
    source_text: str
    violations: list[str] = field(default_factory=list)


def _table_markdowns(layout: PageLayout) -> list[str]:
    """先圈出表格区域，再只对区域算列并还原成 markdown。
    （实验阶段的简化：区域内按一张规整网格处理；跨页表、合并单元格、嵌套表头留给 Phase B。）"""
    rows = cluster_rows(layout.words)
    out: list[str] = []
    for region in find_table_regions(rows):
        out.append(grid_to_markdown(rows_to_grid(region, column_edges(region))))
    return out


def parse_pages_local(pdf_bytes: bytes, structurer: PageStructurer) -> list[PageResult]:
    results: list[PageResult] = []
    for layout in extract_pages(pdf_bytes):
        if len(layout.text.strip()) < MIN_CHARS_PER_PAGE and not layout.words:
            results.append(PageResult(layout.page, "no_text_layer", "", layout.text))
            continue
        tables = _table_markdowns(layout)
        try:
            markdown = structurer.structure(layout.text, tables)
        except ClosureFailed as exc:
            results.append(
                PageResult(layout.page, "closure_failed", "", layout.text, exc.violations)
            )
            continue
        except StructureFailed:  # 截断 / 空回复 / 畸形响应 / 丢失数字：这一页不可信
            results.append(PageResult(layout.page, "structure_failed", "", layout.text))
            continue
        results.append(PageResult(layout.page, "ok", markdown, layout.text))
    return results
