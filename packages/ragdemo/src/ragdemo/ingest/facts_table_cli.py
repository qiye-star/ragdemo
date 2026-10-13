"""`ragdemo facts table-check`：Wind 事实 ↔ 已解析表格单元格逐格校验（表格处理改进计划 T18）。

默认 dry run：只读（连接设为 read_only），不碰 `core.fact_table_ref`（迁移 031 可以还没跑），
只打印统计与清单；`--yes` 才写 `core.fact_table_ref`。**任何情况下都不修改 `core.fin_fact`。**
"""

from __future__ import annotations

import os
import uuid

import click
import psycopg

from ragdemo.tables.fact_check import (
    FactCheckConfig,
    FactCheckReport,
    compare_with_legacy,
    run_fact_table_check,
)


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置（参见 .env.example）")
    return dsn


def _echo_report(report: FactCheckReport, *, limit: int) -> None:
    click.echo(
        f"事实 {len(report.results)} 条；候选表 {report.tables_considered} 张，"
        f"其中有命中的 {report.tables_with_hits} 张"
    )
    for verdict, n in sorted(report.counts().items()):
        click.echo(f"  {verdict}: {n}")
    for (verdict, reason), n in sorted(report.reasons().items()):
        click.echo(f"    {verdict}/{reason or '-'}: {n}")
    for kind, (m, n) in sorted(report.by_period_kind().items()):
        click.echo(f"  期别 {kind}: match {m} / 已定位 {n}" + (f" = {m / n:.3f}" if n else ""))
    bad = [r for r in report.results if r.verdict == "mismatch"]
    for r in bad[:limit]:
        diff = (
            None
            if r.report_value is None or r.fact_value is None
            else r.report_value - r.fact_value
        )
        click.echo(
            f"[mismatch] {r.entity_id} {r.period} {r.metric_id} 块{r.block_id} p{r.page} "
            f"r{r.row}c{r.col} 列[{r.period_label}] 表值={r.report_value} 事实={r.fact_value}"
            f" 差={diff}"
            f" ({r.reason})"
        )
    if len(bad) > limit:
        click.echo(f"[mismatch] 另有 {len(bad) - limit} 条未列出（--show 调大）")


@click.command("table-check")
@click.option(
    "--strict-unit",
    is_flag=True,
    default=False,
    help="不采信由前文块绑定的表单位（人工抽查 U5 发现单位绑定有误时使用）",
)
@click.option("--show", default=50, show_default=True, type=int, help="最多列出多少条 mismatch")
@click.option("--compare-legacy", is_flag=True, default=False, help="对比旧 crosscheck_facts")
@click.option("--yes", is_flag=True, default=False, help="写入 core.fact_table_ref；不传只 dry run")
def table_check_command(strict_unit: bool, show: int, compare_legacy: bool, yes: bool) -> None:
    """逐格校验 Wind 事实与公告表格单元格（只写 core.fact_table_ref，不改任何事实）。"""
    config = FactCheckConfig(allow_preceding_unit=not strict_unit)
    with psycopg.connect(_dsn()) as conn:
        if not yes:
            conn.read_only = True
        report = run_fact_table_check(
            conn,
            ingest_run_id=f"cli:facts-table-check:{uuid.uuid4().hex[:8]}",
            write=yes,
            config=config,
        )
        _echo_report(report, limit=show)
        if compare_legacy:
            cmp = compare_with_legacy(conn, report.results)
            click.echo(
                f"旧法：核对 {cmp.legacy_checked} 条，matched {cmp.legacy_matched}；"
                f"新旧都 match {cmp.both_matched}；旧 matched 而新非 match {len(cmp.legacy_only)}"
            )
            for entity, period, metric, verdict in cmp.legacy_only[:show]:
                click.echo(f"  [旧matched/新{verdict}] {entity} {period} {metric}")
    if yes:
        click.echo(f"写入 {report.written} 行，取代 {report.superseded} 行")
    else:
        click.echo("[dry run] 未写库；加 --yes 写入 core.fact_table_ref")
