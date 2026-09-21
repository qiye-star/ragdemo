"""测试夹具：为每个需要数据库的测试提供一个全新的临时库。"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

# `corpus` / `as_of_2024` 原本放在 tests/retrieval/conftest.py 里，
# 但 conftest 的夹具只对该目录子树可见——兄弟目录 tests/evals/ 与仓库根的
# tests/test_p1_acceptance.py 都取不到，检索评测和 P1 验收正好都要用它。
# 改成模块 tests/corpus.py + 在根 conftest 注册为插件，全套测试都能看见。
pytest_plugins = ["tests.corpus"]

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
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _dsn_for(name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
