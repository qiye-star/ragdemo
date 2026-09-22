"""评测冻结快照：同一 as_of 重放三次结果一致（H5），且能感知语料变化。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.evals.freeze import (
    create_freeze,
    diff_against_current_corpus,
    load_freeze,
    save_freeze,
)
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
AS_OF = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node) "
        "VALUES ('CN.A','甲公司','listed','算力','AI芯片',ARRAY['x'],'x')"
    )
    c.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at, source,"
        " content_hash, version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (1,'CN.A','quarterly','t','2024-10-28 10:00+00',"
        "'mock','h1',1,'2024-07-01','2024-10-28 10:00+00','r1')"
    )
    c.execute(
        "INSERT INTO core.doc_block (block_id, doc_id, block_type, section_path, ordinal,"
        " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at, source,"
        " ingest_run_id, chunking_version, embedding_version) "
        "OVERRIDING SYSTEM VALUE VALUES (1,1,'paragraph','',1,'内容',true,'CN.A','quarterly',"
        "'2024-10-28 10:00+00','2024-07-01','2024-10-28 10:00+00','mock','r1',"
        "'chunker:v1+aaa','embed:siliconflow-bge-m3')"
    )
    c.commit()
    return c


@pytest.mark.db
def test_freeze_captures_block_ids_and_versions(conn: psycopg.Connection) -> None:
    freeze = create_freeze(conn, AS_OF)

    assert freeze.block_ids == (1,)
    assert freeze.chunking_versions == ("chunker:v1+aaa",)
    assert freeze.embedding_versions == ("embed:siliconflow-bge-m3",)


@pytest.mark.db
def test_replaying_the_same_as_of_three_times_is_identical(conn: psycopg.Connection) -> None:
    """H5：冻结快照后重跑三次，结果完全一致。"""
    first = create_freeze(conn, AS_OF)
    second = create_freeze(conn, AS_OF)
    third = create_freeze(conn, AS_OF)

    # created_at 是这次调用的落地时刻，理应每次不同；真正要重放一致的是
    # 观测到的语料本身。
    for f in (first, second, third):
        assert f.block_ids == first.block_ids
        assert f.chunking_versions == first.chunking_versions
        assert f.embedding_versions == first.embedding_versions


@pytest.mark.db
def test_save_and_load_round_trips(conn: psycopg.Connection, tmp_path: Path) -> None:
    freeze = create_freeze(conn, AS_OF)
    path = tmp_path / "freeze.json"

    save_freeze(freeze, path)
    loaded = load_freeze(path)

    assert loaded == freeze


@pytest.mark.db
def test_diff_is_empty_when_corpus_is_unchanged(conn: psycopg.Connection) -> None:
    freeze = create_freeze(conn, AS_OF)

    assert diff_against_current_corpus(conn, freeze) == []


@pytest.mark.db
def test_diff_reports_a_newly_added_block_within_the_frozen_as_of_window(
    conn: psycopg.Connection,
) -> None:
    """一份新文档以历史 known_at（早于 AS_OF）补录进来——这在真实世界里会
    发生（迟报、回填），冻结快照必须能发现这种"语料悄悄变了"的情况，
    不能被"as_of 没变"误导成一切照旧。"""
    freeze = create_freeze(conn, AS_OF)

    conn.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at, source,"
        " content_hash, version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (2,'CN.A','quarterly','t2','2024-09-01 10:00+00',"
        "'mock','h2',2,'2024-07-01','2024-09-01 10:00+00','r1')"
    )
    conn.execute(
        "INSERT INTO core.doc_block (block_id, doc_id, block_type, section_path, ordinal,"
        " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at, source,"
        " ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (2,2,'paragraph','',1,'补录内容',true,'CN.A',"
        "'quarterly','2024-09-01 10:00+00','2024-07-01','2024-09-01 10:00+00','mock','r1')"
    )
    conn.commit()

    diffs = diff_against_current_corpus(conn, freeze)

    assert any("新增了" in d for d in diffs)


@pytest.mark.db
def test_freeze_rejects_naive_datetime(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="时区"):
        create_freeze(conn, datetime(2024, 12, 31))
