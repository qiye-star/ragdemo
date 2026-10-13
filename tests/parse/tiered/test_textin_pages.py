# tests/parse/tiered/test_textin_pages.py
import glob
import io
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter

from ragdemo.adapters.announcements import NormalizedBlock
from ragdemo.parse import textin as textin_mod
from ragdemo.parse.textin import ParseResult
from ragdemo.parse.tiered import textin_pages as tp
from ragdemo.parse.tiered.pdfpages import sub_pdf
from ragdemo.parse.tiered.textin_pages import contiguous_runs, parse_pages_with_textin
from ragdemo.parse.tiered.types import TextInCall, call_from_dict, call_to_dict
from ragdemo.tables.grid import GridCell, TableAttachment, TableGrid, remap_grid_pages


def _pdf(n: int) -> bytes:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class FakeTextIn:
    def __init__(
        self,
        *,
        from_cache: bool = False,
        table_on: int | None = None,
        pageless: bool = False,
        bad_page: int | None = None,
    ) -> None:
        self.seen: list[int] = []
        self._from_cache, self._table_on, self._pageless, self._bad = (
            from_cache,
            table_on,
            pageless,
            bad_page,
        )

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        n = len(PdfReader(io.BytesIO(file_bytes)).pages)
        self.seen.append(n)
        blocks = [
            NormalizedBlock(
                i, "paragraph", "", f"子文档第{i + 1}页", page=None if self._pageless else i + 1
            )
            for i in range(n)
        ]
        if self._bad is not None:
            blocks.append(NormalizedBlock(n, "paragraph", "", "越界", page=self._bad))
        if self._table_on is not None and self._table_on <= n:
            grid = TableGrid(
                cells=(GridCell(0, 0, 1, 1, "19.3", page=self._table_on),),
                source_engine="textin:1+abc",
            )
            blocks.append(
                NormalizedBlock(
                    len(blocks),
                    "table",
                    "",
                    "| 19.3 |",
                    page=self._table_on,
                    table=TableAttachment(grid),
                )
            )
        return ParseResult(
            blocks,
            n,
            "textin:1+abc",
            "",
            f"parse/textin/x/{n}.json",
            f"parse/textin/x/{n}.md",
            [],
            self._from_cache,
        )


def test_contiguous_runs() -> None:
    assert contiguous_runs([5, 1, 2, 4, 2]) == [[1, 2], [4, 5]]
    assert contiguous_runs([]) == []


def test_runs_go_as_sub_documents_and_pages_and_grids_map_back() -> None:
    t = FakeTextIn(table_on=1)
    r = parse_pages_with_textin(t, _pdf(5), [1, 2, 4], total_pages=5)
    assert t.seen == [2, 1]
    assert sorted(r.blocks_by_page) == [1, 2, 4]
    t4 = next(b for b in r.blocks_by_page[4] if b.block_type == "table")
    assert t4.page == 4 and t4.table is not None and t4.table.grid.pages == (4,)
    assert {b.source for bs in r.blocks_by_page.values() for b in bs} == {"textin"}
    assert r.calls == (
        TextInCall((1, 2), "parse/textin/x/2.json", "parse/textin/x/2.md", "textin:1+abc", 2),
        TextInCall((4,), "parse/textin/x/1.json", "parse/textin/x/1.md", "textin:1+abc", 1),
    )
    assert r.billed_pages == 3 and r.whole_document is None


def test_all_pages_go_as_the_original_document_unchanged() -> None:
    t = FakeTextIn()
    r = parse_pages_with_textin(t, _pdf(3), [3, 1, 2], total_pages=3)
    assert t.seen == [3] and r.billed_pages == 3
    assert r.whole_document is not None
    assert [b.page for b in r.whole_document.blocks] == [1, 2, 3]
    assert {b.source for b in r.whole_document.blocks} == {"textin"}


def test_cache_hits_are_not_billed_and_no_pages_means_no_calls() -> None:
    r = parse_pages_with_textin(FakeTextIn(from_cache=True), _pdf(3), [2], total_pages=3)
    assert r.billed_pages == 0 and r.calls[0].billed_pages == 0
    t = FakeTextIn()
    empty = parse_pages_with_textin(t, _pdf(3), [], total_pages=3)
    assert (empty.calls, empty.blocks_by_page, t.seen) == ((), {}, [])


def test_pageless_blocks_go_to_the_first_page_of_their_run_with_a_warning() -> None:
    r = parse_pages_with_textin(FakeTextIn(pageless=True), _pdf(4), [3, 4], total_pages=4)
    assert sorted(r.blocks_by_page) == [3] and len(r.blocks_by_page[3]) == 2
    assert any("缺页码" in w for w in r.warnings)


