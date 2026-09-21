"""迁移执行器：顺序执行、幂等、拒绝修改已执行的迁移。"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import MigrationChecksumMismatch, discover, migrate


def _write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


@pytest.mark.db
def test_applies_migrations_in_lexical_order(temp_db: str, tmp_path: Path) -> None:
    _write(tmp_path, "002_second.sql", "CREATE TABLE b (id int);")
    _write(tmp_path, "001_first.sql", "CREATE TABLE a (id int);")

    with psycopg.connect(temp_db) as conn:
        applied = migrate(conn, tmp_path)

    assert applied == ["001_first", "002_second"]


@pytest.mark.db
def test_rerun_is_a_noop(temp_db: str, tmp_path: Path) -> None:
    _write(tmp_path, "001_first.sql", "CREATE TABLE a (id int);")

    with psycopg.connect(temp_db) as conn:
        assert migrate(conn, tmp_path) == ["001_first"]
        assert migrate(conn, tmp_path) == []


@pytest.mark.db
def test_modifying_an_applied_migration_raises(temp_db: str, tmp_path: Path) -> None:
    """迁移只加不改（docs/11-sdlc.md §4.2）。改了要能被发现。"""
    _write(tmp_path, "001_first.sql", "CREATE TABLE a (id int);")
    with psycopg.connect(temp_db) as conn:
        migrate(conn, tmp_path)

    _write(tmp_path, "001_first.sql", "CREATE TABLE a (id bigint);")
    with psycopg.connect(temp_db) as conn, pytest.raises(MigrationChecksumMismatch):
        migrate(conn, tmp_path)


@pytest.mark.db
def test_failed_migration_rolls_back_entirely(temp_db: str, tmp_path: Path) -> None:
    """一条迁移内部失败，它的前半部分也不得留下痕迹。"""
    _write(tmp_path, "001_bad.sql", "CREATE TABLE ok (id int); CREATE TABLE ok (id int);")

    with psycopg.connect(temp_db) as conn, pytest.raises(psycopg.errors.DuplicateTable):
        migrate(conn, tmp_path)

    with psycopg.connect(temp_db) as conn:
        row = conn.execute("SELECT to_regclass('public.ok') IS NOT NULL").fetchone()
    assert row is not None
    assert row[0] is False


def test_discover_ignores_non_sql_files(tmp_path: Path) -> None:
    _write(tmp_path, "001_first.sql", "SELECT 1;")
    (tmp_path / "README.md").write_text("notes", encoding="utf-8")
    assert [m.version for m in discover(tmp_path)] == ["001_first"]


@pytest.mark.db
def test_successful_migrations_are_committed_before_a_later_one_fails(
    temp_db: str, tmp_path: Path
) -> None:
    """一条失败，它**之前**已成功的迁移必须留在库里。

    psycopg 的 conn.transaction() 开的是 SAVEPOINT 而不是顶层事务，
    不显式 commit 的话整批迁移会挤在同一个事务里，中途失败会把前面全部丢掉，
    而 schema_migrations 里又什么都没留下——重跑时无从判断进度。
    """
    _write(tmp_path, "001_good.sql", "CREATE TABLE good (id int);")
    _write(tmp_path, "002_bad.sql", "CREATE TABLE bad (id int); CREATE TABLE bad (id int);")

    # 异常必须穿过连接的 with，才是 CLI 里真实的路径：
    # 连接上下文退出时看到异常会回滚整个外层事务。
    with pytest.raises(psycopg.errors.DuplicateTable), psycopg.connect(temp_db) as conn:
        migrate(conn, tmp_path)

    with psycopg.connect(temp_db) as conn:
        good = conn.execute("SELECT to_regclass('public.good') IS NOT NULL").fetchone()
        bad = conn.execute("SELECT to_regclass('public.bad') IS NOT NULL").fetchone()
        recorded = conn.execute("SELECT version FROM public.schema_migrations").fetchall()
    assert good is not None and good[0] is True
    assert bad is not None and bad[0] is False
    assert [r[0] for r in recorded] == ["001_good"]
