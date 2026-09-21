"""索引召回率验证（docs/06-retrieval.md §3.6）。

不能假设迭代扫描与谓词下推有效，必须测：把「索引路径」（走 HNSW/Chroma 近邻，
可能是近似的）的结果和「强制精确扫描」（暴力算距离，一定精确）的结果比。
阈值 0.95，低于此说明过滤下推没生效、`oversample_growth` 不够，或索引本身
漏召回（ADR-0009 的推翻条件之一）。

`exact_vector_search` 是分母：关闭 `enable_indexscan` / `enable_indexonlyscan`
逼平面规划器退化成 Seq Scan，对每一行都算一次精确距离。

`index_recall` 比较的是**向量一路本身**（`ragdemo.retrieval.vector.vector_search`），
不经过 `RetrievalService.search()` 的 BM25 融合、重排与父子块去重——那三步会把
同一父块下的多个叶子块折叠成一条证据，折叠掉的候选与「索引漏召回」是两回事，
混在一起测出来的数字没有意义（`tests/evals/test_index_recall.py` 的
`test_index_recall_is_one_on_a_tiny_corpus` 已经验证过这一点：按
`service.search().blocks` 直接比只能测出 0.75，是父子块去重的正常副作用，
不是索引路径的缺陷）。

精确扫描是全表，跑得慢，因此只在 nightly 任务里跑，不进 PR 门禁。
"""

from __future__ import annotations

from collections.abc import Sequence

import psycopg

from ragdemo.embed.base import Embedder
from ragdemo.retrieval.filters import build_filters
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalRequest
from ragdemo.retrieval.vector import apply_scan_settings, vector_search
from ragdemo_core.db.session import as_of_session

_SQL = """
SELECT block_id
  FROM asof.doc_block
 WHERE embedding IS NOT NULL
 {filters}
 ORDER BY embedding <=> %(qvec)s::vector
 LIMIT %(limit)s
"""


def exact_vector_search(
    conn: psycopg.Connection,
    req: RetrievalRequest,
    query_vector: Sequence[float],
    limit: int,
) -> list[int]:
    """强制精确扫描，作为召回率的分母。

    `%(qvec)s::vector` 的显式类型转换不能省——不转型时 psycopg 把参数当无类型
    字面量传给 Postgres，`<=>` 运算符解析不出该用哪个重载（`vector_index.py` /
    `embed/cache.py` / `embed/batch.py` 写向量字面量时都做了同样的转型，
    是本仓库反复验证过的约定，不是风格选择）。
    """
    filters, params = build_filters(req)
    params["qvec"] = "[" + ",".join(repr(float(x)) for x in query_vector) + "]"
    params["limit"] = limit

    with as_of_session(conn, req.as_of, tenant=req.tenant, user=req.user):
        conn.execute("SELECT set_config('enable_indexscan', 'off', true)")
        conn.execute("SELECT set_config('enable_indexonlyscan', 'off', true)")
        rows = conn.execute(_SQL.format(filters=filters), params).fetchall()
    return [int(r[0]) for r in rows]


def index_recall(
    conn: psycopg.Connection,
    service: RetrievalService,
    embedder: Embedder,
    cases: Sequence[RetrievalRequest],
) -> float:
    """索引路径命中的、精确路径也命中的比例，对每条用例取比值再平均。

    `service` 提供与生产一致的连接与扫描设置：`apply_scan_settings()` 用
    `SET LOCAL` 语义，必须与实际发起向量查询的语句共享同一个事务
    （`vector.py` 里 `apply_scan_settings` 的文档字符串），这里复用
    `service.conn` 并在同一个 `as_of_session` 块内先设置扫描参数、
    再调用 `vector_search`，就是为了保证这一点——不能悄悄换一套更宽松的
    扫描参数把召回率考好看。`embedder` 单独传入而不是取 `service.embedder`，
    是因为召回率要能独立于 `service` 内部选择的嵌入器复算。
    """
    if not cases:
        raise ValueError("用例为空，无法计算索引召回率")

    ratios: list[float] = []
    for req in cases:
        qvec = embedder.embed([req.query])[0]
        exact = set(exact_vector_search(conn, req, qvec, req.config.candidate_k))
        if not exact:
            continue
        with as_of_session(service.conn, req.as_of, tenant=req.tenant, user=req.user) as c:
            apply_scan_settings(c, req.config)
            approx = {hit.block_id for hit in vector_search(c, req, qvec)}
        ratios.append(len(approx & exact) / len(exact))
    return sum(ratios) / len(ratios) if ratios else 1.0