def test_table_at_a_run_edge_warns_that_it_may_continue_across_the_boundary() -> None:
    r = parse_pages_with_textin(FakeTextIn(table_on=2), _pdf(5), [2, 3, 5], total_pages=5)
    assert any("段尾为表格" in w and "第 3 页" in w for w in r.warnings)

    class TableFirst(FakeTextIn):
        def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
            r = super().parse(file_bytes, owner_user=owner_user)
            return replace(r, blocks=sorted(r.blocks, key=lambda b: (b.block_type != "table",)))

    r1 = parse_pages_with_textin(TableFirst(table_on=1), _pdf(5), [2, 3, 5], total_pages=5)
    assert any("段首即为表格" in w for w in r1.warnings)
    whole = parse_pages_with_textin(FakeTextIn(table_on=1), _pdf(3), [1, 2, 3], total_pages=3)
    assert not any("段" in w for w in whole.warnings)


def test_out_of_range_pages_from_textin_are_an_error() -> None:
    with pytest.raises(ValueError, match="超出子文档"):
        parse_pages_with_textin(FakeTextIn(bad_page=5), _pdf(4), [2], total_pages=4)


def test_sub_pdf_bytes_are_deterministic_and_calls_round_trip() -> None:
    pdf = _pdf(4)
    assert sub_pdf(pdf, [2, 3]) == sub_pdf(pdf, [2, 3])
    c = TextInCall((2, 3), "j", None, "textin:1+abc", 0)
    assert call_from_dict(call_to_dict(c)) == c
    with pytest.raises(ValueError):
        TextInCall((3, 2), "j", None, "e", 0)


# ---------------------------------------------------------------------------
# 真实缓存 JSON 的页码 / 网格映射证明（只读，零成本，不调 TextIn）。
# 把 40 份缓存响应当"原文档"：挑一个不被任何表格跨越的页当作被跳过的页，
# 其余页按连续段送入；伪 TextIn 对每段返回"该段块、页码重编为 1..n"的结果
# （与真实 TextIn 对子 PDF 的行为同构），映射回来后必须与原文档逐块一致。
# ---------------------------------------------------------------------------
_CACHE = sorted(
    glob.glob(str(Path(__file__).resolve().parents[3] / "data/blob/parse/textin/*/*.json"))
)


def _orig_blocks(path: str) -> tuple[list[NormalizedBlock], int]:
    payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf8"))
    result = payload["result"]
    detail = result["detail"]
    off = textin_mod._page_offset(detail)
    blocks = textin_mod.blocks_from_detail(
        detail,
        page_dims=textin_mod.page_dims_from_metrics(payload.get("metrics"), off),
        json_ref=path,
    )
    return blocks, int(result["total_page_number"])


def _fingerprint(b: NormalizedBlock) -> tuple[object, ...]:
    cells = (
        () if b.table is None else tuple((c.row, c.col, c.text, c.page) for c in b.table.grid.cells)
    )
    return (b.ordinal, b.block_type, b.content, b.page, b.bbox, cells)


def _fake_sub_pdf(_b: bytes, run: Sequence[int]) -> bytes:
    return ("RUN:" + ",".join(map(str, run))).encode()


class _RealShaped:
    """对子 PDF 返回"该段原块、页码与网格页码重编为 1..n"——与真实 TextIn 对子 PDF 的行为同构。"""

    def __init__(self, blocks: list[NormalizedBlock], ref: str) -> None:
        self._blocks, self._ref = blocks, ref

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        run = [int(x) for x in file_bytes.decode().split(":")[1].split(",")]
        to_sub = {p: i + 1 for i, p in enumerate(run)}
        sub: list[NormalizedBlock] = []
        for b in self._blocks:
            if b.page not in to_sub:
                continue
            t = b.table
            if t is not None:
                t = replace(t, grid=remap_grid_pages(t.grid, to_sub))
            sub.append(replace(b, page=to_sub[b.page], table=t))
        return ParseResult(
            sub, len(run), "textin:t+x", "", f"{self._ref}#{run[0]}", None, [], False
        )


@pytest.mark.skipif(not _CACHE, reason="没有本地 TextIn 缓存")
def test_real_cached_documents_remap_identically_across_a_skipped_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp, "sub_pdf", _fake_sub_pdf)
    checked = tables = 0
    for path in _CACHE:
        blocks, total = _orig_blocks(path)
        table_pages = [b.table.grid.pages for b in blocks if b.table is not None]
        skip = next(
            (
                s
                for s in range(1, total + 1)
                if not any(s in ps or (ps and min(ps) < s < max(ps)) for ps in table_pages)
                and not any(ps and min(ps) <= s <= max(ps) for ps in table_pages)
            ),
            None,
        )
        if skip is None:
            continue
        wanted = [p for p in range(1, total + 1) if p != skip]

        parser = _RealShaped(blocks, path)
        r = parse_pages_with_textin(parser, b"%PDF", wanted, total_pages=total)
        got = sorted(
            (_fingerprint(b) for bs in r.blocks_by_page.values() for b in bs),
            key=lambda x: str(x[0]),
        )
        want = sorted(
            (_fingerprint(b) for b in blocks if b.page != skip and b.page is not None),
            key=lambda x: str(x[0]),
        )
        assert got == want, path
        assert skip not in r.blocks_by_page
        assert r.billed_pages == len(wanted)
        checked += 1
        tables += len(table_pages)
    assert checked >= 20, f"只有 {checked} 份文档能做这个证明"
    assert tables > 0
