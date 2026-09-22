"""时点泄漏抽样重放：确定性抽样 + 篡改检测。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.quality.replay import replay_and_check, sample_block_ids
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
PARTITION = date(2026, 1, 15)
BASE_PUBLISH_AT = datetime(2024, 10, 28, 10, 0, tzinfo=UTC)


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    c.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at,"
        " source, content_hash, version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (1,'CN.688256','quarterly','t',%s,"
        "'mock','h1',1,'2024-07-01',%s,'r1')",
        (BASE_PUBLISH_AT, BASE_PUBLISH_AT),
    )
    c.commit()
    return c


def _add_block(
    conn: psycopg.Connection,
    *,
    block_id: int,
    known_at: datetime,
    superseded_at: datetime | None = None,
    content: str = "内容",
) -> None:
    conn.execute(
        "INSERT INTO core.doc_block (block_id, doc_id, block_type, section_path, ordinal,"
        " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at,"
        " superseded_at, source, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (%s,1,'paragraph','',%s,%s,true,'CN.688256',"
        "'quarterly',%s,'2024-07-01',%s,%s,'mock','r1')",
        (block_id, block_id, content, BASE_PUBLISH_AT, known_at, superseded_at),
    )
    conn.commit()


# --- sample_block_ids ---------------------------------------------------------


@pytest.mark.db
def test_sample_is_deterministic_for_the_same_partition_date(conn: psycopg.Connection) -> None:
    for i in range(1, 11):
        _add_block(conn, block_id=i, known_at=BASE_PUBLISH_AT)

    first = sample_block_ids(conn, PARTITION, size=5)
    second = sample_block_ids(conn, PARTITION, size=5)
    assert first == second
    assert len(first) == 5


@pytest.mark.db
def test_sample_size_is_capped_to_the_available_pool(conn: psycopg.Connection) -> None:
    for i in range(1, 4):
        _add_block(conn, block_id=i, known_at=BASE_PUBLISH_AT)

    assert len(sample_block_ids(conn, PARTITION, size=200)) == 3


@pytest.mark.db
def test_sample_is_empty_with_no_blocks(conn: psycopg.Connection) -> None:
    assert sample_block_ids(conn, PARTITION, size=10) == []


# --- 基线记录与一致性 ----------------------------------------------------------


@pytest.mark.db
def test_first_replay_records_a_baseline_with_no_mismatch(conn: psycopg.Connection) -> None:
    _add_block(conn, block_id=1, known_at=BASE_PUBLISH_AT)

    mismatches = replay_and_check(conn, PARTITION, size=10)

    assert mismatches == []
    (n,) = conn.execute("SELECT count(*) FROM quality.asof_replay_baseline").fetchone()  # type: ignore[misc]
    assert n > 0


@pytest.mark.db
def test_repeated_replay_with_no_changes_stays_consistent(conn: psycopg.Connection) -> None:
    """连续多次重放、数据不变，一致率应该恒为 100%（E5）。"""
    _add_block(conn, block_id=1, known_at=BASE_PUBLISH_AT)

    for _ in range(3):
        assert replay_and_check(conn, PARTITION, size=10) == []


@pytest.mark.db
def test_a_corrected_document_produces_no_mismatch(conn: psycopg.Connection) -> None:
    """走合法更正路径（新块 known_at 更晚，旧块标 superseded_at）不该被
    误判成篡改——更正是被允许的状态变化，篡改不是。"""
    _add_block(
        conn,
        block_id=1,
        known_at=BASE_PUBLISH_AT,
        superseded_at=datetime(2024, 11, 1, tzinfo=UTC),
    )
    assert replay_and_check(conn, PARTITION, size=10) == []
    assert replay_and_check(conn, PARTITION, size=10) == []


# --- 篡改检测（核心场景） -------------------------------------------------------


@pytest.mark.db
def test_moving_known_at_earlier_after_baseline_is_detected(conn: psycopg.Connection) -> None:
    """把 known_at 悄悄改早，让块在一个本该看不见的历史 as_of 下冒出来——
    这正是"未来信息泄漏进历史"的篡改，必须被抓住（阶段 E 的核心场景）。

    探测点在第一次抽中时就固定并存进基线表，不会随 known_at 篡改而跟着
    偏移——这是本模块与"直接从当前 known_at 现算探测点"的实现相比刻意
    修正过的一处（见 quality/replay.py 的 _initial_probe_moments 与
    _existing_probe_moments）。
    """
    _add_block(conn, block_id=1, known_at=datetime(2024, 10, 28, 20, 0, tzinfo=UTC))
    baseline = replay_and_check(conn, PARTITION, size=10)
    assert baseline == []

    conn.execute(
        "UPDATE core.doc_block SET known_at = %s WHERE block_id = 1",
        (datetime(2024, 10, 28, 10, 0, tzinfo=UTC),),
    )
    conn.commit()

    mismatches = replay_and_check(conn, PARTITION, size=10)
    assert any(m.block_id == 1 for m in mismatches)


@pytest.mark.db
def test_editing_content_in_place_after_baseline_is_detected(conn: psycopg.Connection) -> None:
    """内容被就地改写（没有走"新增一行、旧行标 superseded_at"这条唯一
    合法的更正路径）——同一个 as_of 下重放出的哈希必须变。"""
    _add_block(conn, block_id=1, known_at=BASE_PUBLISH_AT)
    assert replay_and_check(conn, PARTITION, size=10) == []

    conn.execute("UPDATE core.doc_block SET content = '被篡改的内容' WHERE block_id = 1")
    conn.commit()

    mismatches = replay_and_check(conn, PARTITION, size=10)
    assert any(m.block_id == 1 for m in mismatches)


@pytest.mark.db
def test_mismatch_reports_baseline_and_replayed_hashes(conn: psycopg.Connection) -> None:
    _add_block(conn, block_id=1, known_at=BASE_PUBLISH_AT)
    replay_and_check(conn, PARTITION, size=10)

    conn.execute("UPDATE core.doc_block SET content = '被篡改的内容' WHERE block_id = 1")
    conn.commit()

    (mismatch,) = [m for m in replay_and_check(conn, PARTITION, size=10) if m.block_id == 1]
    assert mismatch.baseline_hash != mismatch.replayed_hash
    assert len(mismatch.baseline_hash) == 64
    assert len(mismatch.replayed_hash) == 64
