"""ragdemo 命令行入口。"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import click
import psycopg

from ragdemo.evals.cli import eval_group
from ragdemo.ingest.cli import docs_group
from ragdemo.quality.metrics import MetricResult, record_metric
from ragdemo.quality.replay import replay_and_check
from ragdemo.seed.isolation_demo import AlreadySeeded, clear_isolation_demo, seed_isolation_demo
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


main.add_command(eval_group)
main.add_command(docs_group)


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


@db.command("replay-check")
@click.option(
    "--partition-date",
    "partition_date_str",
    default=None,
    help="按哪一天的种子抽样（YYYY-MM-DD）；不传则用今天。种子由这个日期派生，"
    "同一天重跑会抽中完全相同的一批块。",
)
def db_replay_check(partition_date_str: str | None) -> None:
    """时点泄漏抽样重放：200 条历史块 × 三个历史 as_of，比对哈希基线；不一致以非零码退出。

    首次遇到的 (block_id, as_of) 组合直接记基线，不算不一致——"没见过"和
    "见过但对不上"是两种不同的状态，只有后者才是泄漏信号。
    """
    partition_date = date.fromisoformat(partition_date_str) if partition_date_str else date.today()
    with psycopg.connect(_dsn()) as conn:
        mismatches = replay_and_check(conn, partition_date)
        record_metric(
            conn,
            partition_date,
            MetricResult(
                "asof_replay_mismatch_count",
                float(len(mismatches)),
                len(mismatches) == 0,
                threshold=0.0,
            ),
        )

    for m in mismatches:
        click.echo(
            f"[时点重放不一致] block_id={m.block_id} as_of={m.as_of.isoformat()} "
            f"baseline={m.baseline_hash} replayed={m.replayed_hash}",
            err=True,
        )
    if mismatches:
        sys.exit(1)
    click.echo("重放全部一致")


@db.command("seed-isolation-demo")
@click.option("--yes", is_flag=True, default=False, help="真正写库；不传则只 dry run")
@click.option("--force", is_flag=True, default=False, help="已有演示数据时先清空再重新写入")
def db_seed_isolation_demo(yes: bool, force: bool) -> None:
    """插入权限隔离演示种子：公共对照 + 租户私有 + 用户 A/B 私有各一份文档，
    供诊断界面的隔离探针矩阵（adr/0010）有真实数据可看。只在回环地址的
    数据库上生效——拒绝把合成的"用户私有材料"写进共享/远程库。"""
    dsn = _dsn()
    if not yes:
        click.echo("[dry run] 不传 --yes 不会真正写库。将插入 4 份合成文档，各带 1 个块")
        return
    with psycopg.connect(dsn) as conn:
        try:
            doc_ids = seed_isolation_demo(conn, dsn=dsn, force=force)
        except AlreadySeeded as exc:
            raise click.ClickException(str(exc)) from None
    for key, doc_id in doc_ids.items():
        click.echo(f"{key}: doc_id={doc_id}")


@db.command("clear-isolation-demo")
@click.option("--yes", is_flag=True, default=False, help="真正删除；不传则只 dry run")
def db_clear_isolation_demo(yes: bool) -> None:
    """物理删除全部隔离演示种子数据（`source = 'isolation-demo'` 的文档与块）。"""
    dsn = _dsn()
    if not yes:
        click.echo("[dry run] 不传 --yes 不会真正删除")
        return
    with psycopg.connect(dsn) as conn:
        clear_isolation_demo(conn, dsn=dsn)
    click.echo("已清空隔离演示种子数据")
