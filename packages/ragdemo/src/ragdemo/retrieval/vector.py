"""向量一路：Chroma 出候选，PostgreSQL 的 asof 视图定生死（ADR-0009）。

Chroma 不参与 PG 的事务，`known_at` / `superseded_at` 因此不能由它独立判定——
一次更正入库与同步之间必然有窗口，让 Chroma 自己说了算会把「PG 里已撤回的块」
泄漏进某个历史时点的检索结果，这正是 CLAUDE.md §1.1 最高优先级约束要防的那类
错误。本模块因此只把 `VectorIndex.query()`（不论底下是 Chroma 还是
`index=None` 时默认的 `PgVectorIndex`）当候选生成器：返回的候选一律要回穿
`asof.doc_block` 视图 + `build_filters()` 的全部谓词再确认一遍，只有通过这道
权威过滤的块才会出现在最终结果里。Chroma 的 `where` 预过滤只是为了少传数据，
不是正确性来源。

两个已知的坑（别再踩）：
1. 查 `asof` 视图前必须用 `as_of_session` 包住，否则视图里的
   `asof.current_as_of()` 因为 GUC 没设而抛 `InvalidParameterValue`
   （见 `_authoritative_filter`）。
2. pgvector 的 `<=>` 运算符优先级低于 `*`，需要显式加括号才能安全做
   `距离 * -1` 之类的变换。本模块不写任何 `<=>` SQL——距离计算全部委托给
   `VectorIndex.query()`（`vector_index.py` 里已经处理过这个坑），
   这里只做候选生成 → 权威过滤 → 迭代过采样三件事，结构上不会触发它。

迭代过采样：权威过滤会因时点/owner/entity/doc_type 淘汰一批候选，直接取
`candidate_k` 个往往过滤后不够，等价于 pgvector 0.8 iterative index scan
要解决的同一个问题。命中不足、且索引里可能还有更多数据时（即上一轮请求 k
个、索引确实凑够了 k 个候选），按 `RetrievalConfig.oversample_growth` 倍数扩大再查一次；
索引确实没有更多数据了（返回数少于请求数）就用现有结果收尾，不会因为过滤后
凑不够而死循环。
"""

from __future__ import annotations

from collections.abc import Sequence

import psycopg

from ragdemo.embed.base import EMBEDDING_DIM
from ragdemo.retrieval.filters import build_filters
from ragdemo.retrieval.lexical import RankedHit
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest
from ragdemo.retrieval.vector_index import PgVectorIndex, VectorCandidate, VectorIndex
from ragdemo_core.db.session import as_of_session


def apply_scan_settings(conn: psycopg.Connection, cfg: RetrievalConfig) -> None:
    """设置 pgvector 0.8 的迭代扫描参数。

    只对走 `PgVectorIndex` 的候选生成有效——用 `set_config(..., true)`，
    即 `SET LOCAL` 语义，必须在事务内调用，且要与实际发起向量查询的语句共享
    同一个事务（或其保存点）才会生效，调用方需要自己把两者包进同一个
    `conn.transaction()`。走 Chroma 时这些 GUC 对结果没有任何影响，是纯粹的
    空操作，但调用方不应该关心底下是哪个实现，所以这个函数照样保留、照样能调用。
    """
    conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(cfg.ef_search),))
    conn.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
    conn.execute("SELECT set_config('hnsw.max_scan_tuples', %s, true)", (str(cfg.max_scan_tuples),))


def _authoritative_filter(
    conn: psycopg.Connection, req: RetrievalRequest, candidates: Sequence[VectorCandidate]
) -> list[VectorCandidate]:
    """回穿 `asof.doc_block` 视图做权威过滤，保留 candidates 原有的距离顺序。

    Chroma（或 `PgVectorIndex` 自己的预过滤）给出的候选只是「大概率相关」，
    时点 / owner / entity / doc_type 的最终判定必须由这里的查询做——这是
    ADR-0009「Chroma 出候选，PostgreSQL 定生死」的落地点，不能省。
    """
    if not candidates:
        return []
    filters, params = build_filters(req)
    params["block_ids"] = [c.block_id for c in candidates]
    sql = f"SELECT block_id FROM asof.doc_block WHERE block_id = ANY(%(block_ids)s) {filters}"
    with as_of_session(conn, req.as_of, tenant=req.tenant, user=req.user):
        rows = conn.execute(sql, params).fetchall()
    valid_ids = {int(r[0]) for r in rows}
    return [c for c in candidates if c.block_id in valid_ids]


def vector_search(
    conn: psycopg.Connection,
    req: RetrievalRequest,
    query_vector: Sequence[float],
    *,
    index: VectorIndex | None = None,
) -> list[RankedHit]:
    """向量一路：候选生成（Chroma 或 pgvector）+ PG 权威过滤 + 迭代过采样。

    `index` 省略时退回 `PgVectorIndex(conn)`——保证不传索引也能跑，同时它是
    Task 10 双跑一致性核对的对照组（ADR-0009 后果 4）。
    """
    if len(query_vector) != EMBEDDING_DIM:
        raise ValueError(f"查询向量维度应为 {EMBEDDING_DIM}，收到 {len(query_vector)}")

    idx = index if index is not None else PgVectorIndex(conn)
    candidate_k = req.config.candidate_k

    multiplier = 1
    filtered: list[VectorCandidate] = []
    while True:
        k = candidate_k * multiplier
        candidates = idx.query(
            query_vector,
            k=k,
            as_of=req.as_of,
            entity_ids=req.entity_ids,
            doc_types=req.doc_types,
        )
        filtered = _authoritative_filter(conn, req, candidates)
        exhausted = len(candidates) < k
        if len(filtered) >= candidate_k or exhausted:
            break
        multiplier *= req.config.oversample_growth

    top = filtered[:candidate_k]
    return [RankedHit(c.block_id, rank, 1.0 - c.distance) for rank, c in enumerate(top, start=1)]
