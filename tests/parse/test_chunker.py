"""切块器：纯函数，覆盖长度、重叠、表格保护、章节边界、样板过滤。"""

from __future__ import annotations

from datetime import UTC, datetime

from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument
from ragdemo.parse.chunker import chunk_document, split_text
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.filters import is_boilerplate

CFG = ChunkConfig()


def _doc(blocks: list[NormalizedBlock]) -> NormalizedDocument:
    return NormalizedDocument(
        provider_doc_id="D1",
        entity_ref="688256.SH",
        doc_type="quarterly",
        title="三季报",
        period="2024Q3",
        publish_at=datetime(2024, 10, 28, 18, 32, tzinfo=UTC),
        language="zh",
        source_url=None,
        raw_bytes_ref=None,
        content_hash="h",
        is_correction=False,
        supersedes_provider_doc_id=None,
        page_count=1,
        blocks=blocks,
    )


def test_short_paragraph_stays_one_chunk() -> None:
    parts = split_text("报告期内营业收入 12,340 万元。", CFG)
    assert parts == ["报告期内营业收入 12,340 万元。"]


def test_long_paragraph_is_split_within_max() -> None:
    text = "。".join(f"第{i}句内容" * 6 for i in range(40))
    parts = split_text(text, CFG)
    assert len(parts) > 1
    assert all(len(p) <= CFG.leaf_max_chars for p in parts)


def test_consecutive_chunks_overlap() -> None:
    text = "甲" * 1200
    parts = split_text(text, CFG)
    assert len(parts) >= 3
    assert parts[0][-CFG.overlap_chars :] == parts[1][: CFG.overlap_chars]


def test_split_terminates_and_covers_all_text() -> None:
    """重叠切分最容易写出死循环或丢尾巴。"""
    text = "乙" * 997
    parts = split_text(text, CFG)
    assert len(parts) < 20
    assert parts[-1].endswith("乙")


def test_table_block_is_never_split_however_long() -> None:
    long_table = "| 列 | 值 |\n" + "\n".join(f"| 行{i} | {i} |" for i in range(400))
    chunks = chunk_document(
        _doc([NormalizedBlock(0, "table", "第三节 > 分部收入", long_table, page=13)]), CFG
    )
    assert len(chunks) == 1
    assert chunks[0].content == long_table
    assert len(chunks[0].content) > CFG.leaf_max_chars


def test_chunks_do_not_span_sections() -> None:
    """跨小节的块会混入不相关内容，稀释 BM25 与向量信号。"""
    chunks = chunk_document(
        _doc(
            [
                NormalizedBlock(0, "paragraph", "第一节 概况", "丙" * 150),
                NormalizedBlock(1, "paragraph", "第二节 业务", "丁" * 150),
            ]
        ),
        CFG,
    )
    assert len(chunks) == 2
    assert {c.section_path for c in chunks} == {"第一节 概况", "第二节 业务"}


def test_section_path_is_prepended_to_content() -> None:
    """块自带章节上下文，对 BM25 与嵌入都有增益（05 §3.1）。"""
    chunks = chunk_document(
        _doc([NormalizedBlock(0, "paragraph", "第三节 主营业务", "戊" * 100)]), CFG
    )
    assert chunks[0].content.startswith("第三节 主营业务")


def test_ordinals_are_contiguous_from_zero() -> None:
    chunks = chunk_document(
        _doc(
            [
                NormalizedBlock(0, "paragraph", "第一节", "己" * 900),
                NormalizedBlock(1, "table", "第一节", "| a | b |"),
            ]
        ),
        CFG,
    )
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_boilerplate_is_recognised() -> None:
    assert is_boilerplate("第 12 页 共 24 页")
    assert is_boilerplate("目 录")
    assert is_boilerplate("本公司及董事会全体成员保证信息披露内容的真实、准确和完整")
    assert not is_boilerplate("报告期内营业收入 12,340 万元，同比增长 58.2%。")


def test_boilerplate_blocks_are_dropped_when_enabled() -> None:
    chunks = chunk_document(
        _doc(
            [
                NormalizedBlock(0, "paragraph", "", "第 12 页 共 24 页"),
                NormalizedBlock(1, "paragraph", "第一节", "庚" * 100),
            ]
        ),
        CFG,
    )
    assert len(chunks) == 1
    assert "第 12 页" not in chunks[0].content


def test_boilerplate_kept_when_disabled() -> None:
    cfg = ChunkConfig(drop_boilerplate=False)
    chunks = chunk_document(_doc([NormalizedBlock(0, "paragraph", "", "第 12 页 共 24 页")]), cfg)
    assert len(chunks) == 1


def test_overlap_content_is_genuinely_shared_not_a_uniform_char_coincidence() -> None:
    """`甲`*1200 的重叠断言对任何切法都成立（字符全同）。用非均匀文本证明
    重叠区间确实是同一段原文，而不是巧合相等。"""
    text = "".join(f"{i:04d}" for i in range(400))  # 1600 个数字字符，无标点可切
    parts = split_text(text, CFG)
    assert len(parts) >= 2
    overlap_from_first = parts[0][-CFG.overlap_chars :]
    overlap_from_second = parts[1][: CFG.overlap_chars]
    assert overlap_from_first == overlap_from_second
    assert len(set(overlap_from_first)) > 1  # 排除“全同字符”式的退化通过


def test_page_survives_split_into_multiple_leaf_chunks() -> None:
    """CLAUDE.md §0：输出中的数值必须可溯源到 [block_id | page]。切分不能丢页码。"""
    long_text = "。".join(f"第{i}句内容" * 6 for i in range(40))
    chunks = chunk_document(
        _doc([NormalizedBlock(0, "paragraph", "第一节", long_text, page=7)]), CFG
    )
    assert len(chunks) > 1
    assert all(c.page == 7 for c in chunks)
