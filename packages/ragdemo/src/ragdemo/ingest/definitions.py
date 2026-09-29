"""Dagster Definitions 与传感器。

`new_document_sensor` 读 core.document，把未处理的新文档行（按 doc_id 游标）
封装成 RunRequest / SkipReason 返回，本身不写库。真正的衔接点（写 core.event
候选行，供 LangGraph 侧监听器按 event_id 取任务）留给 Agent 层接入时实现——
**Dagster 资产不直接调用 Agent**，避免数据管线被模型调用的延迟与失败拖垮
（docs/01-architecture.md §3）。

文档管线的三个资产（`doc_normalized` / `doc_prepared` / `doc_blocks_loaded` /
`block_embeddings`）此前写完了、有测试，但从未注册进这里的 `Definitions`——
生产环境从来没有物化过它们。本文件把它们接上：`announcements` / `parser` /
`embedder` 目前都只能是 Mock（供应商未定 ADR-0005；真实解析器走
`ragdemo.config.load_config()` 判断，见 `parser_resource`；真实嵌入器留给
Phase 1.4，与 `ingest/cli.py` 的 `embed` 命令是同一个处境），`blob` /
`document_writer` / `conn` 是真实资源。

本文件刻意不用 `from __future__ import annotations`——与 `ingest/assets_docs.py`
同样的原因：Dagster 在 `@asset` 装饰期对 `context` 参数做类型校验时直接比较
`Parameter.annotation`，不解析 PEP 563 的延迟字符串注解，开着这行会让
`announcements_daily` 这个资产在 import 期就抛
`DagsterInvalidDefinitionError`。Python 3.11 原生支持 `list[X]` / `X | None`，
去掉这行不影响其余注解写法。
"""

import os
from datetime import date

import psycopg
from dagster import (
    AssetExecutionContext,
    DefaultSensorStatus,
    Definitions,
    InitResourceContext,
    ResourceParam,
    RunRequest,
    SensorEvaluationContext,
    SkipReason,
    asset,
    define_asset_job,
    resource,
    sensor,
)

from ragdemo.adapters.announcements import AnnouncementProvider
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import gateway_client_from_env
from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from ragdemo.adapters.mock.facts import MockFactAdapter
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.config import load_config
from ragdemo.embed.base import Embedder
from ragdemo.embed.mock import MockEmbedder
from ragdemo.ingest.announcement_sources import build_announcement_sources
from ragdemo.ingest.announcements import (
    DOWNLOAD_ENTITIES,
    AnnouncementRunReport,
    AnnouncementSources,
    entity_routes,
    ingest_announcements,
)
from ragdemo.ingest.assets import (
    DAILY,
    fact_normalized,
    fin_fact_loaded,
    price_daily_loaded,
    price_normalized,
)
from ragdemo.ingest.assets_docs import (
    block_embeddings,
    doc_blocks_loaded,
    doc_normalized,
    doc_prepared,
    tier_c_reparse,
)
from ragdemo.ingest.documents import DocumentWriter
from ragdemo.ingest.parse_policy import ParseTier
from ragdemo.ingest.prices import PriceSources
from ragdemo.ingest.writer import PointInTimeWriter
from ragdemo.parse.textin import (
    XPARSE_PARAMS_HIGH_PRECISION,
    DocumentParser,
    MockDocumentParser,
    PageBudget,
    TextInParser,
)
from ragdemo.quality.budget_asset import monthly_cost
from ragdemo.quality.checks import ALL_CHECKS
from ragdemo.sources.backlog import Backlog
from ragdemo.sources.health import MemorySourceHealth, PgSourceHealth, SourceHealth
from ragdemo_core.blob import BlobStore, LocalBlobStore

TRACKED_DOC_TYPES = ("quarterly", "annual_report", "announcement", "10-K", "10-Q", "8-K")


def _conn() -> psycopg.Connection:
    return psycopg.connect(os.environ["RAGDEMO_DSN"])


@resource
def writer_resource(_context: InitResourceContext) -> PointInTimeWriter:
    """惰性构造：只有 Dagster 真正初始化该资源时才会连库，而不是模块导入时。"""
    return PointInTimeWriter(_conn(), ingest_run_id="dagster", source="mock")


