"""向量候选生成层（ADR-0009）。

**Chroma 不是时点权威。** 它只按向量相似度给出候选 `block_id`；`known_at` /
`superseded_at` 的权威判定永远在 PostgreSQL 的 `asof.doc_block` 视图里做
（`CLAUDE.md` §1.1）。本模块的 `query()` 允许把时点谓词下推给底层引擎做
**预过滤**（快，但不保证准——Chroma 不参与 PG 的事务，一次更正入库与同步
之间必然有窗口），调用方（Task 3 的检索链路）**必须**再用 `asof.doc_block`
过一遍才能把结果当真。两道过滤都要有，缺一个就是把「已被撤回的块」泄漏进
某个历史时点检索结果的那类错误。

`PgVectorIndex` 保留 pgvector 实现，一是 ADR-0005 供应商可替换原则，
二是 Task 10 双跑一致性核对的对照组——没有第二个实现就没法判断是 Chroma
漏召回了，还是查询本身就该返回这些（ADR-0009 后果 4）。它不做按
`owner_user` 的物理隔离：`core.doc_block` 本身就是唯一的权威表，没有「选错
表」这个风险类别；用户材料隔离只对 Chroma（真正的独立存储）才是必需品
（ADR-0009 边界）。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

import chromadb
import psycopg

from ragdemo_core.db.session import as_of_session

PUBLIC_COLLECTION = "doc_block_public"

# 调用方用来表达「我要访问公共向量空间」的显式哨兵值。与
# `ragdemo.embed.cache.PUBLIC_OWNER`（=""，嵌入缓存表里代表公共行的
# owner_user 列值）是两个独立概念，只是恰好同名：那个是持久化的表内哨兵，
# 这个是本模块 API 层面的调用约定，互不影响。
PUBLIC_OWNER = "public"

# superseded_at IS NULL 的哨兵值：9999-12-31T23:59:59Z 的 epoch 秒。
# 用哨兵而不是省略 key，是因为 Chroma 的 where 过滤对「缺失的 key」
# 不会匹配任何比较操作符——省略会让未被 supersede 的块全部召不回来。
NEVER_SUPERSEDED = 253402300799.0

_COLLECTION_NAME_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9._-]")
_COLLECTION_NAME_PREFIX = "doc_block_u_"
_COLLECTION_NAME_MAX_LEN = 512
_OWNER_HASH_LEN = 16

# Chroma 自建容器的 hostname 允许清单：不做 DNS 解析（避免联网、避免
# TOCTOU），纯字面判断。docker-compose 里的服务名是不带点的裸主机名
# （如 "chroma"），因此裸主机名一律放行；带点的域名只放行内网惯用后缀。
_PRIVATE_HOSTNAME_SUFFIXES = (".local", ".internal", ".cluster.local")


@dataclass(frozen=True)
class VectorItem:
    """写入向量索引的一条记录，字段取自 `core.doc_block`。"""

    block_id: int
    embedding: Sequence[float]
    doc_id: int
    entity_id: str | None
    doc_type: str
    known_at: datetime
    superseded_at: datetime | None
    publish_at: datetime


@dataclass(frozen=True)
class VectorCandidate:
    """一条候选结果。"""

    block_id: int
    distance: float  # 余弦距离，越小越相关；不是相似度，排序不要搞反。


@runtime_checkable
class VectorIndex(Protocol):
    """向量候选生成器协议。ADR-0005：业务代码只依赖这个协议，不 import 具体实现。"""

    def upsert(self, items: Sequence[VectorItem]) -> None:
        """写入或更新一批向量。"""
        ...

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int,
        as_of: datetime | None = None,
        entity_ids: Sequence[str] | None = None,
        doc_types: Sequence[str] | None = None,
    ) -> list[VectorCandidate]:
        """按向量相似度返回候选，按距离升序排列。

        `as_of` / `entity_ids` / `doc_types` 是**预过滤**，用于减少候选数量与
        提升召回相关性，不是最终的时点权威判定——调用方仍须再用
        `asof.doc_block` 过一遍。

        entity / doc_type 收复数而不是单数：`RetrievalRequest` 携带的本来就是
        列表，收单数会让多实体查询在候选生成阶段完全放弃窄化（只能靠权威过滤
        兜底），过采样倍数因此被迫调高，召回率与延迟一起变差。
        """
        ...

    def delete(self, block_ids: Sequence[int]) -> None:
        """从索引中移除指定块（不代表删除 `core.doc_block` 里的行）。"""
        ...

    def count(self) -> int:
        """索引中当前的向量条数。"""
        ...


def collection_name(owner_user: str | None) -> str:
    """把 owner_user 规范化成合法的 Chroma collection 名。

    公共空间（`owner_user` 为 `None` 或 `PUBLIC_OWNER`）用 `PUBLIC_COLLECTION`。
    用户上传材料按 owner 物理隔离到独立 collection——`CLAUDE.md` §0
    「用户上传材料私有隔离，不得进入公共检索空间」。用独立 collection 而不是
    元数据过滤，是因为过滤条件写错只是一行代码的事，collection 选错会在
    写入时就暴露（ADR-0009 边界）。

    规范化规则：非 `[a-zA-Z0-9._-]` 的字符替换为 `-`，前缀 `doc_block_u_`，
    尾部接 `owner_user` 的 sha256 前 16 位（避免不同 owner 规范化后撞名），
    整体截断到 512 字符以内。截断只作用于中间的规范化片段，前缀与哈希后缀
    始终完整保留，因此首尾必为字母数字——满足 Chroma 对 collection 名的
    格式要求。
    """
    if owner_user is None or owner_user == PUBLIC_OWNER:
        return PUBLIC_COLLECTION

    normalized = _COLLECTION_NAME_INVALID_CHARS.sub("-", owner_user)
    suffix = hashlib.sha256(owner_user.encode("utf-8")).hexdigest()[:_OWNER_HASH_LEN]
    # 前缀 + 分隔下划线 + 哈希后缀是固定开销，截断预算只从中间片段里扣。
    budget = _COLLECTION_NAME_MAX_LEN - len(_COLLECTION_NAME_PREFIX) - 1 - len(suffix)
    normalized = normalized[:budget]
    return f"{_COLLECTION_NAME_PREFIX}{normalized}_{suffix}"


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector 的文本输入格式：`[x1,x2,...]`。"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _membership(field: str, values: Sequence[str]) -> dict[str, object]:
    """单值用 $eq、多值用 $in。

    分开写不是洁癖：$in 只有一个元素时 Chroma 也接受，但 $eq 的语义更直接，
    而且把「单值」这条最常见的路径与多值分开，将来换索引实现时更容易映射。
    """
    if len(values) == 1:
        return {field: {"$eq": values[0]}}
    return {field: {"$in": list(values)}}


def _to_chroma_metadata(item: VectorItem) -> dict[str, str | int | float]:
    """把 VectorItem 转成 Chroma metadata。

    Chroma metadata 的值只能是 str / int / float / bool，不能是 `None`
    （实测：写 `None` 会抛 `TypeError: Cannot convert Python object to
    MetadataValue`）。`entity_id` 在库里可以为 NULL，这里用空串顶替；
    `superseded_at` 为 `None` 时写 `NEVER_SUPERSEDED` 哨兵。
    """
    superseded_at = (
        item.superseded_at.timestamp() if item.superseded_at is not None else NEVER_SUPERSEDED
    )
    return {
        "doc_id": item.doc_id,
        "entity_id": item.entity_id if item.entity_id is not None else "",
        "doc_type": item.doc_type,
        "known_at": item.known_at.timestamp(),
        "superseded_at": superseded_at,
        "publish_at": item.publish_at.timestamp(),
    }


class ChromaVectorIndex:
    """Chroma 实现：候选生成器，不是时点权威（ADR-0009）。"""

    def __init__(self, client: chromadb.api.ClientAPI, *, owner_user: str | None = None) -> None:
        self._owner_user = owner_user
        self._collection = client.get_or_create_collection(
            collection_name(owner_user), metadata={"hnsw:space": "cosine"}
        )

    def upsert(self, items: Sequence[VectorItem]) -> None:
        if not items:
            return
        self._collection.upsert(
            ids=[str(item.block_id) for item in items],
            # chromadb 的类型标注对 `list` 是不变的，`list[list[float]]` 因此
            # 对不上 `list[Sequence[float] | Sequence[int]]`——运行时完全接受。
            embeddings=[list(item.embedding) for item in items],  # type: ignore[arg-type]
            metadatas=[_to_chroma_metadata(item) for item in items],
        )

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int,
        as_of: datetime | None = None,
        entity_ids: Sequence[str] | None = None,
        doc_types: Sequence[str] | None = None,
    ) -> list[VectorCandidate]:
        if self._collection.count() == 0:
            return []

        conditions: list[dict[str, object]] = []
        if as_of is not None:
            epoch = as_of.timestamp()
            conditions.append({"known_at": {"$lte": epoch}})
            conditions.append({"superseded_at": {"$gt": epoch}})
        # 单值用 $eq、多值用 $in。两个都支持，$in 也能嵌在 $and 里（实测）。
        if entity_ids:
            conditions.append(_membership("entity_id", entity_ids))
        if doc_types:
            conditions.append(_membership("doc_type", doc_types))

        # 复合条件必须写成 {"$and": [...]}，不能把多个 key 平铺后还带操作符；
        # 只有一个条件时直接用它本身即可（实测约束 4）。
        where: dict[str, object] | None
        if len(conditions) > 1:
            where = {"$and": conditions}
        elif len(conditions) == 1:
            where = conditions[0]
        else:
            where = None

        result = self._collection.query(
            query_embeddings=[list(vector)],  # type: ignore[arg-type]
            n_results=k,
            where=where,  # type: ignore[arg-type]
        )
        ids = result["ids"][0]
        distances = result["distances"][0]  # type: ignore[index]
        candidates = [
            VectorCandidate(block_id=int(block_id), distance=float(distance))
            for block_id, distance in zip(ids, distances, strict=True)
        ]
        candidates.sort(key=lambda c: c.distance)
        return candidates

    def delete(self, block_ids: Sequence[int]) -> None:
        if not block_ids:
            return
        self._collection.delete(ids=[str(block_id) for block_id in block_ids])

    def count(self) -> int:
        return self._collection.count()


class PgVectorIndex:
    """pgvector 实现。ADR-0009 后果 4：保留它既为可替换性，
    也作为 Task 10 双跑一致性的对照组。

    `core.doc_block` 本身就是权威表，不是一份可重建的派生索引——`upsert` /
    `delete` 因此只触碰 `embedding` 列，其余时点字段（`known_at` /
    `superseded_at` 等）由文档摄入与更正流程维护，不归这层管。
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def upsert(self, items: Sequence[VectorItem]) -> None:
        if not items:
            return
        with self._conn.transaction():
            for item in items:
                self._conn.execute(
                    "UPDATE core.doc_block SET embedding = %s::vector WHERE block_id = %s",
                    (_vector_literal(item.embedding), item.block_id),
                )

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int,
        as_of: datetime | None = None,
        entity_ids: Sequence[str] | None = None,
        doc_types: Sequence[str] | None = None,
    ) -> list[VectorCandidate]:
        """按余弦距离升序返回候选。

        `as_of` 给定时查 `asof.doc_block` 安全视图（`CLAUDE.md` §1.1：应用层
        只允许查 asof 视图），时点谓词由视图本身强制。`as_of` 为 `None`
        时退化为对 `core.doc_block` 的纯向量近邻查询（不带任何时点谓词）——
        与 `ChromaVectorIndex` 在 `as_of=None` 时的语义对称：两者此时都只是
        「给个候选」，不宣称时点权威性；这正是 ADR-0009 里「Chroma 只是候选
        生成器」的对照实现，本身就不属于 `CLAUDE.md` §1.1 约束的「应用层
        查询接口」。

        注意 pgvector 的 `<=>` 运算符优先级低于 `*`：若要在此基础上做
        `距离 * -1` 之类的变换必须显式加括号，否则会被解析成
        `embedding <=> (%s * -1)`（实测踩过的坑）。本方法直接用距离升序，
        未触发这个问题。
        """
        qvec = _vector_literal(vector)
        params: dict[str, object] = {
            "qvec": qvec,
            # 空列表与 None 都表示「不按这一维过滤」，统一成 NULL 交给下面的
            # `IS NULL OR ...` 分支处理，避免 `= ANY('{}')` 恒假把结果清空。
            "entity_ids": list(entity_ids) if entity_ids else None,
            "doc_types": list(doc_types) if doc_types else None,
            "k": k,
        }
        source = "asof.doc_block" if as_of is not None else "core.doc_block"
        sql = (
            f"SELECT block_id, embedding <=> %(qvec)s::vector AS distance "
            f"  FROM {source} "
            " WHERE embedding IS NOT NULL "
            "   AND is_leaf "
            "   AND (%(entity_ids)s::text[] IS NULL OR entity_id = ANY(%(entity_ids)s)) "
            "   AND (%(doc_types)s::text[] IS NULL OR doc_type = ANY(%(doc_types)s)) "
            " ORDER BY embedding <=> %(qvec)s::vector "
            " LIMIT %(k)s"
        )
        if as_of is not None:
            with as_of_session(self._conn, as_of):
                rows = self._conn.execute(sql, params).fetchall()
        else:
            rows = self._conn.execute(sql, params).fetchall()
        return [VectorCandidate(block_id=int(r[0]), distance=float(r[1])) for r in rows]

    def delete(self, block_ids: Sequence[int]) -> None:
        if not block_ids:
            return
        with self._conn.transaction():
            self._conn.execute(
                "UPDATE core.doc_block SET embedding = NULL WHERE block_id = ANY(%s)",
                (list(block_ids),),
            )

    def count(self) -> int:
        row = self._conn.execute(
            "SELECT count(*) FROM core.doc_block WHERE embedding IS NOT NULL"
        ).fetchone()
        assert row is not None
        return int(row[0])


