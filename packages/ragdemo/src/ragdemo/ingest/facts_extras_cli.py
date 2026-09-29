"""`ragdemo facts fx / holders / segments`：阶段二的三类结构化数据入库命令。

挂到 `facts_cli.facts_group` 上（见该文件末尾的注册）。默认 dry run：不调外部、不写库；
`--yes` 才执行。股东 / 分部的 dry run 会给出 Wind 调用次数上限，方便事先估额度。
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import click
import psycopg

from ragdemo.adapters.cfets import PAIRS, WINDOW_DAYS, CfetsAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import gateway_client_from_env
from ragdemo.adapters.snapshot_replay import SnapshotReplayGateway
from ragdemo.adapters.wind_structured import WindHoldersAdapter, WindSegmentsAdapter
from ragdemo.config import load_config
from ragdemo.ingest.structured_extras import (
    HOLDER_KINDS,
    SEGMENT_KINDS,
    IngestSummary,
    a_share_entities,
    ingest_fx,
    ingest_holders,
    ingest_segments,
    periods_back,
)
from ragdemo.ingest.structured_plan import plan_calls
from ragdemo_core.blob import LocalBlobStore

WIND_URL_ENV = "RAGDEMO_WIND_MCP_URL"


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置（参见 .env.example）")
    return dsn


def _cfets_adapter() -> CfetsAdapter:
    ua = os.environ.get(
        "RAGDEMO_CFETS_USER_AGENT", "Mozilla/5.0 (compatible; ragdemo/0.1; research)"
    )
    client = HttpClient(
        "cfets",
        "https://www.chinamoney.com.cn",
        policy=RetryPolicy(max_attempts=2, backoff_base_s=5.0),
        # 该站 WAF 对连续请求返回 403（2026-09-29 实测），保守限速。
        bucket=TokenBucket(rate_per_minute=6),
        default_headers={"User-Agent": ua},
    )
    return CfetsAdapter(client)


def echo_summary(summary: IngestSummary) -> None:
    click.echo(
        f"新增 {summary.inserted}，更正 {summary.revised}，无变化 {summary.unchanged}，"
        f"库里已有跳过 {summary.already_present}，Wind 调用 {summary.wind_calls} 次"
    )
    if summary.skipped_no_disclosure:
        click.echo(
            f"[未入库] {len(summary.skipped_no_disclosure)} 个期别在公告目录里找不到定期报告，"
            "没有可信 known_at，未调用 Wind",
            err=True,
        )
    for key in summary.unreconciled_to_fin_fact:
        click.echo(f"[未与 fin_fact 对账] {key}（库里没有该期营业收入）", err=True)
    for key, missing in summary.partial_dimensions.items():
        click.echo(
            f"[维度缺失] {key}：Wind 未返回 {'/'.join(missing)}，已入库的部分不受影响", err=True
        )
    for key, reason in summary.rejected.items():
        click.echo(f"[拒绝] {key}: {reason}", err=True)
    if summary.aborted:
        raise click.ClickException(summary.aborted)
    if summary.rejected:
        raise click.ClickException(f"{len(summary.rejected)} 项被拒绝，见上方原因")


@click.command("fx")
@click.option("--since", required=True, help="起始日 YYYY-MM-DD")
@click.option("--until", required=True, help="截止日 YYYY-MM-DD（含）")
@click.option("--yes", is_flag=True, default=False, help="真正请求 CFETS 并写库；不传只 dry run")
def fx_command(since: str, until: str, yes: bool) -> None:
    """CFETS 人民币汇率中间价入 core.fx_rate（免费公开，不消耗 Wind 额度）。"""
    start, end = date.fromisoformat(since), date.fromisoformat(until)
    if not yes:
        windows = -(-((end - start).days + 1) // WINDOW_DAYS)
        click.echo(
            f"[dry run] {len(PAIRS)} 个币种对 × {windows} 个窗口（至少 {len(PAIRS) * windows} 次"
            f"请求，限速 6 次/分钟）：{', '.join(PAIRS)}；加 --yes 执行"
        )
        return
    blob = LocalBlobStore(load_config().blob_root)
    with psycopg.connect(_dsn()) as conn:
        summary = ingest_fx(
            conn,
            adapter=_cfets_adapter(),
            blob=blob,
            start=start,
            end=end,
            ingest_run_id=f"cli:facts-fx:{uuid.uuid4().hex[:12]}",
        )
    echo_summary(summary)


@click.command("holders")
@click.option("--entity-ref", "entity_refs", multiple=True, help="只取这些代码（如 688041.SH）")
@click.option("--period", "periods", multiple=True, help="期别，如 2024FY / 2025H1 / 2025Q3")
@click.option(
    "--recent", default=8, show_default=True, type=int, help="未指定 --period 时取最近几期"
)
@click.option("--refresh", is_flag=True, default=False, help="已有数据的期别也重取（检测追溯调整）")
@click.option(
    "--from-snapshots",
    is_flag=True,
    default=False,
    help="不调用 Wind，用已留存的同问句响应重新解析入库（解析规则改进后省额度）",
)
@click.option("--yes", is_flag=True, default=False, help="真正调用 Wind 并写库；不传只 dry run")
def holders_command(
    entity_refs: tuple[str, ...],
    periods: tuple[str, ...],
    recent: int,
    refresh: bool,
    from_snapshots: bool,
    yes: bool,
) -> None:
    """前十大股东入 core.shareholder_holding；known_at 取该期定期报告的首次披露时刻。"""
    with psycopg.connect(_dsn()) as conn:
        entities = a_share_entities(conn, entity_refs or None)
        chosen = list(periods) or periods_back(date.today(), recent, HOLDER_KINDS)
        if not yes:
            plan = plan_calls(
                conn,
                table="core.shareholder_holding",
                entities=entities,
                periods=chosen,
                refresh=refresh,
            )
            click.echo(
                f"[dry run] {len(entities)} 家 × {len(chosen)} 期：最多调用 Wind {plan.would_call}"
                f" 次；库里已有 {plan.already_present}；无披露记录跳过 {plan.skipped_no_disclosure}"
            )
            return
        gateway = (
            SnapshotReplayGateway(conn)
            if from_snapshots
            else gateway_client_from_env(url_env=WIND_URL_ENV, provider="wind-mcp")
        )
        summary = ingest_holders(
            conn,
            adapter=WindHoldersAdapter(gateway),
            blob=LocalBlobStore(load_config().blob_root),
            entities=entities,
            periods=chosen,
            ingest_run_id=f"cli:facts-holders:{uuid.uuid4().hex[:12]}",
            refresh=refresh,
        )
    echo_summary(summary)


@click.command("segments")
@click.option("--entity-ref", "entity_refs", multiple=True, help="只取这些代码（如 688041.SH）")
@click.option(
    "--period", "periods", multiple=True, help="期别：2024FY / 2025H1（分部只有年报、中报）"
)
@click.option(
    "--recent", default=4, show_default=True, type=int, help="未指定 --period 时取最近几期"
)
@click.option("--refresh", is_flag=True, default=False, help="已有数据的期别也重取（检测追溯调整）")
@click.option(
    "--from-snapshots",
    is_flag=True,
    default=False,
    help="不调用 Wind，用已留存的同问句响应重新解析入库（解析规则改进后省额度）",
)
@click.option("--yes", is_flag=True, default=False, help="真正调用 Wind 并写库；不传只 dry run")
def segments_command(
    entity_refs: tuple[str, ...],
    periods: tuple[str, ...],
    recent: int,
    refresh: bool,
    from_snapshots: bool,
    yes: bool,
) -> None:
    """主营构成入 core.segment_revenue；入库前与 fin_fact 营业收入对账，对不上的不入库。"""
    with psycopg.connect(_dsn()) as conn:
        entities = a_share_entities(conn, entity_refs or None)
        chosen = list(periods) or periods_back(date.today(), recent, SEGMENT_KINDS)
        if not yes:
            plan = plan_calls(
                conn,
                table="core.segment_revenue",
                entities=entities,
                periods=chosen,
                refresh=refresh,
            )
            click.echo(
                f"[dry run] {len(entities)} 家 × {len(chosen)} 期：最多调用 Wind {plan.would_call}"
                f" 次；库里已有 {plan.already_present}；无披露记录跳过 {plan.skipped_no_disclosure}"
            )
            return
        gateway = (
            SnapshotReplayGateway(conn)
            if from_snapshots
            else gateway_client_from_env(url_env=WIND_URL_ENV, provider="wind-mcp")
        )
        summary = ingest_segments(
            conn,
            adapter=WindSegmentsAdapter(gateway),
            blob=LocalBlobStore(load_config().blob_root),
            entities=entities,
            periods=chosen,
            ingest_run_id=f"cli:facts-segments:{uuid.uuid4().hex[:12]}",
            refresh=refresh,
        )
    echo_summary(summary)
