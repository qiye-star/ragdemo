"""`ragdemo facts fx / holders / segments`：阶段二的三类结构化数据入库命令。

挂到 `facts_cli.facts_group` 上（见该文件末尾的注册）。默认 dry run：不调外部、不写库；
`--yes` 才执行。股东 / 分部的 dry run 会给出 Wind 调用次数上限，方便事先估额度。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime

import click
import psycopg

from ragdemo.adapters.cfets import PAIRS, WINDOW_DAYS, CfetsAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import gateway_client_from_env
from ragdemo.adapters.snapshot_replay import SnapshotReplayGateway
from ragdemo.adapters.wind_structured import WindHoldersAdapter, WindSegmentsAdapter
from ragdemo.config import load_config
from ragdemo.ingest.price_backfill import backfill_prices, tushare_codes
from ragdemo.ingest.price_sources_build import build_price_sources
from ragdemo.ingest.stage2_crosscheck import crosscheck_holders
from ragdemo.ingest.stage2_verify import verify_stage2
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
        # 该站 WAF 的 403 由 pageSize 触发、与频率无关（见 adapters/cfets.py），仍限速礼貌取数。
        bucket=TokenBucket(rate_per_minute=30),
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
    for key, dims in summary.dropped_shares.items():
        click.echo(
            f"[占比已丢弃] {key}：{'/'.join(dims)} 的收入占比与收入对不上，只入库收入", err=True
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
            f"请求，限速 30 次/分钟）：{', '.join(PAIRS)}；加 --yes 执行"
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


@click.command("verify-stage2")
def verify_stage2_command() -> None:
    """阶段二数据体检（只读库）：股东 / 分部 / 汇率的一致性、时点、溯源、可复现。
    有任何 error 以非零码退出；warning（如分部没有对应期营收、未对账）只报告。"""
    with psycopg.connect(_dsn()) as conn:
        findings = verify_stage2(conn, as_of=datetime.now(UTC))
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t} WHERE superseded_at IS NULL").fetchone()
            for t in ("core.shareholder_holding", "core.segment_revenue", "core.fx_rate")
        }
    click.echo(
        "现行行数：" + "，".join(f"{t.split('.')[1]} {c[0] if c else 0}" for t, c in counts.items())
    )
    for f in findings:
        click.echo(f"[{f.severity}] {f.check}: {f.message}", err=f.severity == "error")
    errors = [f for f in findings if f.severity == "error"]
    click.echo(f"错误 {len(errors)}，警告 {len(findings) - len(errors)}")
    if errors:
        raise SystemExit(1)


@click.command("crosscheck-holders")
def crosscheck_holders_command() -> None:
    """股东数据的独立核对：Wind 入库的前十大股东 vs 已解析的公告原文表格（逐股相等）。
    原文没解析的期别如实报告为"无来源"，不当作通过；有一处对不上以非零码退出。"""
    with psycopg.connect(_dsn()) as conn:
        report = crosscheck_holders(conn)
    click.echo(
        f"核对 {report.checked} 名股东，与原文一致 {report.matched}，"
        f"对不上 {len(report.mismatched)}"
    )
    for line in report.mismatched:
        click.echo(f"[对不上] {line}", err=True)
    if report.no_source:
        click.echo(f"[无来源] {len(report.no_source)} 个期别没有可核对的已解析原文", err=True)
    if report.mismatched:
        raise SystemExit(1)


@click.command("prices")
@click.option("--since", required=True, help="起始日 YYYY-MM-DD")
@click.option("--until", required=True, help="截止日 YYYY-MM-DD（含）")
@click.option("--entity-ref", "entity_refs", multiple=True, help="只回填这些代码（如 688041.SH）")
@click.option("--yes", is_flag=True, default=False, help="真正请求行情源并写库；不传只 dry run")
def prices_command(since: str, until: str, entity_refs: tuple[str, ...], yes: bool) -> None:
    """日行情区间回填 → core.price_daily（一次区间调用/实体，三层降级，每次留快照）。
    不推进 Dagster 共用的行情游标；写入幂等，可安全重跑。"""
    start, end = date.fromisoformat(since), date.fromisoformat(until)
    with psycopg.connect(_dsn()) as conn:
        codes = tushare_codes(conn, entity_refs or None)
        if not yes:
            click.echo(
                f"[dry run] {len(codes)} 个实体 × 一次区间调用（{start} ~ {end}）；加 --yes 执行"
            )
            return
        report = backfill_prices(
            conn,
            # 区间回填不启用 Wind 日 K 线降级层：它逐日调用、消耗 Wind 额度。
            replace(build_price_sources(), wind_stock=None),
            LocalBlobStore(load_config().blob_root),
            ts_codes=codes,
            since=start,
            until=end,
            ingest_run_id=f"cli:facts-prices:{uuid.uuid4().hex[:12]}",
            on_progress=click.echo,
        )
    click.echo(
        f"实体 {report.entities}，新增 {report.inserted}，更正 {report.corrected}，"
        f"无变化 {report.skipped}，按层 {report.by_layer}"
    )
    for code, reason in report.failed.items():
        click.echo(f"[失败] {code}: {reason}", err=True)
    if report.failed:
        raise click.ClickException(f"{len(report.failed)} 个实体未能回填（冷却过后重跑即可）")
