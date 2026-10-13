# ruff: noqa: E501
# tests/db/test_migration_034.py
"""迁移 034：台账两表的 CHECK 守住"跳页必有规则、只有 L3 计费、L0 免费、页数守恒"。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate

INSERT = (
    "INSERT INTO core.parse_page_route (routes_ref, content_hash, doc_type, page, tier, engine, page_kind,"
    " skip_rule, textin_billed, tier_verdict, p3_state, cost_cny, ingest_run_id, routed_at)"
    " VALUES ('r', 'h', 'announcement', %s, %s, 'e', 'plain_text', %s, %s, %s, %s, %s, 't', now())"
)


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, Path("db/migrations"))
        yield c


@pytest.mark.db
@pytest.mark.parametrize(
    "row",
    [
        (1, "skipped", None, False, "not_run", None, 0),  # 跳页没有规则
        (1, "L0", None, True, "passed", "pass", 0),  # L0 计费
        (1, "L0", None, False, "passed", "pass", None),  # L0 费用未知
        (1, "L3", None, False, "not_run", None, 0),  # 非跳页没有 P3
        (1, "L1", None, False, "passed", "pass", 0),  # 不存在的层级
    ],
)
def test_ledger_checks_reject_inconsistent_rows(
    conn: psycopg.Connection, row: tuple[object, ...]
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(INSERT, row)


@pytest.mark.db
def test_doc_acceptance_pages_must_add_up(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO core.parse_doc_acceptance (routes_ref, content_hash, doc_type, state,"
            " unverified_numbers, scan_pages, page_count, l0_pages, l3_pages, skipped_pages, billed_pages,"
            " acceptance_version, ingest_run_id, evaluated_at)"
            " VALUES ('r', 'h', 'announcement', 'accepted', 0, 0, 3, 1, 1, 0, 1, 'acc-v2', 't', now())"
        )
