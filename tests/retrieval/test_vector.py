"""向量一路：Chroma 候选生成、PG 权威过滤、迭代过采样（ADR-0009）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import chromadb
import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest
from ragdemo.retrieval.vector import apply_scan_settings, vector_search
from ragdemo.retrieval.vector_index import ChromaVectorIndex, VectorItem


def _qvec(text: str) -> list[float]:
    return MockEmbedder().embed([text])[0]


def _isolated_chroma_index() -> ChromaVectorIndex:
    """每个测试一个独立 owner——EphemeralClient 在同一进程内是共享后端
    （`tests/retrieval/test_vector_index.py` 的 `chroma_index` fixture已经
    踩过这个坑），固定走公共 collection 会在测试之间、乃至与其他测试文件之间
    串数据。这里只是借 owner_user 换一个物理隔离的 collection 做测试隔离，
    与 `RetrievalRequest.user`（PG 侧权威过滤的 owner 语义）无关。
    """
    return ChromaVectorIndex(chromadb.EphemeralClient(), owner_user=f"test-{uuid.uuid4().hex}")


@pytest.mark.db
def test_returns_ranked_hits(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    hits = vector_search(
        corpus, RetrievalRequest(query="云端训练芯片", as_of=as_of_2024), _qvec("云端训练芯片")
    )
    assert hits
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))


@pytest.mark.db
def test_future_documents_are_invisible(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    early = vector_search(
        corpus, RetrievalRequest(query="采购合同", as_of=as_of_2024), _qvec("采购合同")
    )
    later = vector_search(
        corpus,
        RetrievalRequest(query="采购合同", as_of=datetime(2025, 6, 1, tzinfo=UTC)),
        _qvec("采购合同"),
    )
    assert len(later) > len(early)


@pytest.mark.db
def test_scan_settings_are_applied_to_the_session(corpus: psycopg.Connection) -> None:
    """pgvector 0.8 的迭代扫描：过滤后候选不足时继续扫索引（06 §3.3）。

    只对走 PgVectorIndex 的候选生成有意义；Chroma 完全不看这些 GUC，
    但函数本身在两条路径下都要能被安全调用（见 vector.py 的模块 docstring）。
    """
    with corpus.transaction():
        apply_scan_settings(corpus, RetrievalConfig(ef_search=321))
        ef = corpus.execute("SHOW hnsw.ef_search").fetchone()
        mode = corpus.execute("SHOW hnsw.iterative_scan").fetchone()
    assert ef is not None and ef[0] == "321"
    assert mode is not None and mode[0] == "relaxed_order"


@pytest.mark.db
def test_blocks_without_embedding_are_skipped(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """父块没有 embedding，不能出现在向量召回里。"""
    hits = vector_search(
        corpus, RetrievalRequest(query="主营业务", as_of=as_of_2024), _qvec("主营业务")
    )
    if hits:
        (all_leaf,) = corpus.execute(
            "SELECT bool_and(is_leaf AND embedding IS NOT NULL) FROM core.doc_block"
            " WHERE block_id = ANY(%s)",
            ([h.block_id for h in hits],),
        ).fetchone()  # type: ignore[misc]
        assert all_leaf is True


@pytest.mark.db
def test_wrong_dimension_query_vector_raises(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    with pytest.raises(ValueError, match="维度"):
        vector_search(corpus, RetrievalRequest(query="x", as_of=as_of_2024), [0.1, 0.2])


@pytest.mark.db
def test_pg_authority_overrides_stale_chroma_candidate(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """ADR-0009 的核心保证：Chroma 若声称一个块在 as_of_2024 可见（同步延迟或
    脏数据造成的谎报），但 PG 里该块真实的 known_at 晚于 as_of_2024（doc 2 是
    2025-03 的公告，as_of_2024 本不可见），`vector_search` 必须依然把它拦下——
    权威判定永远在 PG，Chroma 的元数据只是预过滤，不是真相来源。
    """
    row = corpus.execute(
        "SELECT block_id, entity_id, doc_type, publish_at FROM core.doc_block"
        " WHERE doc_id = 2 AND is_leaf LIMIT 1"
    ).fetchone()
    assert row is not None
    leaked_block_id, entity_id, doc_type, publish_at = row

    chroma_index = _isolated_chroma_index()
    qvec = _qvec("采购合同")
    # Chroma 元数据「撒谎」：known_at 写成 as_of_2024 之前，制造与 PG 真实状态
    # 不一致的窗口——这正是 ADR-0009 里「Chroma 不参与 PG 事务」那个风险场景。
    chroma_index.upsert(
        [
            VectorItem(
                block_id=int(leaked_block_id),
                embedding=qvec,
                doc_id=2,
                entity_id=entity_id,
                doc_type=doc_type,
                known_at=datetime(2024, 1, 1, tzinfo=UTC),
                superseded_at=None,
                publish_at=publish_at,
            )
        ]
    )

    hits = vector_search(
        corpus,
        RetrievalRequest(query="采购合同", as_of=as_of_2024),
        qvec,
        index=chroma_index,
    )
    assert all(h.block_id != int(leaked_block_id) for h in hits)


@pytest.mark.db
def test_iterative_oversample_recovers_from_heavy_filtering(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """Chroma 混入大量在 PG 里查无此块的幻影候选（模拟同步偏移/脏数据），
    权威过滤会把它们全部滤掉；命中不足 candidate_k 时必须加大过采样倍数重新
    取数，而不是直接把塌陷的结果集（甚至空结果）返回给上层。
    """
    real_rows = corpus.execute(
        "SELECT block_id FROM core.doc_block"
        " WHERE is_leaf AND embedding IS NOT NULL AND doc_id IN (1, 3)"
    ).fetchall()
    real_ids = {int(r[0]) for r in real_rows}
    assert len(real_ids) == 4  # corpus fixture：doc1 三个叶子块 + doc3 一个叶子块

    chroma_index = _isolated_chroma_index()
    qvec = _qvec("主营业务")
    known_at = datetime(2024, 1, 1, tzinfo=UTC)

    # 幻影候选：embedding 与查询向量完全一致（距离恒为 0），保证排在真实候选
    # 之前；block_id 在 900000 以上，PG 里查无此块，权威过滤会把它们全滤掉。
    phantom_items = [
        VectorItem(
            block_id=900_000 + i,
            embedding=qvec,
            doc_id=1,
            entity_id="CN.688256",
            doc_type="quarterly",
            known_at=known_at,
            superseded_at=None,
            publish_at=known_at,
        )
        for i in range(30)
    ]
    # 真实候选：embedding 换一个不同的内容哈希出来的向量，距离必然大于 0，
    # 排在幻影候选之后——第一轮（k = candidate_k）只会取到幻影候选。
    real_vec = _qvec("研发费用")
    real_items = [
        VectorItem(
            block_id=block_id,
            embedding=real_vec,
            doc_id=1,
            entity_id="CN.688256",
            doc_type="quarterly",
            known_at=known_at,
            superseded_at=None,
            publish_at=known_at,
        )
        for block_id in real_ids
    ]
    chroma_index.upsert(phantom_items + real_items)

    candidate_k = 3
    req = RetrievalRequest(
        query="主营业务",
        as_of=as_of_2024,
        config=RetrievalConfig(candidate_k=candidate_k, fusion_k=candidate_k, top_k=candidate_k),
    )
    hits = vector_search(corpus, req, qvec, index=chroma_index)

    assert len(hits) == candidate_k
    assert all(h.block_id in real_ids for h in hits)
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
