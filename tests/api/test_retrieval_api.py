"""GET /api/retrieval/search 集成测试：真实数据库 + app_diag 登录用户 +
真实语料（`tests/corpus.py` 的共享 fixture，含真实 Mock 嵌入向量）+
TestClient。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from ragdemo.api.queries import retrieval as retrieval_queries
from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.mock import MockEmbedder
from ragdemo.retrieval.service import RetrievalService

QUERY = "云端训练芯片"


@pytest.fixture(autouse=True)
def _clear_retrieval_cache() -> Iterator[None]:
    """模块级 LRU 缓存跨测试用例持续存在——每个测试都该从空缓存开始，
    否则一个测试的结果可能悄悄喂给另一个用了相同 (query, as_of, ...) 的
    测试，看起来像是缓存生效了，其实只是没清干净。"""
    retrieval_queries.clear_cache()
    yield
    retrieval_queries.clear_cache()


_PRIVATE_DOC_ID = 9001  # tests/corpus.py 用 OVERRIDING SYSTEM VALUE 写了 doc_id 1-3，
# 那不会推进 core.document 的 identity 序列——普通 INSERT 拿到的下一个自增值
# 仍然是 1，会撞上 corpus 已经写好的行。这里同样显式指定一个远离 1-3 的 doc_id。


def _seed_private_block(admin_dsn: str, *, content: str) -> int:
    """插一份 owner_user 私有的文档 + 一个内容为 `content` 的叶子块，并让它
    也被嵌入——检索的两路召回都有机会命中它，RLS 必须在两路都拦住它。
    """
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(
            "INSERT INTO core.document (doc_id, doc_type, title, publish_at, source,"
            " content_hash, version_group_id, owner_user, valid_from, known_at,"
            " ingest_run_id)"
            " OVERRIDING SYSTEM VALUE"
            " VALUES (%s, 'quarterly', '私有测试文档', '2024-01-01T00:00:00+08', 'mock',"
            "         'h-private-retrieval', %s, 'u1', '2024-01-01',"
            "         '2024-01-01T00:00:00+08', 'r1')",
            (_PRIVATE_DOC_ID, _PRIVATE_DOC_ID),
        )
        doc_id = _PRIVATE_DOC_ID
        conn.execute(
            "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal, page,"
            " content, is_leaf, owner_user, doc_type, publish_at, valid_from, known_at,"
            " source, ingest_run_id)"
            " VALUES (%s, 'paragraph', '', 0, 1, %s, true, 'u1', 'quarterly',"
            "         '2024-01-01T00:00:00+08', '2024-01-01', '2024-01-01T00:00:00+08',"
            "         'mock', 'r1')",
            (doc_id, content),
        )
        conn.commit()
        embed_pending_blocks(conn, MockEmbedder())
        conn.commit()
    return doc_id


@pytest.mark.db
def test_missing_as_of_returns_422(client: TestClient, corpus: psycopg.Connection) -> None:
    r = client.get("/api/retrieval/search", params={"q": QUERY})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "as_of_required"


@pytest.mark.db
def test_search_returns_ranked_rows_across_all_stages(
    client: TestClient, corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    r = client.get("/api/retrieval/search", params={"as_of": as_of_2024.isoformat(), "q": QUERY})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == QUERY
    assert body["rows"], "语料里真实存在匹配这个查询的块，行不该是空的"
    # 每一行至少要在某一个阶段里出现过，不能是全 null 的空壳行。
    for row in body["rows"]:
        assert any(row[f"{stage}_rank"] is not None for stage in ("bm25", "vec", "fused", "rerank"))


@pytest.mark.db
def test_private_block_never_appears_in_any_stage_column(
    client: TestClient, corpus: psycopg.Connection, as_of_2024: datetime, api_db: tuple[str, str]
) -> None:
    admin_dsn, _ = api_db
    # 查询词特意复用 QUERY（语料里公共块也含它，见 tests/corpus.py），让私有
    # 块在 BM25/向量两路都有真实机会成为候选，不是查一个谁都测不到的怪词
    # 侥幸过关。真正只属于私有内容、绝不该出现在响应任何地方的是这个
    # leak_marker——它和搜索词是两个独立的字符串，不会像"搜索词本身"那样
    # 被正常回显在响应的 query 字段里，因此出现即为真泄漏。
    leak_marker = "THISPRIVATECONTENTMUSTNEVERLEAK"
    private_doc_id = _seed_private_block(
        admin_dsn, content=f"私有内容 {QUERY} {leak_marker} 测试文本"
    )

    r = client.get(
        "/api/retrieval/search",
        params={"as_of": as_of_2024.isoformat(), "q": QUERY, "candidate_k": 200},
    )
    assert r.status_code == 200
    body = r.json()
    assert all(row["doc_id"] != private_doc_id for row in body["rows"])
    assert leak_marker not in r.text


@pytest.mark.db
def test_stage_ranks_are_internally_consistent_with_stats(
    client: TestClient, corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    r = client.get("/api/retrieval/search", params={"as_of": as_of_2024.isoformat(), "q": QUERY})
    body = r.json()
    stats = body["stats"]
    bm25_ranked = [row for row in body["rows"] if row["bm25_rank"] is not None]
    vec_ranked = [row for row in body["rows"] if row["vec_rank"] is not None]
    fused_ranked = [row for row in body["rows"] if row["fused_rank"] is not None]
    assert len(bm25_ranked) <= stats["bm25_hits"]
    assert len(vec_ranked) <= stats["vec_hits"]
    assert len(fused_ranked) == stats["after_fusion"]
    # 每个阶段内部排名唯一、从 1 开始连续。
    for stage in ("bm25_rank", "vec_rank", "fused_rank", "rerank_rank"):
        ranks = sorted(row[stage] for row in body["rows"] if row[stage] is not None)
        assert ranks == list(range(1, len(ranks) + 1))


@pytest.mark.db
def test_vector_path_is_not_meaningful_on_mock_embedded_corpus(
    client: TestClient, corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """语料是 tests/corpus.py 用 MockEmbedder 嵌入的——诚实标注这一点，
    不能让向量列的排名看起来像是真的（web-diagnostic-ui 计划裁决 5）。
    """
    r = client.get("/api/retrieval/search", params={"as_of": as_of_2024.isoformat(), "q": QUERY})
    body = r.json()
    assert body["models"]["embedder_is_mock"] is True
    assert body["models"]["vector_path_is_meaningful"] is False
    assert "mock-1024" in body["models"]["corpus_embedding_versions"]


@pytest.mark.db
def test_repeated_request_is_served_from_cache(
    client: TestClient,
    corpus: psycopg.Connection,
    as_of_2024: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}
    original_search = RetrievalService.search

    def counting_search(self: RetrievalService, *args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return original_search(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(RetrievalService, "search", counting_search)

    params = {"as_of": as_of_2024.isoformat(), "q": QUERY}
    first = client.get("/api/retrieval/search", params=params)
    second = client.get("/api/retrieval/search", params=params)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    assert calls["n"] == 1, "第二次请求应该直接命中缓存，不该再跑一遍检索"


@pytest.mark.db
def test_block_without_embedding_has_bm25_rank_but_no_vec_rank(
    client: TestClient, corpus: psycopg.Connection, as_of_2024: datetime, api_db: tuple[str, str]
) -> None:
    """corpus fixture 在 seed 完之后就跑过一轮 embed_pending_blocks——这里
    在那之后再插一个新块，让它保持"待嵌入"状态，制造一个真实存在的
    embedding IS NULL 场景，而不是伪造一行数据。
    """
    admin_dsn, _ = api_db
    marker = "从未嵌入的新块标记词"
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(
            "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal, page,"
            " content, is_leaf, doc_type, publish_at, valid_from, known_at, source,"
            " ingest_run_id)"
            " SELECT doc_id, 'paragraph', '', 99, 1, %s, true, doc_type, publish_at,"
            "        valid_from, known_at, 'mock', 'r1'"
            "   FROM core.document WHERE doc_id = 1",
            (f"未嵌入内容 {marker}",),
        )
        conn.commit()

    r = client.get(
        "/api/retrieval/search",
        params={"as_of": as_of_2024.isoformat(), "q": marker, "candidate_k": 200},
    )
    assert r.status_code == 200
    body = r.json()
    matching = [row for row in body["rows"] if marker in row["preview"]]
    assert matching, "新插入的块应该能被 BM25 命中"
    assert matching[0]["bm25_rank"] is not None
    assert matching[0]["vec_rank"] is None
