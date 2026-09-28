"""迁移 021：公告目录表 `core.announcement_index`（公告双源接入计划 Task 2）。

每份公告一行、不论来源；跨源去重/取代靠上层的 `IndexWriter`（Task 3），这里只守
表结构本身的约束：时点表登记、app_read 只能经 asof 视图读、时序与唯一性约束。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.invariants import check_schema_invariants
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        c.execute(
            "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer, l2_segment,"
            " l3_node, primary_node) VALUES ('CN.688041', '海光信息', 'listed', '算力', 'AI芯片',"
            " ARRAY['通用服务器CPU'], '通用服务器CPU')"
        )
        c.commit()
        yield c
        c.close()


def _insert(
    conn: psycopg.Connection,
    *,
    source: str = "cninfo",
    source_ref: str = "1",
    known_at: datetime = NOW,
    superseded_at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO core.announcement_index (entity_id, source, source_ref, title, title_norm,"
        " publish_date, has_full_text, valid_from, known_at, superseded_at, ingest_run_id)"
        " VALUES ('CN.688041', %s, %s, '公告', '公告', %s, true, %s, %s, %s, 't')",
        (source, source_ref, known_at.date(), known_at.date(), known_at, superseded_at),
    )


@pytest.mark.db
def test_migration_leaves_no_schema_invariant_violations(conn: psycopg.Connection) -> None:
    assert check_schema_invariants(conn) == []


@pytest.mark.db
def test_app_read_has_the_view_but_not_the_base_table(conn: psycopg.Connection) -> None:
    """`check_schema_invariants`（上面那条测试）已经通用地验证了"app_read 对登记在册的
    时点表无 SELECT"；这里补的是它的正面：app_read 确实能读 asof 视图，不是两边都锁死。"""
    base = conn.execute(
        "SELECT has_table_privilege('app_read', 'core.announcement_index', 'SELECT')"
    ).fetchone()
    view = conn.execute(
        "SELECT has_table_privilege('app_read', 'asof.announcement_index', 'SELECT')"
    ).fetchone()
    assert base == (False,) and view == (True,)


@pytest.mark.db
def test_superseded_at_before_known_at_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(conn, known_at=NOW, superseded_at=NOW - timedelta(days=1))


@pytest.mark.db
def test_two_live_rows_with_the_same_source_and_ref_are_rejected(
    conn: psycopg.Connection,
) -> None:
    _insert(conn, source_ref="1")
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(conn, source_ref="1")


@pytest.mark.db
def test_a_superseded_row_does_not_block_a_new_live_one_with_the_same_ref(
    conn: psycopg.Connection,
) -> None:
    """取代旧行时先 UPDATE superseded_at 再 INSERT 新行——这条测试确保部分唯一索引
    真的只约束"现行"行，不是整张表按 (source, source_ref) 唯一。"""
    _insert(conn, source_ref="1", superseded_at=None)
    conn.commit()
    conn.execute(
        "UPDATE core.announcement_index SET superseded_at = %s"
        " WHERE source = 'cninfo' AND source_ref = '1' AND superseded_at IS NULL",
        (NOW + timedelta(hours=1),),
    )
    _insert(conn, source_ref="1", known_at=NOW + timedelta(hours=1))
    conn.commit()


@pytest.mark.db
def test_unknown_source_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(conn, source="不存在的来源")
