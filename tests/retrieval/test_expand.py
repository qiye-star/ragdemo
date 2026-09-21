"""父子块展开：引用用叶子块 id，内容用父块；同父块只返回一次。

检索只查 `asof` 视图，不查 `core` 基表（CLAUDE.md §1.1）；本文件里除
`_leaf_ids` / `_first_block_id` 两个测试夹具助手外（沿用 test_lexical.py 的
既有做法，直接查 core 便于按 doc_id/block_type 定位测试用的 block_id），
其余全部经 `expand_to_evidence` 走 asof 视图。
"""
from __future__ import annotations

from datetime import datetime

import psycopg
import pytest

from ragdemo.retrieval.expand import expand_to_evidence


def _leaf_ids(conn: psycopg.Connection, doc_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT block_id FROM core.doc_block WHERE doc_id = %s AND is_leaf"
        " AND parent_block_id IS NOT NULL ORDER BY ordinal",
        (doc_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _first_block_id(conn: psycopg.Connection, doc_id: int) -> int:
    row = conn.execute(
        "SELECT block_id FROM core.doc_block WHERE doc_id = %s ORDER BY ordinal LIMIT 1",
        (doc_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.db
def test_content_comes_from_the_parent_block(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """生成要完整上下文，所以给父块内容。"""
    leaf = _leaf_ids(corpus, 1)[0]
    evidence = expand_to_evidence(
        corpus, [leaf], {leaf: 1.0}, reranked=True, top_k=10, as_of=as_of_2024
    )
    assert len(evidence) == 1
    assert "研发费用" in evidence[0].content, "父块应包含同小节的其他叶子内容"


@pytest.mark.db
def test_citation_uses_the_leaf_block_id(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """溯源要精确到段落，指到整个小节等于没指。"""
    leaf = _leaf_ids(corpus, 1)[0]
    evidence = expand_to_evidence(
        corpus, [leaf], {leaf: 1.0}, reranked=True, top_k=10, as_of=as_of_2024
    )
    assert evidence[0].block_id == leaf
    assert evidence[0].parent_block_id is not None
    assert evidence[0].parent_block_id != leaf


@pytest.mark.db
def test_same_parent_is_returned_once_with_all_matched_children(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    leaves = _leaf_ids(corpus, 1)
    assert len(leaves) >= 2
    scores = {leaves[0]: 0.9, leaves[1]: 0.5}
    evidence = expand_to_evidence(
        corpus, leaves[:2], scores, reranked=True, top_k=10, as_of=as_of_2024
    )
    assert len(evidence) == 1
    assert set(evidence[0].matched_child_ids) == set(leaves[:2])
    assert evidence[0].score == 0.9, "取命中子块中的最高分"
    assert evidence[0].block_id == leaves[0], "block_id 取最高分的那个子块"


@pytest.mark.db
def test_table_block_uses_its_own_content(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """表格块没有父块，用自身内容。"""
    row = corpus.execute(
        "SELECT block_id FROM core.doc_block WHERE block_type = 'table' LIMIT 1"
    ).fetchone()
    assert row is not None
    table_id = int(row[0])
    evidence = expand_to_evidence(
        corpus, [table_id], {table_id: 1.0}, reranked=True, top_k=10, as_of=as_of_2024
    )
    assert evidence[0].parent_block_id is None
    assert "业务分部" in evidence[0].content


@pytest.mark.db
def test_dedup_does_not_backfill_to_top_k(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """去重后不足 top_k 时不补位——宁可少给也不引入低相关证据（06 §6 规则 4）。"""
    leaves = _leaf_ids(corpus, 1)[:2]
    evidence = expand_to_evidence(
        corpus, leaves, {b: 1.0 for b in leaves}, reranked=True, top_k=10, as_of=as_of_2024
    )
    assert len(evidence) == 1


@pytest.mark.db
def test_reranked_flag_is_propagated(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    leaf = _leaf_ids(corpus, 1)[0]
    evidence = expand_to_evidence(
        corpus, [leaf], {leaf: 1.0}, reranked=False, top_k=10, as_of=as_of_2024
    )
    assert evidence[0].reranked is False


@pytest.mark.db
def test_order_is_preserved(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    table = corpus.execute(
        "SELECT block_id FROM core.doc_block WHERE block_type = 'table' LIMIT 1"
    ).fetchone()
    leaf = _leaf_ids(corpus, 1)[0]
    assert table is not None
    order = [int(table[0]), leaf]
    evidence = expand_to_evidence(
        corpus, order, {order[0]: 0.9, order[1]: 0.8}, reranked=True, top_k=10, as_of=as_of_2024
    )
    assert [e.block_id for e in evidence] == order


@pytest.mark.db
def test_future_block_is_invisible_through_asof_view(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """检索只查 asof 视图：doc 2 的公告要到 2025-03 才 known_at，as_of_2024 时点上
    即便上游误把它的 block_id 传进来，展开这一步也不能把内容偷渡回结果。
    """
    future_block = _first_block_id(corpus, 2)
    evidence = expand_to_evidence(
        corpus, [future_block], {future_block: 1.0}, reranked=True, top_k=10, as_of=as_of_2024
    )
    assert evidence == []
