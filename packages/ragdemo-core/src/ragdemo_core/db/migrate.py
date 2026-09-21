"""极简 SQL 迁移执行器。

设计取舍：不用 Alembic。本项目的 schema 是手写 DDL（含 ParadeDB 专有的
BM25 索引选项），ORM 迁移工具的自动生成能力用不上，反而增加一层抽象。
换来的是「迁移只加不改」可以用校验和强制（docs/11-sdlc.md §4.2）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version    text PRIMARY KEY,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationChecksumMismatch(RuntimeError):
    """已执行的迁移文件内容被修改了。"""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path) -> list[Migration]:
    """按文件名字典序返回目录下的全部 .sql 迁移。"""
    return [
        Migration(version=p.stem, path=p, sql=p.read_text(encoding="utf-8"))
        for p in sorted(directory.glob("*.sql"))
    ]


def _applied(conn: psycopg.Connection[tuple[object, ...]]) -> dict[str, str]:
    with conn.transaction():
        conn.execute(_MIGRATIONS_TABLE)
    rows = conn.execute("SELECT version, checksum FROM public.schema_migrations").fetchall()
    return {str(v): str(c) for v, c in rows}


def migrate(conn: psycopg.Connection[tuple[object, ...]], directory: Path) -> list[str]:
    """执行尚未执行的迁移，返回本次新执行的 version 列表。

    每条迁移在独立事务中执行：失败则整条回滚，不留半截 schema。
    """
    already = _applied(conn)
    newly: list[str] = []
    for m in discover(directory):
        if m.version in already:
            if already[m.version] != m.checksum:
                raise MigrationChecksumMismatch(
                    f"迁移 {m.version} 已执行但文件内容被修改。迁移只加不改，请新增一条迁移来纠正。"
                )
            continue
        with conn.transaction():
            # 整个文件作为一条语句交给服务端：多条 DDL 之间的原子性由这层事务保证。
            conn.execute(m.sql)
            conn.execute(
                "INSERT INTO public.schema_migrations (version, checksum) VALUES (%s, %s)",
                (m.version, m.checksum),
            )
        newly.append(m.version)
    return newly
