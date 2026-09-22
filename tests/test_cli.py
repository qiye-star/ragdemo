"""CLI：子命令存在，且 check/serve/grant-api-read 在缺少必要配置时以
非零码退出并给出可操作的错误信息（不是静默失败或猜一个默认值）。
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner
from psycopg import sql

from ragdemo.cli import main
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


def test_db_subcommands_exist() -> None:
    result = CliRunner().invoke(main, ["db", "--help"])
    assert result.exit_code == 0
    for cmd in (
        "migrate",
        "seed",
        "check",
        "grant-api-read",
        "seed-isolation-demo",
        "clear-isolation-demo",
    ):
        assert cmd in result.output


def test_top_level_serve_command_exists() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output


def test_check_requires_dsn() -> None:
    result = CliRunner().invoke(main, ["db", "check"], env={"RAGDEMO_DSN": ""})
    assert result.exit_code != 0
    assert "RAGDEMO_DSN" in result.output


def test_serve_refuses_non_loopback_host() -> None:
    result = CliRunner().invoke(
        main, ["serve", "--host", "0.0.0.0"], env={"RAGDEMO_API_ALLOW_LAN": ""}
    )
    assert result.exit_code != 0
    assert "RAGDEMO_API_ALLOW_LAN" in result.output


def test_grant_api_read_requires_password_env() -> None:
    result = CliRunner().invoke(
        main,
        ["db", "grant-api-read", "--user", "someone"],
        env={"RAGDEMO_DSN": "postgresql://x/y", "RAGDEMO_API_DB_PASSWORD": ""},
    )
    assert result.exit_code != 0
    assert "RAGDEMO_API_DB_PASSWORD" in result.output


@pytest.mark.db
def test_grant_api_read_creates_a_login_user_with_app_diag(temp_db: str) -> None:
    """端到端：跑完这条命令，产出的登录用户必须真的能连接、真的是
    app_diag 成员，且输出里不出现口令本身（约束：密钥只在环境变量）。
    """
    with psycopg.connect(temp_db) as admin:
        migrate(admin, MIGRATIONS)
        admin.commit()

    password = "smoketest-password-not-a-real-secret"
    user = "apitest_grant_cli"
    result = CliRunner().invoke(
        main,
        ["db", "grant-api-read", "--user", user],
        env={"RAGDEMO_DSN": temp_db, "RAGDEMO_API_DB_PASSWORD": password},
    )
    try:
        assert result.exit_code == 0, result.output
        assert password not in result.output

        with psycopg.connect(temp_db) as admin:
            (is_member,) = admin.execute(
                "SELECT pg_has_role(%s, 'app_diag', 'member')", (user,)
            ).fetchone()  # type: ignore[misc]
            assert is_member is True

        # 真的能用这个口令连接——不只是角色关系对了，口令本身也生效。
        conn = psycopg.connect(temp_db, user=user, password=password)
        conn.close()

        # 幂等：第二次跑（口令换一个）应该是 ALTER 而不是报错。
        result2 = CliRunner().invoke(
            main,
            ["db", "grant-api-read", "--user", user],
            env={"RAGDEMO_DSN": temp_db, "RAGDEMO_API_DB_PASSWORD": password + "-v2"},
        )
        assert result2.exit_code == 0, result2.output
    finally:
        with psycopg.connect(temp_db, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(user)))
            admin.execute(sql.SQL("DROP USER IF EXISTS {}").format(sql.Identifier(user)))
