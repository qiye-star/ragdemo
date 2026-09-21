"""元数据校验 8 项。任一项不过，整份文档回滚——部分入库比没入库更危险。"""
from __future__ import annotations

from datetime import UTC, datetime

from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument
from ragdemo.parse.chunker import Chunk
from ragdemo.parse.validate import validate_chunks


def _doc(page_count: int = 24) -> NormalizedDocument:
    return NormalizedDocument(
        provider_doc_id="D1", entity_ref="688256.SH", doc_type="quarterly",
        title="三季报", period="2024Q3",
        publish_at=datetime(2024, 10, 28, 18, 32, tzinfo=UTC),
        language="zh", source_url=None, raw_bytes_ref=None, content_hash="h",
        is_correction=False, supersedes_provider_doc_id=None, page_count=page_count,
        blocks=[NormalizedBlock(0, "paragraph", "第一节", "内容")],
    )


def _chunk(**kw: object) -> Chunk:
    base = dict(
        ordinal=0, block_type="paragraph", section_path="第一节", content="内容",
        page=1, bbox=None, is_leaf=True, parent_ordinal=None,
    )
    base.update(kw)
    return Chunk(**base)  # type: ignore[arg-type]


def test_valid_chunks_pass() -> None:
    chunks = [
        _chunk(ordinal=0, is_leaf=False),
        _chunk(ordinal=1, is_leaf=True, parent_ordinal=0),
    ]
    assert validate_chunks(_doc(), chunks) == []


def test_non_contiguous_ordinals_are_caught() -> None:
    chunks = [_chunk(ordinal=0, is_leaf=False), _chunk(ordinal=2, parent_ordinal=0)]
    assert any("ordinal" in v for v in validate_chunks(_doc(), chunks))


def test_empty_content_is_caught() -> None:
    assert any("content" in v for v in validate_chunks(_doc(), [_chunk(content="   ")]))


def test_table_without_description_is_caught() -> None:
    """表格块没有 content_desc 就无法被语义命中。"""
    chunks = [_chunk(block_type="table", content="| a |")]
    assert any("content_desc" in v for v in validate_chunks(_doc(), chunks, descriptions={}))


def test_parent_pointing_outside_the_document_is_caught() -> None:
    assert any("parent" in v for v in validate_chunks(_doc(), [_chunk(parent_ordinal=99)]))


def test_nesting_deeper_than_two_levels_is_caught() -> None:
    """只允许两层。父块自己有父块说明构造出错了。"""
    chunks = [
        _chunk(ordinal=0, is_leaf=False, parent_ordinal=None),
        _chunk(ordinal=1, is_leaf=False, parent_ordinal=0),
        _chunk(ordinal=2, is_leaf=True, parent_ordinal=1),
    ]
    assert any("两层" in v for v in validate_chunks(_doc(), chunks))


def test_page_out_of_range_is_caught() -> None:
    assert any("page" in v for v in validate_chunks(_doc(page_count=5), [_chunk(page=99)]))


def test_leaf_with_children_is_caught() -> None:
    """is_leaf = true 的块不能有子块。"""
    chunks = [_chunk(ordinal=0, is_leaf=True), _chunk(ordinal=1, parent_ordinal=0)]
    assert any("is_leaf" in v for v in validate_chunks(_doc(), chunks))


def test_non_leaf_without_children_is_caught() -> None:
    chunks = [_chunk(ordinal=0, is_leaf=False)]
    assert any("is_leaf" in v for v in validate_chunks(_doc(), chunks))


def test_all_violations_are_reported_not_just_the_first() -> None:
    """一次看到全部问题，避免修一个跑一次。"""
    chunks = [_chunk(ordinal=3, content="  ", page=99)]
    assert len(validate_chunks(_doc(page_count=5), chunks)) >= 3
