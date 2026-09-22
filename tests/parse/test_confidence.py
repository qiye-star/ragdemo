"""解析置信度评分：四项加权，权重与依据见 parse/confidence.py 模块 docstring。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument
from ragdemo.parse.confidence import score_document, score_pages

# U+FFFD（替换字符）是编码错误的典型产物：不是中日韩字符、不是 ASCII、
# 不在允许的标点集合里，用来模拟"乱码"内容。
GARBLED = "���" * 10


def _doc(blocks: list[NormalizedBlock], *, page_count: int | None) -> NormalizedDocument:
    return NormalizedDocument(
        provider_doc_id="P1",
        entity_ref="688256.SH",
        doc_type="annual_report",
        title="t",
        period="2024",
        publish_at=datetime(2024, 10, 28, 18, 32, tzinfo=UTC),
        language="zh",
        source_url=None,
        raw_bytes_ref=None,
        content_hash="h1",
        is_correction=False,
        supersedes_provider_doc_id=None,
        page_count=page_count,
        blocks=blocks,
    )


def _para(ordinal: int, content: str, *, page: int | None = 1) -> NormalizedBlock:
    return NormalizedBlock(
        ordinal=ordinal, block_type="paragraph", section_path="第一节", content=content, page=page
    )


def _table(ordinal: int, markdown: str, *, page: int | None = 1) -> NormalizedBlock:
    return NormalizedBlock(
        ordinal=ordinal, block_type="table", section_path="第一节", content=markdown, page=page
    )


def test_clean_document_scores_near_one() -> None:
    blocks = [_para(0, "正常的中文段落内容，字数足够覆盖一页的健康阈值。" * 3, page=1)]
    doc = _doc(blocks, page_count=1)
    assert score_document(doc) > 0.9


def test_missing_page_lowers_the_score() -> None:
    """page_count=2 但只有第 1 页有块，第 2 页完全缺失。"""
    healthy = _doc([_para(0, "第一页正常内容" * 10, page=1)], page_count=1)
    with_gap = _doc([_para(0, "第一页正常内容" * 10, page=1)], page_count=2)
    assert score_document(with_gap) < score_document(healthy)


def test_document_level_missing_page_ratio_is_proportional_not_binary() -> None:
    """回归测试：真实管线跑出来的场景复现过的一个 bug——`page_count=24` 但
    内容只出现在 3 页时，`score_document()` 曾经把"缺页"算成二元的（只要
    `doc.blocks` 非空就当作 0 缺页），21/24 页完全没有产出任何块这件事被
    完全吞掉，最终分只由覆盖率一项承担，两个独立的失效模式被混在一起、
    互相掩盖。这里用同样的三块内容、只是 `page_count` 报得更接近真实覆盖
    范围来对照：唯一显著变化的应该是缺页比例（0 vs 21/24）。"""
    sparse = _doc(
        [
            _para(0, "内容充足" * 20, page=12),
            _para(1, "内容充足" * 20, page=13),
            _para(2, "内容充足" * 20, page=14),
        ],
        page_count=24,
    )
    dense = _doc(
        [
            _para(0, "内容充足" * 20, page=1),
            _para(1, "内容充足" * 20, page=2),
            _para(2, "内容充足" * 20, page=3),
        ],
        page_count=3,
    )
    assert score_document(sparse) < score_document(dense)


def test_garbled_content_lowers_the_score() -> None:
    clean = _doc([_para(0, "正常的中文内容" * 10, page=1)], page_count=1)
    garbled = _doc([_para(0, GARBLED, page=1)], page_count=1)
    assert score_document(garbled) < score_document(clean)


def test_unclosed_table_lowers_the_score() -> None:
    closed = _table(0, "| 甲 | 乙 |\n|---|---|\n| 1 | 2 |", page=1)
    unclosed = _table(0, "| 甲 | 乙 |\n|---|---|\n| 1 |  |", page=1)
    doc_closed = _doc([closed], page_count=1)
    doc_unclosed = _doc([unclosed], page_count=1)
    assert score_document(doc_unclosed) < score_document(doc_closed)


def test_no_tables_does_not_penalise_closure() -> None:
    """没有表格时闭合率这一项不适用，不该拖累总分——用一段干净正文对照。"""
    doc = _doc([_para(0, "没有表格的干净文档" * 10, page=1)], page_count=1)
    assert score_document(doc) > 0.9


def test_unknown_page_count_does_not_penalise_coverage_or_missing_page() -> None:
    """路径 A（供应商结构化接口）不一定带 page_count；不该无中生有一个缺页/覆盖率的 0 分。"""
    doc = _doc([_para(0, "内容")], page_count=None)
    assert score_document(doc) == pytest.approx(1.0, abs=1e-6)


def test_score_pages_is_empty_without_page_count() -> None:
    doc = _doc([_para(0, "内容", page=1)], page_count=None)
    assert score_pages(doc) == {}


def test_score_pages_distinguishes_a_bad_page_from_a_clean_one() -> None:
    """同一份文档，第 1 页干净、第 2 页乱码——per-page 打分必须能区分两者，
    这正是块级置信度（"块用自己的 page 去查"）存在的意义。"""
    doc = _doc(
        [
            _para(0, "第一页是干净的正常中文内容" * 10, page=1),
            _para(1, GARBLED, page=2),
        ],
        page_count=2,
    )
    pages = score_pages(doc)
    assert set(pages) == {1, 2}
    assert pages[1] > pages[2]


def test_score_pages_flags_a_page_with_zero_blocks() -> None:
    doc = _doc([_para(0, "只有第一页" * 10, page=1)], page_count=3)
    pages = score_pages(doc)
    assert set(pages) == {1, 2, 3}
    assert pages[2] < pages[1]
    assert pages[3] < pages[1]


def test_score_is_clamped_to_zero_one() -> None:
    doc = _doc([], page_count=1)
    score = score_document(doc)
    assert 0.0 <= score <= 1.0
