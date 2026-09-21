"""BM25 一路（ParadeDB pg_search）。

entity_id / doc_type / is_leaf / known_at 都在 BM25 索引里且标了 fast，
ParadeDB 能把这些谓词下推进 tantivy，过滤在打分之前完成——这是时点过滤后
仍能保证召回的关键（docs/06-retrieval.md §3.2）。
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ragdemo.retrieval.filters import build_filters
from ragdemo.retrieval.types import RetrievalRequest
from ragdemo_core.db.session import as_of_session

_SQL = """
SELECT block_id,
       ROW_NUMBER() OVER (ORDER BY paradedb.score(block_id) DESC) AS rnk,
       paradedb.score(block_id) AS raw_score
  FROM asof.doc_block
 WHERE content @@@ %(query)s
 {filters}
 ORDER BY paradedb.score(block_id) DESC
 LIMIT %(candidate_k)s
"""


@dataclass(frozen=True)
class RankedHit:
    block_id: int
    rank: int
    raw_score: float


def _statement(req: RetrievalRequest) -> tuple[str, dict[str, object]]:
    # owner 谓词的 ParadeDB 兼容形式由 build_filters() 直接产出，这里不做任何改写。
    # 曾经在这里用字符串替换把 `IS NOT DISTINCT FROM` 换成 OR 形式，但那样
    # filters.py 一改空格或参数名，替换就会静默失效——而失效的表现是
    # `Unsupported query shape` 或更糟的错结果，不是显式报错。
    filters, params = build_filters(req)
    params["query"] = req.query
    params["candidate_k"] = req.config.candidate_k
    return _SQL.format(filters=filters), params


def bm25_search(conn: psycopg.Connection, req: RetrievalRequest) -> list[RankedHit]:
    sql, params = _statement(req)
    with as_of_session(conn, req.as_of, tenant=req.tenant, user=req.user):
        rows = conn.execute(sql, params).fetchall()
    return [RankedHit(int(r[0]), int(r[1]), float(r[2])) for r in rows]


def explain_bm25(conn: psycopg.Connection, req: RetrievalRequest) -> str:
    """返回执行计划文本。用于验证过滤是否下推进索引扫描节点。"""
    sql, params = _statement(req)
    with as_of_session(conn, req.as_of, tenant=req.tenant, user=req.user):
        rows = conn.execute(f"EXPLAIN (ANALYZE, VERBOSE) {sql}", params).fetchall()
    return "\n".join(str(r[0]) for r in rows)