@resource
def document_writer_resource(_context: InitResourceContext) -> DocumentWriter:
    """惰性构造，理由与 `writer_resource` 相同。`source` 固定为
    "mock-announcements"：与下面 `announcements_resource` 是同一个来源，
    真实供应商接入时两处要一起改，不能只改一处。"""
    return DocumentWriter(_conn(), ingest_run_id="dagster", source="mock-announcements")


@resource
def conn_resource(_context: InitResourceContext) -> psycopg.Connection:
    """`block_embeddings` 直接用它查待办队列、写 embedding 列——与
    `document_writer_resource` 是两条独立的连接，互不影响各自的事务边界。
    `price_normalized` / `price_daily_loaded`（阶段 F）复用同一个资源读写
    `core.ingest_watermark`，不需要再开一条独立连接。"""
    return _conn()


@resource
def tushare_writer_resource(_context: InitResourceContext) -> PointInTimeWriter:
    """阶段 F：`source="tushare"`，与 `writer_resource`（`source="mock"`，
    服务 `MockFactAdapter` 那条既有的事实管线）是两个独立的写入器——行情
    真的来自 Tushare（经 MCP 网关），把它标成 "mock" 会违反 CLAUDE.md §1.3
    「每个事实都必须携带来源标识」。"""
    return PointInTimeWriter(_conn(), ingest_run_id="dagster", source="tushare")


TUSHARE_MCP_URL_ENV = "RAGDEMO_TUSHARE_MCP_URL"
TUSHARE_REST_URL_ENV = "RAGDEMO_TUSHARE_REST_URL"
WIND_STOCK_URL_ENV = "RAGDEMO_WIND_MCP_URL"


def _configured(env: str) -> bool:
    return bool(os.environ.get(env, "").strip())


@resource
def price_sources_resource(_context: InitResourceContext) -> PriceSources:
    """行情三层（ADR-0012）：Tushare MCP → Tushare REST → Wind 日 K 线，都经出网代理
    （8083 / 8083 / 8084 端口，凭据由代理注入）。哪一层的地址没配，哪一层就不参与。

    三个地址一个都没配时退回 Mock 网关——与 `parser_resource` 判断 `TEXTIN_BASE_URL`
    同一套规则，本地开发与测试不该被逼着配真实地址。冷却状态在配了真实来源时存
    `core.source_health`（autocommit 连接，入库回滚撤销不了它）。
    """
    if not any(map(_configured, (TUSHARE_MCP_URL_ENV, TUSHARE_REST_URL_ENV, WIND_STOCK_URL_ENV))):
        return PriceSources(health=MemorySourceHealth(), tushare_mcp=MockMcpGateway())
    rest = None
    if _configured(TUSHARE_REST_URL_ENV):
        rest = HttpClient(
            "tushare-rest",
            os.environ[TUSHARE_REST_URL_ENV].strip(),
            policy=RetryPolicy(max_attempts=2, backoff_base_s=1.0),
            bucket=TokenBucket(rate_per_minute=120),
            timeout_s=30.0,
        )
    return PriceSources(
        health=PgSourceHealth(psycopg.connect(os.environ["RAGDEMO_DSN"], autocommit=True)),
        tushare_mcp=gateway_client_from_env(url_env=TUSHARE_MCP_URL_ENV, provider="tushare-mcp")
        if _configured(TUSHARE_MCP_URL_ENV)
        else None,
        tushare_rest=rest,
        wind_stock=gateway_client_from_env(url_env=WIND_STOCK_URL_ENV, provider="wind-mcp")
        if _configured(WIND_STOCK_URL_ENV)
        else None,
    )


@resource
def announcements_resource(_context: InitResourceContext) -> AnnouncementProvider:
    """公告供应商未定（ADR-0005 只锁定了 `AnnouncementProvider` 接口，不锁
    供应商），生产环境目前没有真实实现可接——与事实侧的
    `adapter: MockFactAdapter()` 是同一个处境，不是本次接线任务要解决的
    缺口：供应商选型属于 `docs/10-roadmap.md` 另一项未完成的交付。"""
    return MockAnnouncementProvider()


