"""ragdemo 命令行入口。"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import click
import psycopg

from ragdemo.seed.loader import load_all
from ragdemo_core.db.invariants import check_point_in_time_leaks, check_schema_invariants
from ragdemo_core.db.migrate import migrate

MIGRATIONS_DIR = Path("db/migrations")
SEED_DIR = Path("db/seed")


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置（参见 .env.example）")
    return dsn


@click.group()
def main() -> None:
    """AI 产业链时点研究引擎。"""


@main.group()
def db() -> None:
    """数据库操作。"""


@db.command("migrate")
def db_migrate() -> None:
    """执行尚未执行的迁移。"""
    with psycopg.connect(_dsn()) as conn:
        applied = migrate(conn, MIGRATIONS_DIR)
    click.echo(f"已执行 {len(applied)} 条迁移: {applied}" if applied else "无待执行迁移")


@db.command("seed")
def db_seed() -> None:
    """导入种子数据。"""
    with psycopg.connect(_dsn()) as conn:
        counts = load_all(conn, SEED_DIR)
    for table, n in counts.items():
        click.echo(f"{table}: {n}")


@db.command("check")
def db_check() -> None:
    """跑 schema 不变量与时点泄漏自检；有问题以非零码退出。"""
    with psycopg.connect(_dsn()) as conn:
        violations = check_schema_invariants(conn)
        leaks = check_point_in_time_leaks(conn, datetime.now(UTC))

    for v in violations:
        click.echo(f"[不变量] {v}", err=True)
    for name, n in leaks.items():
        click.echo(f"[时点泄漏] {name}: {n} 行", err=True)

    if violations or leaks:
        sys.exit(1)
    click.echo("全部检查通过")
