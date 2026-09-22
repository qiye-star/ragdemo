"""tests/api/ 共享夹具：真实数据库 + 被 GRANT app_diag 的登录用户 + TestClient。

与 tests/db/test_asof_layer.py 同一套范式（临时库 + 独立登录用户名 + 结束时
DROP OWNED BY/DROP USER），针对新角色 app_diag、并额外包一层 TestClient。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from ragdemo.api.app import create_app
from ragdemo.api.settings import ApiSettings
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

# 测试库里的一次性口令，不是任何环境的真实凭据。
APITEST_PASSWORD = "not-a-real-secret"


@pytest.fixture
def api_db(temp_db: str) -> Iterator[tuple[str, str]]:
    """迁移一个临时库，建一个被 GRANT app_diag 的登录用户。

    产出 (admin_dsn, api_dsn)：前者仍是超级用户，供测试自己写种子数据；
    后者是诊断接口实际会用的身份。角色是集群级对象，不随临时库消失，
    所以用随机用户名，并在 finally 里显式清理。
    """
    user = f"apidiag_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(temp_db) as admin:
        migrate(admin, MIGRATIONS)
        admin.execute(
            sql.SQL("CREATE USER {} PASSWORD {}").format(
                sql.Identifier(user), sql.Literal(APITEST_PASSWORD)
            )
        )
        admin.execute(sql.SQL("GRANT app_diag TO {}").format(sql.Identifier(user)))
        admin.commit()
    api_dsn = psycopg.conninfo.make_conninfo(temp_db, user=user, password=APITEST_PASSWORD)
    try:
        yield temp_db, api_dsn
    finally:
        with psycopg.connect(temp_db, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(user)))
            admin.execute(sql.SQL("DROP USER IF EXISTS {}").format(sql.Identifier(user)))


@pytest.fixture
def client(api_db: tuple[str, str]) -> Iterator[TestClient]:
    _, api_dsn = api_db
    app = create_app(ApiSettings(dsn=api_dsn, set_role=None, web_root=None))
    with TestClient(app) as c:
        yield c
