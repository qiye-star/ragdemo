"""测试夹具：为每个需要数据库的测试提供一个全新的临时库。"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

# 用 127.0.0.1 而不是 localhost：compose 只绑回环 IPv4，而 localhost 在本机
# 先解析到 ::1，psycopg 会在那里卡满整个 connect 超时才回退到 IPv4。
ADMIN_DSN = os.environ.get(
    "RAGDEMO_ADMIN_DSN", "postgresql://postgres:ragdemo@127.0.0.1:5433/postgres"
)


def _dsn_for(database: str) -> str:
    base, _, _ = ADMIN_DSN.rpartition("/")
    return f"{base}/{database}"


@pytest.fixture
def temp_db() -> Iterator[str]:
    """创建一个随机命名的空库，测试结束后强制删除。"""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')  # noqa: S608
    try:
        yield _dsn_for(name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')  # noqa: S608
