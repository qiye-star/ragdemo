"""L3：需要 TextIn 的页按连续段切成子 PDF 分别送 TextIn（段内跨页表仍由 TextIn 合并），
块与表格网格的页码映射回原文档。全部页都要 L3 时直接送原 PDF（与旧路径同一缓存键）并保留原样结果。
TextIn 的异常原样抛出，由 prepare_documents 按既有规则处理。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from ragdemo.adapters.announcements import NormalizedBlock
from ragdemo.parse.textin import DocumentParser, ParseResult
from ragdemo.parse.tiered.pdfpages import sub_pdf
from ragdemo.parse.tiered.types import TextInCall
from ragdemo.tables.grid import GridError, remap_grid_pages


def contiguous_runs(pages: Sequence[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for p in sorted(set(pages)):
        if runs and p == runs[-1][-1] + 1:
            runs[-1].append(p)
        else:
            runs.append([p])
    return runs


@dataclass(frozen=True)
class TextInPages:
    blocks_by_page: Mapping[int, tuple[NormalizedBlock, ...]]
    calls: tuple[TextInCall, ...]
    warnings: tuple[str, ...]
    whole_document: ParseResult | None  # 全部页 L3 时的原样结果（已标 source=textin）

    @property
    def billed_pages(self) -> int:
        return sum(c.billed_pages for c in self.calls)


def _billed(r: ParseResult, sent: int) -> int:
    return 0 if r.from_cache else (r.page_count or sent)


def _remap(b: NormalizedBlock, run: Sequence[int], warnings: list[str]) -> NormalizedBlock:
    if b.page is None:
        original = run[0]
        warnings.append(f"TextIn 块缺页码，归入段首页 {original}")
    elif not 1 <= b.page <= len(run):
        raise ValueError(f"TextIn 块页码 {b.page} 超出子文档 1..{len(run)}")
    else:
        original = run[b.page - 1]
    table = b.table
    if table is not None:
        try:
            table = replace(
                table, grid=remap_grid_pages(table.grid, {i + 1: p for i, p in enumerate(run)})
            )
        except GridError:
            warnings.append(f"第 {original} 页表格网格页码无法映射，网格丢弃")
            table = None
    return replace(b, page=original, table=table, source="textin")


def _boundary_warnings(r: ParseResult, run: Sequence[int], total_pages: int) -> list[str]:
    """段首页首块 / 段尾页末块是表格，且该边之外还有原文页：这张表可能跨过段边界，
    被拆成两段各自解析的两张表（不会静默合并）。只告警，由调用方 / 台账决定是否整份重送。"""
    out: list[str] = []
    first = [b for b in r.blocks if b.page == 1]
    last = [b for b in r.blocks if b.page == len(run)]
    if run[0] > 1 and first and first[0].block_type == "table":
        out.append(f"第 {run[0]} 页段首即为表格，可能是跨过段边界的续表（不会与前一页的表合并）")
    if run[-1] < total_pages and last and last[-1].block_type == "table":
        out.append(f"第 {run[-1]} 页段尾为表格，可能延续到段外的下一页（不会与其合并）")
    return out


def _group(blocks: Sequence[NormalizedBlock]) -> dict[int, tuple[NormalizedBlock, ...]]:
    by_page: dict[int, list[NormalizedBlock]] = {}
    for b in blocks:
        if b.page is not None:
            by_page.setdefault(b.page, []).append(b)
    return {p: tuple(bs) for p, bs in by_page.items()}


def parse_pages_with_textin(
    textin: DocumentParser, pdf_bytes: bytes, pages: Sequence[int], *, total_pages: int
) -> TextInPages:
    wanted = sorted(set(pages))
    if not wanted:
        return TextInPages({}, (), (), None)
    if wanted == list(range(1, total_pages + 1)):
        r = textin.parse(pdf_bytes, owner_user=None)
        marked = [replace(b, source="textin") for b in r.blocks]
        whole = replace(r, blocks=marked)
        call = TextInCall(
            tuple(wanted), r.json_ref, r.md_ref, r.engine_version, _billed(r, len(wanted))
        )
        return TextInPages(_group(marked), (call,), tuple(r.warnings), whole)
    remapped: list[NormalizedBlock] = []
    calls: list[TextInCall] = []
    warnings: list[str] = []
    for run in contiguous_runs(wanted):
        r = textin.parse(sub_pdf(pdf_bytes, run), owner_user=None)
        calls.append(
            TextInCall(tuple(run), r.json_ref, r.md_ref, r.engine_version, _billed(r, len(run)))
        )
        warnings.extend(r.warnings)
        warnings.extend(_boundary_warnings(r, run, total_pages))
        remapped.extend(_remap(b, run, warnings) for b in r.blocks)
    return TextInPages(_group(remapped), tuple(calls), tuple(warnings), None)
