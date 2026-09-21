"""过滤条件：时点谓词必须出现在 SQL 里（冗余谓词，06 §3.4）。"""
from __future__ import annotations

from datetime import UTC, datetime

from ragdemo.retrieval.filters import build_filters
from ragdemo.retrieval.types import RetrievalRequest

AS_OF = datetime(2024, 12, 31, tzinfo=UTC)


def test_time_point_predicates_are_always_present() -> None:
    """走 asof 视图还写一遍，是为了让谓词能下推进索引（06 §3.4）。"""
    sql, params = build_filters(RetrievalRequest(query="算力", as_of=AS_OF))
    assert "known_at <= %(as_of)s" in sql
    assert "superseded_at IS NULL OR superseded_at > %(as_of)s" in sql
    assert params["as_of"] == AS_OF


def test_is_leaf_is_always_filtered() -> None:
    sql, _ = build_filters(RetrievalRequest(query="算力", as_of=AS_OF))
    assert "is_leaf" in sql


def test_owner_predicate_is_null_safe() -> None:
    """公共文档的 owner 是 NULL，用裸 = 会把它们全部过滤掉。

    NULL 安全的写法有两种，这里**必须**用展开的 OR 形式而不是
    `IS NOT DISTINCT FROM`：后者与 `paradedb.score()` 同时出现时规划器直接拒绝
    （`Unsupported query shape`），见
    tests/db/test_retrieval_sql.py::test_docs_owner_predicate_is_rejected_by_paradedb。
    """
    sql, params = build_filters(RetrievalRequest(query="算力", as_of=AS_OF))
    assert "IS NOT DISTINCT FROM" not in sql
    for column in ("owner_tenant", "owner_user"):
        assert f"{column} IS NULL" in sql, f"{column} 的 NULL 分支丢了"
    assert params["tenant"] is None
    assert params["user"] is None


def test_entity_filter_is_optional_and_parameterised() -> None:
    sql, params = build_filters(
        RetrievalRequest(query="算力", as_of=AS_OF, entity_ids=["CN.688256"])
    )
    assert "entity_id = ANY(%(entity_ids)s)" in sql
    assert params["entity_ids"] == ["CN.688256"]


def test_no_entity_filter_means_no_entity_clause() -> None:
    sql, params = build_filters(RetrievalRequest(query="算力", as_of=AS_OF))
    assert "entity_id = ANY" not in sql
    assert "entity_ids" not in params


def test_doc_type_and_published_after_are_supported() -> None:
    sql, params = build_filters(
        RetrievalRequest(
            query="算力", as_of=AS_OF, doc_types=["quarterly"],
            published_after=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    assert "doc_type = ANY(%(doc_types)s)" in sql
    assert "publish_at >= %(published_after)s" in sql
    assert params["doc_types"] == ["quarterly"]
