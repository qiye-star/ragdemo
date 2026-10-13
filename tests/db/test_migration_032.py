"""迁移 032：asof.doc_table 按块查找走 (block_id, known_at) 非偏索引，而不是全表扫描。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate
from ragdemo_core.db.session import as_of_session

MIGRATIONS = Path("db/migrations")


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        yield c


@pytest.mark.db
def test_index_is_not_partial(conn: psycopg.Connection) -> None:
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'core' AND tablename = 'doc_table'"
        " AND indexname = 'doc_table_block_known'"
    ).fetchone()
    assert row is not None
    assert "block_id, known_at" in row[0]
    assert "WHERE" not in row[0]


@pytest.mark.db
def test_asof_lookup_by_block_uses_the_index(conn: psycopg.Connection) -> None:
    # 空表上规划器会选顺序扫描；关掉它，才能判断"有没有可用的索引路径"。
    conn.execute("SET enable_seqscan = off")
    with as_of_session(conn, datetime(2025, 11, 1, tzinfo=UTC)) as s:
        plan = "\n".join(
            r[0] for r in s.execute("EXPLAIN SELECT * FROM asof.doc_table WHERE block_id = 10")
        )
    assert "doc_table_block_known" in plan, plan
    assert "Seq Scan on doc_table" not in plan, plan
