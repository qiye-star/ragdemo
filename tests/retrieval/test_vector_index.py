"""向量候选生成层：collection 命名、Chroma 预过滤、与 pgvector 的一致性。

ADR-0009：Chroma 只是候选生成器，不是时点权威。这里测的是「候选生成」这一层
本身的正确性（预过滤、排序、隔离、双跑一致），**不**测「调用方再用 PG 过一遍」
那部分——那是 Task 3 的职责。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import chromadb
import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.retrieval.vector_index import (
    NEVER_SUPERSEDED,
    PUBLIC_COLLECTION,
    PUBLIC_OWNER,
    ChromaVectorIndex,
    PgVectorIndex,
    VectorItem,
    chroma_client_from_env,
    collection_name,
)
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
_DEFAULT_KNOWN_AT = datetime(2024, 1, 1, tzinfo=UTC)


def _item(
    block_id: int,
    embedding: Sequence[float],
    *,
    doc_id: int = 1,
    entity_id: str | None = "CN.688041",
    doc_type: str = "quarterly",
    known_at: datetime = _DEFAULT_KNOWN_AT,
    superseded_at: datetime | None = None,
    publish_at: datetime | None = None,
) -> VectorItem:
    return VectorItem(
        block_id=block_id,
        embedding=embedding,
        doc_id=doc_id,
        entity_id=entity_id,
        doc_type=doc_type,
        known_at=known_at,
        superseded_at=superseded_at,
        publish_at=publish_at if publish_at is not None else known_at,
    )


@pytest.fixture()
def chroma_index() -> ChromaVectorIndex:
    """每个测试一个独立 owner——EphemeralClient 在同一进程内是共享后端，
    固定 collection 名会在测试之间串数据；每次给不同 owner_user 换一个
    物理隔离的 collection，天然避免串扰。"""
    owner = f"test-{uuid.uuid4().hex}"
    return ChromaVectorIndex(chromadb.EphemeralClient(), owner_user=owner)


# ---------------------------------------------------------------------------
# collection_name
# ---------------------------------------------------------------------------


def test_collection_name_public_for_none_and_public_owner() -> None:
    assert collection_name(None) == PUBLIC_COLLECTION
    assert collection_name(PUBLIC_OWNER) == PUBLIC_COLLECTION
    assert collection_name("public") == PUBLIC_COLLECTION


def test_collection_name_is_legal_and_unique_per_owner() -> None:
    name = collection_name("user@example.com")
    assert 3 <= len(name) <= 512
    assert re.fullmatch(r"[a-zA-Z0-9._-]+", name)
    assert name[0].isalnum()
    assert name[-1].isalnum()

    other_name = collection_name("another-user@example.com")
    assert other_name != name
    assert 3 <= len(other_name) <= 512


def test_collection_name_normalizes_invalid_characters_without_colliding() -> None:
    """ "@" 与 "#" 都会被替换成 "-"，规范化后可能撞字符串，但哈希后缀保证不撞名。"""
    a = collection_name("user@example.com")
    b = collection_name("user#example.com")
    assert a != b


# ---------------------------------------------------------------------------
# ChromaVectorIndex：排序、预过滤、None metadata
# ---------------------------------------------------------------------------


def test_query_ranks_candidates_by_distance_ascending(chroma_index: ChromaVectorIndex) -> None:
    embedder = MockEmbedder()
    e1, e2, e3 = embedder.embed(["block-a", "block-b", "block-c"])
    chroma_index.upsert([_item(1, e1), _item(2, e2), _item(3, e3)])

    candidates = chroma_index.query(e1, k=3)

    assert len(candidates) == 3
    assert candidates[0].block_id == 1
    assert candidates[0].distance == pytest.approx(0.0, abs=1e-6)
    distances = [c.distance for c in candidates]
    assert distances == sorted(distances), "rank 语义：距离必须升序"


def test_query_prefilters_blocks_with_future_known_at(chroma_index: ChromaVectorIndex) -> None:
    embedder = MockEmbedder()
    e_future, e_past = embedder.embed(["future-block", "past-block"])
    as_of = datetime(2024, 6, 1, tzinfo=UTC)
    chroma_index.upsert(
        [
            _item(1, e_future, known_at=datetime(2025, 1, 1, tzinfo=UTC)),
            _item(2, e_past, known_at=datetime(2023, 1, 1, tzinfo=UTC)),
        ]
    )

    candidates = chroma_index.query(e_future, k=10, as_of=as_of)

    ids = {c.block_id for c in candidates}
    assert 1 not in ids, "known_at 在 as_of 之后的块不应出现"
    assert 2 in ids


def test_query_prefilters_superseded_blocks(chroma_index: ChromaVectorIndex) -> None:
    embedder = MockEmbedder()
    e1, e2, e3 = embedder.embed(["sup-1", "sup-2", "sup-3"])
    known_at = datetime(2023, 1, 1, tzinfo=UTC)
    as_of = datetime(2024, 6, 1, tzinfo=UTC)
    chroma_index.upsert(
        [
            # 已被更早的时刻 supersede，查询时点之前就已撤回。
            _item(1, e1, known_at=known_at, superseded_at=datetime(2024, 1, 1, tzinfo=UTC)),
            # 从未被 supersede：任何 as_of 都不应被这条规则滤掉。
            _item(2, e2, known_at=known_at, superseded_at=None),
            # supersede 发生在 as_of 之后，查询时点仍应可见。
            _item(3, e3, known_at=known_at, superseded_at=datetime(2025, 1, 1, tzinfo=UTC)),
        ]
    )

    candidates = chroma_index.query(e1, k=10, as_of=as_of)

    ids = {c.block_id for c in candidates}
    assert 1 not in ids, "已被 supersede 的块不应出现在更晚的 as_of 查询里"
    assert {2, 3}.issubset(ids)


def test_upsert_and_query_with_null_entity_id(chroma_index: ChromaVectorIndex) -> None:
    """回归：entity_id 为 None 时曾经直接写成 metadata None，
    实测 Chroma 拒绝 None 值（TypeError）。"""
    embedder = MockEmbedder()
    (e1,) = embedder.embed(["no-entity-block"])
    chroma_index.upsert([_item(1, e1, entity_id=None)])

    candidates = chroma_index.query(e1, k=1)

    assert [c.block_id for c in candidates] == [1]


def test_never_superseded_sentinel_value() -> None:
    """哨兵值本身要足够大：覆盖任何现实中的 as_of。"""
    assert datetime(9999, 1, 1, tzinfo=UTC).timestamp() < NEVER_SUPERSEDED


# ---------------------------------------------------------------------------
# PgVectorIndex 与 ChromaVectorIndex 的一致性（Task 10 双跑核对的雏形）
# ---------------------------------------------------------------------------


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
        " '2024-10-28 18:32+08','mock','h1',1,'2024-07-01','2024-10-28 18:32+08','r1')"
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.db
def test_pg_and_chroma_return_consistent_top_k_block_ids(db_conn: psycopg.Connection) -> None:
    embedder = MockEmbedder()
    vectors = embedder.embed([f"consistency-block-{i}" for i in range(5)])
    known_at = datetime(2024, 10, 28, 10, 32, tzinfo=UTC)

    block_ids: list[int] = []
    for i in range(5):
        row = db_conn.execute(
            "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal,"
            " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at,"
            " source, ingest_run_id) "
            "VALUES (1,'paragraph','第一节',%s,%s,true,'CN.688041','quarterly',"
            " %s,'2024-07-01',%s,'mock','r1') RETURNING block_id",
            (i, f"一致性内容{i}", known_at, known_at),
        ).fetchone()
        assert row is not None
        block_ids.append(int(row[0]))
    db_conn.commit()

    items = [
        _item(
            block_id,
            vector,
            doc_id=1,
            entity_id="CN.688041",
            doc_type="quarterly",
            known_at=known_at,
        )
        for block_id, vector in zip(block_ids, vectors, strict=True)
    ]

    pg_index = PgVectorIndex(db_conn)
    pg_index.upsert(items)

    owner = f"test-{uuid.uuid4().hex}"
    chroma_index = ChromaVectorIndex(chromadb.EphemeralClient(), owner_user=owner)
    chroma_index.upsert(items)

    as_of = datetime(2025, 1, 1, tzinfo=UTC)
    query_vector = vectors[0]

    pg_candidates = pg_index.query(query_vector, k=3, as_of=as_of)
    chroma_candidates = chroma_index.query(query_vector, k=3, as_of=as_of)

    assert {c.block_id for c in pg_candidates} == {c.block_id for c in chroma_candidates}
    assert len(pg_candidates) == 3


@pytest.mark.db
def test_pg_vector_index_upsert_delete_count_roundtrip(db_conn: psycopg.Connection) -> None:
    embedder = MockEmbedder()
    (vector,) = embedder.embed(["pg-roundtrip"])
    known_at = datetime(2024, 10, 28, 10, 32, tzinfo=UTC)
    row = db_conn.execute(
        "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal,"
        " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at,"
        " source, ingest_run_id) "
        "VALUES (1,'paragraph','第一节',0,'内容',true,'CN.688041','quarterly',"
        " %s,'2024-07-01',%s,'mock','r1') RETURNING block_id",
        (known_at, known_at),
    ).fetchone()
    assert row is not None
    block_id = int(row[0])
    db_conn.commit()

    index = PgVectorIndex(db_conn)
    assert index.count() == 0

    index.upsert([_item(block_id, vector, doc_id=1, known_at=known_at)])
    assert index.count() == 1

    index.delete([block_id])
    assert index.count() == 0


# ---------------------------------------------------------------------------
# chroma_client_from_env
# ---------------------------------------------------------------------------


def test_chroma_client_from_env_rejects_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGDEMO_CHROMA_URL", "https://chroma.example.com:8000")
    with pytest.raises(ValueError, match="非私有地址"):
        chroma_client_from_env()


def test_chroma_client_from_env_rejects_chroma_cloud_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0009 边界：明确禁止 Chroma Cloud。"""
    monkeypatch.setenv("RAGDEMO_CHROMA_URL", "https://api.trychroma.com")
    with pytest.raises(ValueError, match="非私有地址"):
        chroma_client_from_env()


def test_chroma_client_from_env_defaults_to_ephemeral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAGDEMO_CHROMA_URL", raising=False)
    monkeypatch.delenv("RAGDEMO_CHROMA_PATH", raising=False)

    client = chroma_client_from_env()

    assert (
        client.get_or_create_collection(
            "doc_block_public", metadata={"hnsw:space": "cosine"}
        ).count()
        == 0
    )


def test_chroma_client_from_env_uses_persistent_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RAGDEMO_CHROMA_URL", raising=False)
    monkeypatch.setenv("RAGDEMO_CHROMA_PATH", str(tmp_path))

    client = chroma_client_from_env()

    assert (
        client.get_or_create_collection(
            "doc_block_public", metadata={"hnsw:space": "cosine"}
        ).count()
        == 0
    )
