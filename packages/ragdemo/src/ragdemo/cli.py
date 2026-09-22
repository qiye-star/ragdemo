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

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@main.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True, help="监听地址")
@click.option("--port", default=8088, show_default=True, type=int, help="监听端口")
def serve(host: str, port: int) -> None:
    """启动内网只读诊断接口（adr/0010）。只有 GET，没有写接口。

    只绑回环/内网是 ADR-0010 的硬约束，不是可选项——非回环地址需要显式
    打开 RAGDEMO_API_ALLOW_LAN 才放行，默认直接拒绝启动，而不是静默
    监听一个可能对外暴露的地址。
    """
    if host not in _LOOPBACK_HOSTS and not os.environ.get("RAGDEMO_API_ALLOW_LAN"):
        raise click.ClickException(
            f"--host {host} 不是回环地址；ADR-0010 要求只绑回环/内网。"
            "确需内网监听请设置环境变量 RAGDEMO_API_ALLOW_LAN=1。"
        )
    # 惰性 import：fastapi/uvicorn 的完整依赖图只有 serve 用得到，
    # 其余每一条 CLI 命令（db migrate/docs ingest/eval run 等）都不该
    # 为了这一条命令多背这份启动开销。
    import uvicorn

    from ragdemo.api.app import create_app
    from ragdemo.api.settings import SettingsError, from_env

    try:
        settings = from_env()
    except SettingsError as exc:
        raise click.ClickException(str(exc)) from None
    uvicorn.run(create_app(settings), host=host, port=port, log_config=None)


@main.group()
def db() -> None:
    """数据库操作。"""


@db.command("migrate")
def db_migrate() -> None:
    """执行尚未执行的迁移。"""
    with psycopg.connect(_dsn()) as conn:
        applied = migrate(conn, MIGRATIONS_DIR)
    click.echo(f"已执行 {len(applied)} 条迁移: {applied}" if applied else "无待执行迁移")


@db.command("grant-api-read")
@click.option("--user", "user", required=True, help="要创建/更新的登录用户名")
def db_grant_api_read(user: str) -> None:
    """建一个被 GRANT app_diag 的登录用户，供诊断接口的 RAGDEMO_API_DSN 使用。

    口令只从环境变量 RAGDEMO_API_DB_PASSWORD 读——不做 --password 选项：
    命令行参数会原样进 shell history 和 `ps`/任务管理器的进程列表。

    幂等：用户已存在就 ALTER 口令 + 补 GRANT，不存在就 CREATE。角色名与
    用户名走 psycopg.sql.Identifier（防止用户名里混进分号之类的东西），
    口令走 sql.Literal（CREATE/ALTER USER ... PASSWORD 不支持 %s 占位符，
    但 Literal 会正确转义引号）。

    警告：`log_statement = 'all'` 的库上，CREATE/ALTER USER ... PASSWORD
    会把明文口令写进 PostgreSQL 服务器日志。这条命令面向开发/内网库；
    生产库请改用 psql 的 \\password（客户端侧加密后发送，不经过这条日志）。
    """
    from psycopg import sql

    password = os.environ.get("RAGDEMO_API_DB_PASSWORD", "")
    if not password:
        raise click.ClickException("环境变量 RAGDEMO_API_DB_PASSWORD 未设置（参见 .env.example）")

    dsn = _dsn()
    with psycopg.connect(dsn) as conn:
        (exists,) = conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = %s", (user,)
        ).fetchone()  # type: ignore[misc]
        if exists:
            conn.execute(
                sql.SQL("ALTER USER {} PASSWORD {}").format(
                    sql.Identifier(user), sql.Literal(password)
                )
            )
        else:
            conn.execute(
                sql.SQL("CREATE USER {} PASSWORD {}").format(
                    sql.Identifier(user), sql.Literal(password)
                )
            )
        conn.execute(sql.SQL("GRANT app_diag TO {}").format(sql.Identifier(user)))
        conn.commit()

    # 口令绝不回显——只打印一条不含口令的连接串模板，运维自己把口令拼进
    # RAGDEMO_API_DSN。
    scheme = dsn.split("://", 1)[0] if "://" in dsn else "postgresql"
    click.echo(f"已{'更新' if exists else '创建'} {user!r} 并授予 app_diag。")
    click.echo(
        f"RAGDEMO_API_DSN={scheme}://{user}:<口令>@<host>:<port>/<dbname>（自行按实际环境填写）"
    )


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
