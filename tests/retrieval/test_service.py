"""端到端检索：五个阶段串起来，统计齐全，时点正确。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.retrieval.rerank import FailingReranker, MockReranker, Reranker
from ragdemo.retrieval.rewrite import expand_synonyms
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest
from ragdemo.retrieval.vector_index import (
    PgVectorIndex,
    VectorCandidate,
    VectorIndex,
    VectorItem,
)


def _service(conn: psycopg.Connection, reranker: Reranker | None = None) -> RetrievalService:
    return RetrievalService(conn, MockEmbedder(), reranker or MockReranker())


class _SpyIndex:
    """记录自己有没有被调用过的 VectorIndex。只用来钉住"服务真的用了这个索引"。"""

    def __init__(self, inner: VectorIndex) -> None:
        self._inner = inner
        self.queries = 0

    def upsert(self, items: Sequence[VectorItem]) -> None:
        self._inner.upsert(items)

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int,
        as_of: datetime | None = None,
        entity_ids: Sequence[str] | None = None,
        doc_types: Sequence[str] | None = None,
    ) -> list[VectorCandidate]:
        self.queries += 1
        return self._inner.query(
            vector, k=k, as_of=as_of, entity_ids=entity_ids, doc_types=doc_types
        )

    def delete(self, block_ids: Sequence[int]) -> None:
        self._inner.delete(block_ids)

    def count(self) -> int:
        return self._inner.count()

    def existing_ids(self, block_ids: Sequence[int]) -> set[int]:
        return self._inner.existing_ids(block_ids)


@pytest.mark.db
def test_service_actually_uses_the_injected_index(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """传进来的向量索引必须真的被用上。

    回归测试：`RetrievalService.search()` 一度根本不把 index 传给 `vector_search`，
    于是不管构造时给了什么索引，向量一路都静默退回 pgvector——ADR-0009 整条
    Chroma 链路接好了却没接上，而所有测试照样绿。没有这条断言就发现不了。
    """
    spy = _SpyIndex(PgVectorIndex(corpus))
    service = RetrievalService(corpus, MockEmbedder(), MockReranker(), index=spy)

    service.search(RetrievalRequest(query="云端训练芯片", as_of=as_of_2024))

    assert spy.queries > 0, "服务没有使用注入的向量索引"


@pytest.mark.db
def test_end_to_end_returns_evidence(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus).search(RetrievalRequest(query="云端训练芯片 收入", as_of=as_of_2024))
    assert result.blocks
    assert all(b.block_id > 0 for b in result.blocks)
    assert all(b.known_at <= as_of_2024 for b in result.blocks)


@pytest.mark.db
def test_stats_are_populated_for_every_stage(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """stats 是排查「为什么没找到」的唯一依据，每次查询都要记（06 §8）。"""
    result = _service(corpus).search(RetrievalRequest(query="收入", as_of=as_of_2024))
    s = result.stats
    assert s.bm25_hits >= 0 and s.vec_hits > 0
    assert s.after_fusion > 0
    assert s.ms_total > 0
    assert s.after_rerank == len(result.blocks) or s.after_rerank >= len(result.blocks)


@pytest.mark.db
def test_trace_defaults_to_none(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    """trace 是纯加法——不传这个参数时，行为必须和加它之前完全一致。"""
    result = _service(corpus).search(RetrievalRequest(query="收入", as_of=as_of_2024))
    assert result.trace is None


@pytest.mark.db
def test_trace_ranks_agree_with_the_non_traced_result(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """trace=True 不该改变检索本身的行为，只是把已经算出来的中间结果带出来——
    两次调用（一次要 trace 一次不要）最终返回的证据顺序必须一致，且
    trace.final_order 必须就是 outcome.order 本身，与 result.blocks 的
    block_id 顺序吻合（expand_to_evidence 不改变 order 的相对顺序，只做
    父子块展开与去重）。
    """
    req = RetrievalRequest(query="收入", as_of=as_of_2024)
    svc = _service(corpus)
    plain = svc.search(req)
    traced = svc.search(req, trace=True)

    assert [b.block_id for b in plain.blocks] == [b.block_id for b in traced.blocks]
    assert traced.trace is not None
    assert traced.trace.embedder_model == "mock-1024"
    assert traced.trace.reranker_model == "mock-reranker"
    assert traced.trace.rerank_attempted is True

    # 每个阶段的候选列表按 rank 升序排列，从 1 开始连续编号。
    for stage in (traced.trace.bm25, traced.trace.vec, traced.trace.fused, traced.trace.rerank):
        assert [s.rank for s in stage] == list(range(1, len(stage) + 1))

    # final_order 就是重排/截断后的最终顺序，rerank 阶段的候选集合恰好是
    # 这个顺序的来源——两者的 block_id 集合必须完全一致。
    assert traced.trace.final_order == [s.block_id for s in traced.trace.rerank]


@pytest.mark.db
def test_trace_marks_rerank_not_attempted_when_disabled(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    result = _service(corpus).search(
        RetrievalRequest(
            query="收入", as_of=as_of_2024, config=RetrievalConfig(rerank_enabled=False)
        ),
        trace=True,
    )
    assert result.trace is not None
    assert result.trace.rerank_attempted is False


@pytest.mark.db
def test_future_document_never_appears(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus).search(RetrievalRequest(query="采购合同 8.5 亿元", as_of=as_of_2024))
    assert all(b.doc_id != 2 for b in result.blocks)


@pytest.mark.db
def test_same_query_same_as_of_is_reproducible(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """可复现是引擎的核心承诺（00-overview §6 第 1 条）。"""
    svc = _service(corpus)
    req = RetrievalRequest(query="云端训练芯片", as_of=as_of_2024)
    first = [b.block_id for b in svc.search(req).blocks]
    second = [b.block_id for b in svc.search(req).blocks]
    assert first == second


@pytest.mark.db
def test_rerank_failure_degrades_but_still_returns(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    result = _service(corpus, FailingReranker()).search(
        RetrievalRequest(query="收入", as_of=as_of_2024)
    )
    assert result.blocks
    assert result.stats.degraded is True
    assert all(b.reranked is False for b in result.blocks)


@pytest.mark.db
def test_rerank_can_be_disabled(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus, FailingReranker()).search(
        RetrievalRequest(
            query="收入", as_of=as_of_2024, config=RetrievalConfig(rerank_enabled=False)
        )
    )
    assert result.blocks
    assert result.stats.ms_rerank == 0.0


@pytest.mark.db
def test_top_k_is_respected(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus).search(
        RetrievalRequest(query="收入", as_of=as_of_2024, config=RetrievalConfig(top_k=1))
    )
    assert len(result.blocks) <= 1


def test_synonym_expansion_adds_variants() -> None:
    table = {"算力": ["计算力", "compute"]}
    assert set(expand_synonyms("算力需求", table)) == {"算力需求", "计算力需求", "compute需求"}


def test_synonym_expansion_is_noop_without_matches() -> None:
    assert expand_synonyms("寒武纪业绩", {"算力": ["计算力"]}) == ["寒武纪业绩"]


@pytest.mark.db
def test_rewrite_is_off_by_default(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    """默认关闭是有意的：改写增加延迟与成本，收益必须由评测证明（06 §7）。"""
    assert RetrievalConfig().rewrite_enabled is False
    result = _service(corpus).search(RetrievalRequest(query="算力", as_of=as_of_2024))
    assert result.stats.ms_total > 0


class _SpyReranker:
    """记录重排收到的文档文本，不改变顺序语义（沿用 MockReranker 打分）。"""

    model = "spy-reranker"

    def __init__(self) -> None:
        self.docs: list[str] = []
        self._inner = MockReranker()

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        self.docs = list(docs)
        return self._inner.rerank(query, docs, top_k)


def test_rerank_rich_input_defaults_on() -> None:
    assert RetrievalConfig().rerank_rich_input is True


@pytest.mark.db
def test_rerank_gets_bare_content_when_rich_input_off(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    spy = _SpyReranker()
    _service(corpus, spy).search(
        RetrievalRequest(
            query="智能计算 收入",
            as_of=as_of_2024,
            config=RetrievalConfig(rerank_rich_input=False),
        )
    )
    assert spy.docs
    assert not any("寒武纪 2024 年三季报" in d for d in spy.docs)


@pytest.mark.db
def test_rerank_rich_input_matches_embedding_input(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """开启后重排看到的文本必须和嵌入输入同一个拼法：标题 + 章节路径 + 描述 + 正文。"""
    from ragdemo.embed.base import embedding_input

    corpus.execute(
        "UPDATE core.doc_block SET content_desc = '各业务分部收入同比表' WHERE block_type = 'table'"
    )
    corpus.commit()
    spy = _SpyReranker()
    _service(corpus, spy).search(
        RetrievalRequest(
            query="智能计算 收入",
            as_of=as_of_2024,
            config=RetrievalConfig(rerank_rich_input=True),
        )
    )
    rows = corpus.execute(
        "SELECT d.title, b.section_path, b.content_desc, b.content "
        "FROM core.doc_block b JOIN core.document d USING (doc_id) WHERE b.is_leaf"
    ).fetchall()
    expected = {
        embedding_input(
            doc_title=str(r[0] or ""),
            section_path=str(r[1] or ""),
            content_desc=str(r[2] or ""),
            content=str(r[3]),
        )
        for r in rows
    }
    assert spy.docs
    assert set(spy.docs) <= expected
    assert any(
        d.startswith("寒武纪 2024 年三季报\n第三节 主营业务\n各业务分部收入同比表\n")
        for d in spy.docs
    )


def test_rerank_rich_max_chars_defaults_to_none() -> None:
    assert RetrievalConfig().rerank_rich_max_chars is None


def test_rerank_rich_max_chars_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RetrievalConfig(rerank_rich_max_chars=0).validate()


@pytest.mark.db
def test_rerank_rich_max_chars_truncates_rerank_input_only(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    spy = _SpyReranker()
    _service(corpus, spy).search(
        RetrievalRequest(
            query="智能计算 收入",
            as_of=as_of_2024,
            config=RetrievalConfig(rerank_rich_input=True, rerank_rich_max_chars=20),
        )
    )
    assert spy.docs
    assert all(len(d) <= 20 for d in spy.docs)
    assert any(len(d) == 20 for d in spy.docs)


@pytest.mark.db
def test_rerank_rich_max_chars_none_keeps_embedding_cap(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    spy = _SpyReranker()
    _service(corpus, spy).search(
        RetrievalRequest(
            query="智能计算 收入",
            as_of=as_of_2024,
            config=RetrievalConfig(rerank_rich_input=True),
        )
    )
    assert spy.docs
    assert any(len(d) > 20 for d in spy.docs)
