"""逐页本地解析：无文字层 → 退回；有表格 → 按坐标还原；整理 → 闭合校验。

实验阶段只产出逐页 markdown 与状态，不构造 NormalizedBlock、不接入入库链路。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ragdemo.parse.confidence import MIN_CHARS_PER_PAGE
from ragdemo.parse.local.decor import strip_decor
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
    reason: str = ""  # 非 ok 状态的原因（实验复盘用：区分虚警与真编造）
    stripped_text: str = ""  # 送模型前剥离的装饰文字（页眉页脚/页码/水印），可审计，不进入输出


def _table_markdowns(layout: PageLayout) -> list[str]:
    """先圈出表格区域，再只对区域算列并还原成 markdown。
    （实验阶段的简化：区域内按一张规整网格处理；跨页表、合并单元格、嵌套表头留给 Phase B。）"""
    rows = cluster_rows(layout.words)
    out: list[str] = []
    for region in find_table_regions(rows):
        out.append(grid_to_markdown(rows_to_grid(region, column_edges(region))))
    return out


def needs_handback(text: str) -> str | None:
    """文字层不可用则返回原因，否则 None。"""
    # 文字层不足 MIN_CHARS_PER_PAGE 字符（无论有无零星词，如页码/水印）一律退回，防扫描件静默 ok
    if len(text.strip()) < MIN_CHARS_PER_PAGE:
        return f"文字层不足 {MIN_CHARS_PER_PAGE} 字符"
    if "(cid:" in text:  # pdfminer 对无法映射的字形输出 (cid:N)，N 会被当成数字
        return "文字层乱码（cid）"
    return None


_needs_handback = needs_handback  # 旧名保留：既有测试从这里导入


def parse_pages_local(pdf_bytes: bytes, structurer: PageStructurer) -> list[PageResult]:
    pages = extract_pages(pdf_bytes)
    results: list[PageResult] = []
    for original, stripped in zip(pages, strip_decor(pages), strict=True):
        layout = stripped.layout  # 剥离装饰后的正文版面
        stripped_text = stripped.stripped_text
        handback = needs_handback(layout.text)
        if handback is None and "(cid:" in original.text:  # 剥离前的文字层也要查乱码
            handback = "文字层乱码（cid）"
        if handback is not None:
            results.append(
                PageResult(
                    layout.page, "no_text_layer", "", layout.text, [], handback, stripped_text
                )
            )
            continue
        tables = _table_markdowns(layout)
        try:
            markdown = structurer.structure(layout.text, tables)
        except ClosureFailed as exc:
            results.append(
                PageResult(
                    layout.page,
                    "closure_failed",
                    "",
                    layout.text,
                    exc.violations,
                    f"数字闭合失败：{exc.violations}",
                    stripped_text,
                )
            )
            continue
        except StructureFailed as exc:  # 截断 / 空回复 / 畸形响应 / 丢失内容：这一页不可信
            results.append(
                PageResult(
                    layout.page, "structure_failed", "", layout.text, [], str(exc), stripped_text
                )
            )
            continue
        results.append(PageResult(layout.page, "ok", markdown, layout.text, [], "", stripped_text))
    return results
