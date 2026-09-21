"""向量索引一致性核对与修复（ADR-0009 后果 2 / `task-10-addendum.md`）。

`embed_pending_blocks()`（`embed/batch.py`）的待办队列是
`is_leaf AND embedding IS NULL`，写入顺序是先提交 PG 事务、再写向量索引——
这个顺序是对的：反过来会在 PG 回滚时留下索引里的孤儿向量，那些向量不受 PG
事务保护，没有对应的 `core.doc_block` 行去撤销它们。

问题出在失败路径：PG 事务提交成功（`doc_block.embedding` 已写入）之后，
`index.upsert()` 抛异常（Chroma 挂了、网络断了、协议不兼容）。重跑
`embed_pending_blocks()` 时，这些块的 `embedding` 已经不是 `NULL`，
待办队列选不中它们——于是它们**永久缺席于向量索引**，不报错，只会让向量一路
的召回悄悄少一块，直到某次用户投诉里才露头。这正是 ADR-0009「后果 2」说的
那类偏移：Chroma 是候选生成器而不是时点权威，PG↔Chroma 的一致性核对因此不是
一次性测试，而是需要长期监控、能反复修复的运维动作。

`reconcile_index()` 是这条失败路径的唯一修复出口。**刻意不把
`embed_pending_blocks` 里的 `index.upsert` 包进 try/except 吞掉异常**——
静默吞掉会让「Chroma 挂了」表现为「嵌入正常完成」，偏移要等到下一次核对才
暴露，比「保持抛异常 + 提供这里的修复出口」更不诚实（task-10-addendum.md
「一个刻意不做的决定」）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import psycopg

from ragdemo.embed.cache import PUBLIC_OWNER
from ragdemo.retrieval.vector_index import VectorIndex, VectorItem


@dataclass(frozen=True)
class IndexDrift:
    """PG ↔ 向量索引的偏移快照。两个列表都为空表示两边一致。"""

    missing_in_index: list[int]
    """PG 有向量（未 supersede 的叶子块）、索引里没有——`embed_pending_blocks`
    的 upsert 失败路径留下的洞。"""

    orphan_in_index: list[int]
    """索引里有、但 PG 侧已经 supersede——查询时点若晚于更正生效时刻，
    这些向量会把「已被撤回的块」泄漏进候选集合（ADR-0009 要防的那类错误）。"""

    checked: int
    """本次核对覆盖的 PG 侧候选总数（有效基准 + 待核实的过期基准之和）。"""


def _to_floats(value: object) -> list[float]:
    """读回 `core.doc_block.embedding` 列的向量值。

    不复用 `embed/cache.py` 里同名的私有函数：那个模块当前有另一个会话在
    并发重写，只读 import 都可能因为其内部实现变动而跟着漂移
    （`task-10-addendum.md` 明确要求本模块与 `embed/batch.py` /
    `embed/cache.py` 完全解耦），在这里自己保留一份等价实现更安全。
    """
    if isinstance(value, str):
        return [float(x) for x in value.strip("[]").split(",")]
    # psycopg 的 pgvector 适配器通常把 vector 列读成 numpy 数组或 pgvector
    # 自带的 Vector 类型，但类型标注只声明为 object——不为此单独加依赖，
    # 用 attr-defined 精确抑制。
    return [float(x) for x in value]  # type: ignore[attr-defined]


def _load_items(conn: psycopg.Connection, block_ids: Sequence[int]) -> list[VectorItem]:
    """按 block_id 取回写索引所需的向量与元数据。查 core 基表理由见下方注释。"""
    rows = conn.execute(
        "SELECT block_id, embedding, doc_id, entity_id, doc_type,"
        "       known_at, superseded_at, publish_at"
        "  FROM core.doc_block WHERE block_id = ANY(%s)",
        (list(block_ids),),
    ).fetchall()
    return [
        VectorItem(
            block_id=int(r[0]),
            embedding=_to_floats(r[1]),
            doc_id=int(r[2]),
            entity_id=r[3],
            doc_type=str(r[4]),
            known_at=r[5],
            superseded_at=r[6],
            publish_at=r[7],
        )
        for r in rows
    ]


def reconcile_index(
    conn: psycopg.Connection,
    index: VectorIndex,
    *,
    owner_user: str = PUBLIC_OWNER,
    limit: int | None = None,
) -> IndexDrift:
    """把 PG 里已嵌入但索引里缺失的叶子块补写进索引，并删掉索引里的孤儿。

    检测与修复一次做完：返回值是**本次调用检测到、并已尝试修复**的偏移。
    因此可以当幂等的自愈探针反复跑——真的没有偏移时返回全空的
    `IndexDrift`；连续两次调用，第二次必然全空（除非两次调用之间又有新的
    写入失败）。

    Args:
        conn: 数据库连接。
        index: 待核对/修复的向量索引（`ChromaVectorIndex` 或 `PgVectorIndex`，
            两者都实现了 `existing_ids()`）。
        owner_user: 与 `embed_pending_blocks(owner_user=...)` 同一个哨兵——
            `PUBLIC_OWNER`（`""`）表示公共空间。调用方必须确保这里的
            `owner_user` 与构造 `index` 时用的是同一个身份（例如
            `ChromaVectorIndex(client, owner_user=...)`），否则会把 A 用户的
            基准去核对 B 用户的 collection（`vector_index.py` 的
            `PUBLIC_OWNER` 是另一个同名但不同值的哨兵，两者的映射关系见
            `embed/batch.py` 的既有注释）。
        limit: 单次最多修复多少条缺失/孤儿记录，`None` 表示不限。数十万块
            规模下一次性全量补写可能耗时很长，需要能分批跑。

    Returns:
        `IndexDrift`：`missing_in_index` / `orphan_in_index` 是本次发现并已
        执行修复动作的 block_id 列表（按 `block_id` 升序），`checked` 是本次
        核对覆盖的 PG 侧候选总数。修复动作本身仍可能抛异常
        （`index.upsert` / `index.delete` 失败）——本函数不吞异常，失败时
        调用方会直接看到底层异常，而不是一个假装成功的空结果。
    """
    doc_block_owner = None if owner_user == PUBLIC_OWNER else owner_user

    # 一致性核对是运维动作，要看的是「当下库里到底有什么」，不是某个 as_of
    # 时点下经过时点过滤的视图——这是本仓库里极少数正当查 core 基表而不是
    # asof 视图的场景（CLAUDE.md §1.1 的例外，task-10-addendum.md 已明确
    # 授权并要求在此处写明理由）。
    valid_rows = conn.execute(
        "SELECT block_id FROM core.doc_block"
        " WHERE is_leaf AND embedding IS NOT NULL AND superseded_at IS NULL"
        "   AND owner_user IS NOT DISTINCT FROM %s"
        " ORDER BY block_id",
        (doc_block_owner,),
    ).fetchall()
    valid_ids = [int(r[0]) for r in valid_rows]

    # 孤儿候选：曾经嵌入过、现在已被 supersede 的叶子块。core.doc_block 只做
    # 版本化、不做物理删除（005 迁移的 RLS 策略注释：「文档只做版本化，不做
    # 物理删除」），所以「索引里有、PG 里整行都不存在」这类孤儿在本系统的
    # 数据模型下结构性地不会发生——孤儿只能经由「已被 supersede」这一条路径
    # 产生，下面的查询因此是完整的孤儿候选集合，不是简化近似。
    stale_rows = conn.execute(
        "SELECT block_id FROM core.doc_block"
        " WHERE is_leaf AND embedding IS NOT NULL AND superseded_at IS NOT NULL"
        "   AND owner_user IS NOT DISTINCT FROM %s"
        " ORDER BY block_id",
        (doc_block_owner,),
    ).fetchall()
    stale_ids = [int(r[0]) for r in stale_rows]

    present_among_valid = index.existing_ids(valid_ids)
    missing = [block_id for block_id in valid_ids if block_id not in present_among_valid]

    present_among_stale = index.existing_ids(stale_ids)
    orphans = [block_id for block_id in stale_ids if block_id in present_among_stale]

    if limit is not None:
        missing = missing[:limit]
        orphans = orphans[:limit]

    if missing:
        index.upsert(_load_items(conn, missing))
    if orphans:
        index.delete(orphans)

    return IndexDrift(
        missing_in_index=missing,
        orphan_in_index=orphans,
        checked=len(valid_ids) + len(stale_ids),
    )
