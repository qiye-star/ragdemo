"""逐页本地解析：无文字层 → 退回；有表格 → 按坐标还原；整理 → 闭合校验。

实验阶段只产出逐页 markdown 与状态，不构造 NormalizedBlock、不接入入库链路。"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ragdemo.parse.confidence import MIN_CHARS_PER_PAGE
from ragdemo.parse.local.char_closure import char_violations
from ragdemo.parse.local.decor import REASON_ROTATED, StrippedPage, strip_decor
from ragdemo.parse.local.layout import PageLayout, extract_pages, unsafe_orientation_reason
from ragdemo.parse.local.number_closure import closure_violations, ordered_numbers
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


def orientation_handback(stripped: StrippedPage) -> str | None:
    """旋转 / 倒向文字的页必须退回：pdfplumber 把这类文字的字序倒过来（"12.5" → "5.21"），
    而自检基准（文字层）同样是倒的，闭合校验看不出来。

    判据（任一）：剥离后仍有非直立词；页面带 /Rotate；有字符文本矩阵从右向左；
    被剥离的旋转文字里含数字（只含字母的水印可以安全剥离）。"""
    reason = unsafe_orientation_reason(stripped.layout)
    if reason is not None:
        return reason
    for why, text in stripped.items:
        if why == REASON_ROTATED and any(ch.isdigit() for ch in text):
            return "被剥离的旋转文字含数字，字序不可信"
    return None


def parse_pages_local(pdf_bytes: bytes, structurer: PageStructurer) -> list[PageResult]:
    pages = extract_pages(pdf_bytes)
    results: list[PageResult] = []
    for original, stripped in zip(pages, strip_decor(pages), strict=True):
        layout = stripped.layout  # 剥离装饰后的正文版面
        stripped_text = stripped.stripped_text
        handback = needs_handback(layout.text)
        if handback is None and "(cid:" in original.text:  # 剥离前的文字层也要查乱码
            handback = "文字层乱码（cid）"
        if handback is None:
            # 本路径没有"字序不可信"专用状态，沿用会退回上层的 no_text_layer，原因里写明
            handback = orientation_handback(stripped)
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


def order_violation(markdown: str, original_text: str, stripped_text: str) -> str | None:
    """数字顺序检查，基准是**未剥离、未重聚类**的原始 pdfplumber extract_text。

    剥离装饰后的 layout.text 是用 cluster_rows 重建的，与排版器同一套行聚类，拿它当基准
    等于只复查渲染步骤。这里用原始序列：输出的数字序列必须是它的子序列，且余下的恰好
    等于被剥离文字里的数字（多重集合）。不满足返回原因（可能是误报，宁可退回）。"""
    orig = ordered_numbers(original_text)
    out = ordered_numbers(markdown)
    it = iter(orig)
    if not all(any(tok == o for o in it) for tok in out):
        return "数字顺序与原始文字层不一致（输出数字不是原始序列的子序列：符号错位或行序被打乱）"
    if Counter(orig) - Counter(out) != Counter(ordered_numbers(stripped_text)):
        return "输出数字与原始文字层相比，余下的数字不等于被剥离文字里的数字"
    return None


def _self_check(
    markdown: str,
    source: str,
    tables: Sequence[str],
    original_text: str,
    stripped_text: str,
) -> tuple[list[str], str]:
    """确定性排版的自检：输出与文字层双向做数字闭合与字符闭合，表格须原样出现。
    返回 (输出里新增的数字, 失败原因)；通过则原因为空。排版器没有生成环节，
    任何不通过都说明排版器有 bug，所以不重试，直接记 closure_failed。"""
    flat = re.sub(r"\s+", "", markdown)
    bad_numbers = closure_violations(markdown, source)
    lost_numbers = closure_violations(source, markdown)
    bad_chars = char_violations(markdown, source)
    lost_chars = char_violations(source, markdown)
    missing = [i for i, t in enumerate(tables, start=1) if re.sub(r"\s+", "", t) not in flat]
    # 顺序检查：多重集合看不出符号错位、数字对调；基准是原始 extract_text，其他检查失败也照做
    order_reason = order_violation(markdown, original_text, stripped_text)
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
    if order_reason is not None:
        reasons.append(order_reason)
    return bad_numbers, "；".join(reasons)


self_check = _self_check  # L0 第二版与测试使用的公开名


def parse_pages_deterministic(pdf_bytes: bytes) -> list[PageResult]:
    """确定性排版版本的逐页本地解析：不调用任何模型，输出字符全部来自文字层。"""
    return parse_layouts_deterministic(extract_pages(pdf_bytes))


def parse_layouts_deterministic(pages: Sequence[PageLayout]) -> list[PageResult]:
    """同 parse_pages_deterministic，输入是已提取的版面（实验运行器要复用版面做指标）。"""
    stripped_pages = strip_decor(pages)
    handbacks: dict[int, str] = {}
    unsupported: dict[int, str] = {}
    for i, (original, stripped) in enumerate(zip(pages, stripped_pages, strict=True)):
        reason = needs_handback(stripped.layout.text)
        if reason is None and "(cid:" in original.text:
            reason = "文字层乱码（cid）"
        if reason is not None:
            handbacks[i] = reason
        elif (orient := orientation_handback(stripped)) is not None:
            unsupported[i] = orient
    live = [i for i in range(len(pages)) if i not in handbacks and i not in unsupported]
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
        if i in unsupported:
            results.append(
                PageResult(
                    layout.page,
                    "layout_unsupported",
                    "",
                    layout.text,
                    [],
                    unsupported[i],
                    stripped_text,
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
        violations, reason = _self_check(
            res.markdown, layout.text, res.tables, pages[i].text, stripped_text
        )
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
