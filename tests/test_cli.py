"""CLI：三个子命令存在，且 check 在缺少 DSN 时以非零码退出。"""

from __future__ import annotations

from click.testing import CliRunner

from ragdemo.cli import main


def test_db_subcommands_exist() -> None:
    result = CliRunner().invoke(main, ["db", "--help"])
    assert result.exit_code == 0
    for cmd in ("migrate", "seed", "check"):
        assert cmd in result.output


def test_check_requires_dsn() -> None:
    result = CliRunner().invoke(main, ["db", "check"], env={"RAGDEMO_DSN": ""})
    assert result.exit_code != 0
    assert "RAGDEMO_DSN" in result.output
