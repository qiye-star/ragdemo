"""过滤条件 → SQL 片段。

时点谓词即使走 asof 视图也要再写一遍：视图里的 asof.current_as_of() 是
PL/pgSQL 函数调用，很可能阻止优化器把谓词下推进 tantivy 或 HNSW 的过滤
（docs/06-retrieval.md §3.4）。冗余不改变结果集，只影响执行计划。
"""

from __future__ import annotations

from typing import Any

from ragdemo.retrieval.types import RetrievalRequest


def build_filters(req: RetrievalRequest) -> tuple[str, dict[str, Any]]:
    """返回 (WHERE 片段, 参数字典)。片段以 AND 开头，可直接拼在已有条件之后。"""
    clauses = [
        "known_at <= %(as_of)s",
        "(superseded_at IS NULL OR superseded_at > %(as_of)s)",
        "is_leaf",
        # owner 谓词必须是 NULL 安全的：公共文档的 owner 是 NULL，用 `=` 会把它们
        # 全部过滤掉。但**不能**写成 `IS NOT DISTINCT FROM`——它与 `paradedb.score()`
        # 同时出现时规划器直接拒绝（`Unsupported query shape`），
        # 见 tests/db/test_retrieval_sql.py::test_docs_owner_predicate_is_rejected_by_paradedb。
        # 展开成等价的 OR 形式，两条召回路径（BM25 与向量）都能用同一个片段。
        # `::text` 转型不能省：参数为 NULL 时 Postgres 推断不出类型。
        "(owner_tenant = %(tenant)s OR (owner_tenant IS NULL AND %(tenant)s::text IS NULL))",
        "(owner_user = %(user)s OR (owner_user IS NULL AND %(user)s::text IS NULL))",
    ]
    params: dict[str, Any] = {
        "as_of": req.as_of,
        "tenant": req.tenant,
        "user": req.user,
    }

    if req.entity_ids:
        clauses.append("entity_id = ANY(%(entity_ids)s)")
        params["entity_ids"] = list(req.entity_ids)
    if req.doc_types:
        clauses.append("doc_type = ANY(%(doc_types)s)")
        params["doc_types"] = list(req.doc_types)
    if req.published_after is not None:
        clauses.append("publish_at >= %(published_after)s")
        params["published_after"] = req.published_after

    return " AND " + " AND ".join(clauses), params
