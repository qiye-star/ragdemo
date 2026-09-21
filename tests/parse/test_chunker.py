"""切块器：纯函数，覆盖长度、重叠、表格保护、章节边界、样板过滤。"""

from __future__ import annotations

from datetime import UTC, datetime

from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument
from ragdemo.parse.chunker import _absorb_short_tail, chunk_document, split_text
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


# --- 尾片段吸收：短于 leaf_min_chars 的尾片段要并回前一片段（如果并得下）---
#
# `split_text` 的定长滑窗一旦真的产出一个短尾片段，它的前一片段必然是被
# 硬切到 leaf_max_chars 满格的（否则前一片段自己就会一路吃到文本末尾，
# 压根不会再多出一个尾片段）——这是算法本身的结构性质，不是某个具体输入
# 的巧合。所以合并后的长度必然是 leaf_max_chars 加上尾片段的新增内容，
# 严格大于 leaf_max_chars，"合并会超预算" 分支在 split_text 的真实输出上
# 永远成立（下面第一个测试用的就是这种真实输出）。"合并不超预算" 分支要
# 靠人工拼的 spans 直接测 _absorb_short_tail 本身——不是在绕开
# split_text，是这个分支在 split_text 的自然产出里根本走不到，只能单测
# 合并逻辑自己的正确性。


def test_short_tail_from_hard_wrap_is_left_alone_when_absorbing_would_exceed_max() -> None:
    """无标点的长串按定长滑窗硬切：n=401 时前一片段吃满 400 字（leaf_max），
    尾片段只剩 49 字（leaf_min=200 以下），其中 48 字是与前一片段重叠的
    内容——近乎重复的一条记录。把它并回前一片段会得到 401 字，超过
    leaf_max_chars(400)，按规则必须保留原状：不能为了消灭短尾片段而
    打破 leaf_max_chars 的上限承诺。"""
    text = "甲" * 401
    parts = split_text(text, CFG)
    assert len(parts) == 2
    assert len(parts[0]) == CFG.leaf_max_chars
    assert len(parts[1]) == 49
    assert parts[0][-CFG.overlap_chars :] == parts[1][: CFG.overlap_chars]


def test_absorb_short_tail_merges_when_it_fits_within_max() -> None:
    """人工构造 spans：尾片段 58 字 < leaf_min(200)，合并后 260 字 <=
    leaf_max(400)——应该合并成一个片段。"""
    spans = [(0, 250), (202, 260)]
    assert _absorb_short_tail(spans, CFG) == [(0, 260)]


def test_absorb_short_tail_leaves_it_when_merge_would_exceed_max() -> None:
    """split_text 在 n=401 上真实产出的 spans 形状：合并后 401 字 >
    leaf_max(400)，不合并。"""
    spans = [(0, 400), (352, 401)]
    assert _absorb_short_tail(spans, CFG) == spans


def test_absorb_short_tail_is_a_noop_when_the_tail_already_meets_leaf_min() -> None:
    spans = [(0, 400), (352, 560)]  # 尾片段 208 字 >= leaf_min(200)
    assert _absorb_short_tail(spans, CFG) == spans


def test_absorb_short_tail_is_a_noop_for_a_single_span() -> None:
    assert _absorb_short_tail([(0, 300)], CFG) == [(0, 300)]