@resource
def blob_resource(_context: InitResourceContext) -> BlobStore:
    return LocalBlobStore(load_config().blob_root)


@resource
def parser_resource(_context: InitResourceContext) -> DocumentParser:
    """按 `TEXTIN_BASE_URL` 是否设置选择真实解析器还是 Mock——与 `ragdemo
    docs ingest` CLI（`ingest/cli.py` 的 `--parser` 判断）同一套规则，不
    重新发明一套开关。`base_url` 缺失时退回 `MockDocumentParser`：本地
    开发与测试都不该被逼着配一个真实解析器地址。"""
    cfg = load_config()
    if cfg.textin_base_url is None:
        return MockDocumentParser()
    return TextInParser(
        cfg.require_textin_base_url(),
        LocalBlobStore(cfg.blob_root),
        allow_private=cfg.textin_allow_private,
    )


@resource
def tier_c_parser_resource(_context: InitResourceContext) -> TextInParser | None:
    """阶段 G：C 档重解析用的高精度 `TextInParser`（`dpi`/`raw_ocr` 与 B 档
    不同的参数集，见 `parse/textin.py::XPARSE_PARAMS_HIGH_PRECISION`）。
    `TEXTIN_BASE_URL` 缺失时返回 None——与 `parser_resource` 不同的是这里
    不退回一个 Mock：C 档本来就只在"真的要花钱换更高精度"时才有意义，
    Mock 解析器测不出真实提升，`tier_c_reparse` 收到 None 就直接跳过整个
    资产（见该资产 docstring）。"""
    cfg = load_config()
    if cfg.textin_base_url is None:
        return None
    return TextInParser(
        cfg.require_textin_base_url(),
        LocalBlobStore(cfg.blob_root),
        params=XPARSE_PARAMS_HIGH_PRECISION,
    )


@resource
def embedder_resource(_context: InitResourceContext) -> Embedder:
    """真实嵌入器留给 Phase 1.4——`ingest/cli.py` 的 `embed` 命令对
    `MockEmbedder` 有同一句注释。生产 `Definitions` 目前也只能接它。"""
    return MockEmbedder()


# --- 公告（公告双源接入计划 Task 12）-----------------------------------------


@resource
def announcement_sources_resource(_context: InitResourceContext) -> AnnouncementSources:
    """五个来源层各自独立按环境变量判断是否配置齐全，理由见
    `ingest/announcement_sources.py` 模块 docstring。"""
    return build_announcement_sources(load_config())


@resource
def announcement_health_resource(_context: InitResourceContext) -> SourceHealth:
    """与 `price_sources_resource` 同一条理由：冷却状态存 `core.source_health`，
    autocommit 连接——入库回滚撤销不了它，也不该被撤销（冷却是"这一层最近失败过"
    这一事实本身，不是这次 run 的一部分）。"""
    return PgSourceHealth(psycopg.connect(os.environ["RAGDEMO_DSN"], autocommit=True))


@resource
def announcement_backlog_resource(_context: InitResourceContext) -> Backlog:
    """autocommit 连接：待补记录不能随入库事务回滚，与 `announcement_health_resource`
    同一个理由（`Backlog.__init__` 会拒绝非 autocommit 连接）。"""
    return Backlog(psycopg.connect(os.environ["RAGDEMO_DSN"], autocommit=True))


# 首次为某实体建目录时的回填起点；此后每个实体走自己的 `core.ingest_watermark`
# 增量水位线，不会重复回填到这么早——与 D1-D5 计划的 2025 全年试点范围一致。
ANNOUNCEMENTS_BACKFILL_SINCE = date(2023, 1, 1)  # 与 2026-09-29 全量回填的起点一致

# 单次 run 的页数预算上限，与 `assets_docs.py::MAX_PAGES_PER_PREPARE_RUN` 同一个
# 理由：TextIn 按页计费，回填历史分区时没有这道闸门会一次性把预算烧穿。
MAX_PAGES_PER_ANNOUNCEMENTS_RUN = 2000