def _is_private_host(host: str) -> bool:
    """判断 host 是否只可能指向本地/私有网络。

    Chroma Cloud（如 `*.trychroma.com`）与任何公网地址一律判定为非私有——
    `CLAUDE.md` §0「数据境内存储」+ ADR-0009 边界「禁止 Chroma Cloud 或任何
    境外托管实例」。不做 DNS 解析：既避免测试/沙箱环境的联网依赖，也避免
    「判断时私有、连接时已被解析到别处」的 TOCTOU 窗口。

    - IP 字面量：私有网段（含回环、链路本地）放行，公网 IP 拒绝。
    - 主机名：`localhost`、不带 `.` 的裸主机名（docker-compose 服务名，如
      `chroma`）、或以内网惯用后缀结尾（`.local` / `.internal` /
      `.cluster.local`）放行；其余一律视为公网域名，拒绝。
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return ip.is_private

    if host == "localhost" or "." not in host:
        return True
    return host.endswith(_PRIVATE_HOSTNAME_SUFFIXES)


def chroma_client_from_env() -> chromadb.api.ClientAPI:
    """按环境变量构造客户端。

    `RAGDEMO_CHROMA_URL` 存在 -> `HttpClient`（自建容器）
    否则 `RAGDEMO_CHROMA_PATH` -> `PersistentClient`
    否则 -> `EphemeralClient`

    绝不返回连接 Chroma Cloud 的客户端：`CLAUDE.md` §0 数据境内存储。
    `RAGDEMO_CHROMA_URL` 指向非私有地址时抛 `ValueError`——在构造真正的
    `HttpClient`（它的构造函数会立即尝试连接）之前就拒绝，不发起任何对外
    网络请求。
    """
    url = os.environ.get("RAGDEMO_CHROMA_URL")
    if url:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or ""
        if not _is_private_host(host):
            raise ValueError(
                f"RAGDEMO_CHROMA_URL 指向非私有地址 {host!r}——"
                "禁止连接境外/公网 Chroma 实例（CLAUDE.md §0，ADR-0009 边界）"
            )
        port = parsed.port or (443 if parsed.scheme == "https" else 8000)
        return chromadb.HttpClient(host=host, port=port, ssl=parsed.scheme == "https")

    path = os.environ.get("RAGDEMO_CHROMA_PATH")
    if path:
        return chromadb.PersistentClient(path=path)

    return chromadb.EphemeralClient()
