"""`ragdemo facts`：结构化财务事实入库（Wind MCP，总计划阶段 2.1）。"""

from __future__ import annotations

import os
from datetime import date, datetime

import click
import psycopg

from ragdemo.adapters.mcp_gateway import gateway_client_from_env
from ragdemo.adapters.wind import (
    WIND_FIELDS,
    WindFundamentalsAdapter,
    build_question,
    quarter_ends_back,
)
from ragdemo.config import load_config
from ragdemo.ingest.wind_facts import a_share_windcodes, ingest_wind_facts
from ragdemo.sources.backlog import Backlog
from ragdemo.sources.health import PgSourceHealth
from ragdemo_core.blob import LocalBlobStore

WIND_URL_ENV = "RAGDEMO_WIND_MCP_URL"


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置（参见 .env.example）")
    return dsn


@click.group("facts")
def facts_group() -> None:
    """结构化财务事实（Wind）。"""


@facts_group.command("ingest")
@click.option("--quarters", default=12, show_default=True, type=int, help="往回取多少个季度")
@click.option("--entity-ref", "entity_refs", multiple=True, help="只取这些代码（如 688041.SH）")
@click.option(
    "--as-of-date",
    "as_of_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="以哪一天为基准计算最近的季度（默认今天）",
)
@click.option("--yes", is_flag=True, default=False, help="真正调用 Wind 并写库；不传只 dry run")
def ingest_command(
    quarters: int, entity_refs: tuple[str, ...], as_of_date: datetime | None, yes: bool
) -> None:
    """从 Wind 取三大报表科目写入 core.fin_fact。默认 dry run：只列出要问的问题。"""
    on = as_of_date.date() if as_of_date is not None else date.today()
    with psycopg.connect(_dsn()) as conn:
        codes = list(entity_refs) or a_share_windcodes(conn)
        if not codes:
            raise click.ClickException(
                "没有可取数的 A 股实体（core.entity 里 tushare_code 为空？）"
            )
        ends = quarter_ends_back(on, quarters)
        click.echo(f"{len(codes)} 个实体 × {quarters} 个季度（{ends[0]} ~ {ends[-1]}）")
        if not yes:
            click.echo(f"[dry run] 示例问句：{build_question(codes[0], ends, tuple(WIND_FIELDS))}")
            click.echo("[dry run] 未调用 Wind、未写库；加 --yes 执行")
            return

        gateway = gateway_client_from_env(url_env=WIND_URL_ENV, provider="wind-mcp")
        adapter = WindFundamentalsAdapter(gateway, windcodes=codes, quarters=quarters)
        blob = LocalBlobStore(load_config().blob_root)
        # 冷却与待补清单走独立的 autocommit 连接：入库回滚撤销不了它们（ADR-0012）。
        with psycopg.connect(_dsn(), autocommit=True) as state:
            report = ingest_wind_facts(
                conn,
                adapter,
                blob,
                windcodes=codes,
                partition_date=on,
                ingest_run_id="cli:facts-ingest",
                quarters=quarters,
                health=PgSourceHealth(state),
                backlog=Backlog(state),
            )
    click.echo(f"写入 {report.written} 条，与库中相同跳过 {report.skipped} 条")
    for code, reason in report.backlogged.items():
        click.echo(f"[待补] {code}: {reason}", err=True)
    for warning in report.warnings:
        click.echo(f"[未入库] {warning}", err=True)
    for code, reason in report.failed.items():
        click.echo(f"[失败] {code}: {reason}", err=True)
    if report.failed or report.backlogged:
        raise click.ClickException(
            f"{len(set(report.failed) | set(report.backlogged))} 个实体未能入库，见上方原因"
            "（进了待补清单的，下次运行会自动先补）"
        )