@asset(partitions_def=DAILY, group_name="documents")
def announcements_daily(
    context: AssetExecutionContext,
    announcement_sources: ResourceParam[AnnouncementSources],
    announcement_health: ResourceParam[SourceHealth],
    announcement_backlog: ResourceParam[Backlog],
    conn: ResourceParam[psycopg.Connection],
    blob: ResourceParam[BlobStore],
) -> AnnouncementRunReport:
    """公告双源接入计划 Task 9（`ingest_announcements`）接成真正的资产。解析档位
    固定只请求 `ParseTier.FIRST`——`ParseTier.SECOND`（年报/半年报全文）与更多
    实体的下载范围是 `docs parse-pending` / 调整 `DOWNLOAD_ENTITIES` 之后才
    扩大的范围（Task 10），不在这个每日资产里悄悄多做。"""
    partition_date = date.fromisoformat(context.partition_key)
    routes = entity_routes(conn)
    run_id = context.op_execution_context.run_id
    report = ingest_announcements(
        conn,
        entities=routes,
        sources=announcement_sources,
        blob=blob,
        health=announcement_health,
        backlog=announcement_backlog,
        partition_date=partition_date,
        backfill_since=ANNOUNCEMENTS_BACKFILL_SINCE,
        download_entities=DOWNLOAD_ENTITIES,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(MAX_PAGES_PER_ANNOUNCEMENTS_RUN),
        ingest_run_id=run_id,
        cost_per_page_cny=load_config().textin_cost_per_page_cny,
    )
    context.log.info(
        "announcements ingested",
        extra={
            "run_id": run_id,
            "as_of": partition_date.isoformat(),
            "entity_count": len(routes),
            "indexed": report.indexed,
            "parsed_docs": report.parsed_docs,
            "parsed_pages": report.parsed_pages,
            "by_layer": report.by_layer,
            "backlogged": len(report.backlogged),
            "failed": len(report.failed),
        },
    )
    return report


# 临时目标：传感器发现新文档后，重新物化这两个事实资产。
# 真正的行为（写 core.event 候选行）留给 Agent 层接入时实现——见模块 docstring。
_document_reaction_job = define_asset_job(
    "document_reaction_placeholder", selection=[fact_normalized, fin_fact_loaded]
)


@sensor(
    minimum_interval_seconds=300,
    default_status=DefaultSensorStatus.STOPPED,
    job=_document_reaction_job,
)
def new_document_sensor(ctx: SensorEvaluationContext) -> RunRequest | SkipReason:
    """新文档触发下游处理。

    临时目标：重新物化事实资产（`_document_reaction_job`）。真正的行为
    （写 core.event 候选行，判定条件见 docs/07-agents.md §7）留给 Agent 层
    接入时实现——当前只保证扫到新文档时能安全触发一次 run，不会因为
    传感器缺少 job 目标而在 evaluate_tick 时崩溃。
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT doc_id FROM core.document "
            " WHERE doc_type = ANY(%s) AND superseded_at IS NULL"
            "   AND doc_id > COALESCE(%s, 0) ORDER BY doc_id LIMIT 50",
            (list(TRACKED_DOC_TYPES), ctx.cursor),
        ).fetchall()
    if not rows:
        return SkipReason("无新文档")
    ctx.update_cursor(str(rows[-1][0]))
    return RunRequest(run_key=f"docs-{rows[-1][0]}", run_config={})


defs = Definitions(
    assets=[
        monthly_cost,
        fact_normalized,
        fin_fact_loaded,
        doc_normalized,
        doc_prepared,
        doc_blocks_loaded,
        tier_c_reparse,
        block_embeddings,
        price_normalized,
        price_daily_loaded,
        announcements_daily,
    ],
    asset_checks=list(ALL_CHECKS),
    jobs=[_document_reaction_job],
    sensors=[new_document_sensor],
    resources={
        "adapter": MockFactAdapter(),
        "writer": writer_resource,
        "announcements": announcements_resource,
        "blob": blob_resource,
        "parser": parser_resource,
        "document_writer": document_writer_resource,
        "embedder": embedder_resource,
        "conn": conn_resource,
        "tushare_writer": tushare_writer_resource,
        "price_sources": price_sources_resource,
        "tier_c_parser": tier_c_parser_resource,
        "announcement_sources": announcement_sources_resource,
        "announcement_health": announcement_health_resource,
        "announcement_backlog": announcement_backlog_resource,
    },
)
