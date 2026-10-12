"""逐页本地解析：无文字层 → 退回；有表格 → 按坐标还原；整理 → 闭合校验。

实验阶段只产出逐页 markdown 与状态，不构造 NormalizedBlock、不接入入库链路。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ragdemo.parse.confidence import MIN_CHARS_PER_PAGE
from ragdemo.parse.local.char_closure import char_violations
from ragdemo.parse.local.decor import strip_decor
from ragdemo.parse.local.layout import PageLayout, extract_pages
from ragdemo.parse.local.number_closure import closure_violations
from ragdemo.parse.local.structure import ClosureFailed, StructureFailed
from ragdemo.parse.local.tables import (
    cluster_rows,
    column_edges,
    find_table_regions,
    grid_to_markdown,
    rows_to_grid,
)
from ragdemo.parse.local.typeset import typeset_document

Status = Literal["ok", "no_text_layer", "closure_failed", "structure_failed", "layout_unsupported"]


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


def _self_check(markdown: str, source: str, tables: Sequence[str]) -> tuple[list[str], str]:
    """确定性排版的自检：输出与文字层双向做数字闭合与字符闭合，表格须原样出现。
    返回 (输出里新增的数字, 失败原因)；通过则原因为空。排版器没有生成环节，
    任何不通过都说明排版器有 bug，所以不重试，直接记 closure_failed。"""
    flat = re.sub(r"\s+", "", markdown)
    bad_numbers = closure_violations(markdown, source)
    lost_numbers = closure_violations(source, markdown)
    bad_chars = char_violations(markdown, source)
    lost_chars = char_violations(source, markdown)
    missing = [i for i, t in enumerate(tables, start=1) if re.sub(r"\s+", "", t) not in flat]
    reasons: list[str] = []
    if bad_numbers:
        reasons.append(f"排版后新增了数字：{bad_numbers}")
    if lost_numbers:
        reasons.append(f"排版后丢失了数字：{lost_numbers}")
    if bad_chars:
        reasons.append(f"字符闭合失败：新增{''.join(bad_chars[:20])}")
    if lost_chars:
        reasons.append(f"字符闭合失败：丢失{''.join(lost_chars[:20])}")
    if missing:
        reasons.append(f"表格未原样保留：第 {missing} 张")
    return bad_numbers, "；".join(reasons)


def parse_pages_deterministic(pdf_bytes: bytes) -> list[PageResult]:
    """确定性排版版本的逐页本地解析：不调用任何模型，输出字符全部来自文字层。"""
    return parse_layouts_deterministic(extract_pages(pdf_bytes))


def parse_layouts_deterministic(pages: Sequence[PageLayout]) -> list[PageResult]:
    """同 parse_pages_deterministic，输入是已提取的版面（实验运行器要复用版面做指标）。"""
    stripped_pages = strip_decor(pages)
    handbacks: dict[int, str] = {}
    for i, (original, stripped) in enumerate(zip(pages, stripped_pages, strict=True)):
        reason = needs_handback(stripped.layout.text)
        if reason is None and "(cid:" in original.text:
            reason = "文字层乱码（cid）"
        if reason is not None:
            handbacks[i] = reason
    live = [i for i in range(len(pages)) if i not in handbacks]
    typeset = dict(
        zip(live, typeset_document([stripped_pages[i].layout for i in live]), strict=True)
    )

    results: list[PageResult] = []
    for i, stripped in enumerate(stripped_pages):
        layout = stripped.layout
        stripped_text = stripped.stripped_text
        if i in handbacks:
            results.append(
                PageResult(
                    layout.page, "no_text_layer", "", layout.text, [], handbacks[i], stripped_text
                )
            )
            continue
        res = typeset[i]
        if res.status == "layout_unsupported":
            results.append(
                PageResult(
                    layout.page,
                    "layout_unsupported",
                    "",
                    layout.text,
                    [],
                    res.reason,
                    stripped_text,
                )
            )
            continue
        violations, reason = _self_check(res.markdown, layout.text, res.tables)
        if reason:
            results.append(
                PageResult(
                    layout.page,
                    "closure_failed",
                    "",
                    layout.text,
                    violations,
                    reason,
                    stripped_text,
                )
            )
            continue
        results.append(
            PageResult(layout.page, "ok", res.markdown, layout.text, [], "", stripped_text)
        )
    return results
