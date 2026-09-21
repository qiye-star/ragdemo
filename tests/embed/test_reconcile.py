"""向量索引一致性核对与修复（ADR-0009 后果 2，`task-10-addendum.md`）。

场景全部在一个最小手工搭建的库上做（不用 `corpus` 夹具）：`corpus` 夹具调用
`embed_pending_blocks(conn, MockEmbedder())` 时不传 `index`，天然只写 PG、
不写任何向量索引，没法表达「PG 与索引本来同步，之后才出现偏移」这种对照，
这里需要能精确控制哪个块写了索引、哪个没写。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import chromadb
import psycopg
import pytest

from ragdemo.embed.base import l2_normalize
from ragdemo.embed.mock import MockEmbedder
from ragdemo.embed.reconcile import IndexDrift, reconcile_index
from ragdemo.retrieval.vector_index import ChromaVectorIndex, VectorItem
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
_KNOWN_AT = datetime(2024, 10, 28, 10, 32, tzinfo=UTC)


@pytest.fixture()
def db_conn(temp_db: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node) "
        "VALUES ('CN.688041','海光信息技术股份有限公司','listed','算力','AI芯片',"
        " ARRAY['通用服务器CPU'],'通用服务器CPU')"
    )
    conn.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at,"
        " source, content_hash, version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (1,'CN.688041','quarterly','三季报',"
        " %s,'mock','h1',1,'2024-07-01',%s,'r1')",
        (_KNOWN_AT, _KNOWN_AT),
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _vec_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _insert_block(
    conn: psycopg.Connection,
    *,
    ordinal: int,
    embedding: list[float],
    owner_user: str | None = None,
) -> int:
    row = conn.execute(
        "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal, content,"
        " is_leaf, entity_id, doc_type, publish_at, valid_from, known_at, embedding,"
        " owner_user, source, ingest_run_id) "
        "VALUES (1,'paragraph','第一节',%s,%s,true,'CN.688041','quarterly',%s,"
        " '2024-07-01',%s,%s,%s,'mock','r1') RETURNING block_id",
        (ordinal, f"块内容{ordinal}", _KNOWN_AT, _KNOWN_AT, _vec_literal(embedding), owner_user),
    ).fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def _supersede(conn: psycopg.Connection, block_id: int) -> None:
    conn.execute(
        "UPDATE core.doc_block SET superseded_at = %s WHERE block_id = %s",
        (datetime(2025, 1, 1, tzinfo=UTC), block_id),
    )
    conn.commit()


def _chroma_index(*, owner_user: str | None = None) -> ChromaVectorIndex:
    # 每个测试用独立 owner：EphemeralClient 的后端状态在同一进程内是共享的，
    # 固定 collection 名会在测试之间串数据（tests/retrieval/test_vector_index.py
    # 的 chroma_index 夹具已经踩过这个坑，这里沿用同样的隔离手法）。
    owner = owner_user or f"reconcile-{uuid.uuid4().hex}"
    return ChromaVectorIndex(chromadb.EphemeralClient(), owner_user=owner)


def _item(block_id: int, embedding: list[float]) -> VectorItem:
    return VectorItem(
        block_id=block_id,
        embedding=embedding,
        doc_id=1,
        entity_id="CN.688041",
        doc_type="quarterly",
        known_at=_KNOWN_AT,
        superseded_at=None,
        publish_at=_KNOWN_AT,
    )


@pytest.mark.db
def test_no_drift_after_normal_ingestion(db_conn: psycopg.Connection) -> None:
    """正常入库（PG 与索引都写成功）后，两个列表都为空。"""
    vec = l2_normalize(MockEmbedder().embed(["block-a"])[0])
    block_id = _insert_block(db_conn, ordinal=0, embedding=vec)
    index = _chroma_index()
    index.upsert([_item(block_id, vec)])

    drift = reconcile_index(db_conn, index)

    assert drift == IndexDrift(missing_in_index=[], orphan_in_index=[], checked=1)


@pytest.mark.db
def test_detects_and_repairs_missing_in_index(db_conn: psycopg.Connection) -> None:
    """模拟 upsert 失败后的偏移：块的 embedding 已写进 PG，索引里没有。"""
    vec = l2_normalize(MockEmbedder().embed(["block-b"])[0])
    block_id = _insert_block(db_conn, ordinal=0, embedding=vec)
    index = _chroma_index()

    drift = reconcile_index(db_conn, index)

    assert drift.missing_in_index == [block_id]
    assert drift.orphan_in_index == []
    assert index.existing_ids([block_id]) == {block_id}

    drift_again = reconcile_index(db_conn, index)
    assert drift_again == IndexDrift(missing_in_index=[], orphan_in_index=[], checked=1)


@pytest.mark.db
def test_detects_and_repairs_orphan_in_index(db_conn: psycopg.Connection) -> None:
    """孤儿检出：块在 PG 里已 supersede，索引里仍在。"""
    vec = l2_normalize(MockEmbedder().embed(["block-c"])[0])
    block_id = _insert_block(db_conn, ordinal=0, embedding=vec)
    index = _chroma_index()
    index.upsert([_item(block_id, vec)])
    _supersede(db_conn, block_id)

    drift = reconcile_index(db_conn, index)

    assert drift.missing_in_index == []
    assert drift.orphan_in_index == [block_id]
    assert index.existing_ids([block_id]) == set()


@pytest.mark.db
def test_reconciliation_is_idempotent(db_conn: psycopg.Connection) -> None:
    """连跑两次，第二次的 IndexDrift 全空——同时覆盖缺失与孤儿两条路径。"""
    vec_missing = l2_normalize(MockEmbedder().embed(["block-missing"])[0])
    vec_orphan = l2_normalize(MockEmbedder().embed(["block-orphan"])[0])
    missing_id = _insert_block(db_conn, ordinal=0, embedding=vec_missing)
    orphan_id = _insert_block(db_conn, ordinal=1, embedding=vec_orphan)

    index = _chroma_index()
    index.upsert([_item(orphan_id, vec_orphan)])
    _supersede(db_conn, orphan_id)

    first = reconcile_index(db_conn, index)
    assert first.missing_in_index == [missing_id]
    assert first.orphan_in_index == [orphan_id]

    second = reconcile_index(db_conn, index)
    assert second == IndexDrift(missing_in_index=[], orphan_in_index=[], checked=2)


@pytest.mark.db
def test_reconciliation_is_scoped_to_owner_user(db_conn: psycopg.Connection) -> None:
    """owner_user 不匹配时不能互相污染（CLAUDE.md §0 用户上传材料私有隔离）。

    核对的 PG 侧候选集合必须限定在与 `index` 同一个身份下，否则会把 A 用户的
    基准拿去核对 B 用户的 collection，或者把私有块的偏移算进公共空间的报告里。
    """
    vec = l2_normalize(MockEmbedder().embed(["private-block"])[0])
    block_id = _insert_block(db_conn, ordinal=0, embedding=vec, owner_user="alice")

    public_index = _chroma_index()
    public_drift = reconcile_index(db_conn, public_index)
    assert public_drift == IndexDrift(missing_in_index=[], orphan_in_index=[], checked=0)
    assert public_index.existing_ids([block_id]) == set()

    private_index = _chroma_index()
    private_drift = reconcile_index(db_conn, private_index, owner_user="alice")
    assert private_drift.missing_in_index == [block_id]
    assert private_index.existing_ids([block_id]) == {block_id}
