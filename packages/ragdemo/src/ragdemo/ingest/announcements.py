"""公告路由编排（公告双源接入计划 Task 9）：按市场分派到 A 股 / 港股 / 美股三条
能力，A 股走 `SourceRouter` 的两层降级（cninfo → ifind-notice），待补清单与游标
管理都在这里，各层本身（`run_cninfo_layer` 等）不关心跨层的事。

区间语义：游标 `core.ingest_watermark(source_id=f"announcements:{group}:{entity_ref}")`
记"这个实体处理到哪一天了"；下一次的 `since` = 游标次日，没有游标就用 `backfill_since`
（首次回填）。待补清单里如果这个实体还有未解决项，`since` 改用待补项的 `since`
（更早）——巨潮恢复后要把缺口一次性补上，不是只从游标继续。

只有 A 股的主源（cninfo）成功才推进游标、解决待补；降级层（ifind）成功只写
目录、把全文记入待补，**不推进游标**——全文还没到，不能假装这段时间已经处理完。
港股 / 美股没有降级层，失败直接进待补、本实体本次跳过。

Wind 曾经也是 A 股的一层降级来源（`get_company_announcements`），2026-09-29 实盘
验收时发现这个账号的 Wind 服务范围只覆盖财务/行情/股东/分部数据，不含公告，
已从这里的路由移除（同步改 ADR-0013、`sources/routing.py`）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.ifind_notice import IfindNoticeAdapter
from ragdemo.ingest.announcement_index import IndexOutcome, IndexWriter
from ragdemo.ingest.cninfo_docs import CninfoLayerDeps, run_cninfo_layer
from ragdemo.ingest.edgar_index import EdgarLayerDeps, run_edgar_layer
from ragdemo.ingest.hkexnews_docs import HkexnewsLayerDeps, run_hkexnews_layer
from ragdemo.ingest.parse_policy import ParseTier
from ragdemo.ingest.reconcile import read_watermark, write_watermark
from ragdemo.ingest.snapshot import record_snapshot
from ragdemo.parse.textin import PageBudget
from ragdemo.sources.backlog import Backlog
from ragdemo.sources.failures import Failure, FailureKind
from ragdemo.sources.health import SourceHealth
from ragdemo.sources.router import AllLayersFailed, Layer, SourceRouter
from ragdemo.sources.routing import Capability
from ragdemo_core.blob import BlobStore

# D2（负责人 2026-09-28 决定）：只对这两家下载 PDF 并解析，验证效果与成本后再扩大。
DOWNLOAD_ENTITIES: frozenset[str] = frozenset({"688041.SH", "000977.SZ"})

# entity.market 原样值 → 分组。台积电/日月光这类 TWSE 上市但报 SEC 的公司不在这张
# 表里——它们靠 EntityRoute.cik 非空直接归 'us'，见 market_group()。
_MARKET_GROUP: Mapping[str, str] = {
    "SSE": "cn",
    "SZSE": "cn",
    "HKEX": "hk",
    "NASDAQ": "us",
    "NYSE": "us",
}

_CAPABILITY_BY_GROUP: Mapping[str, Capability] = {
    "cn": Capability.ANNOUNCEMENTS_CN,
    "hk": Capability.ANNOUNCEMENTS_HK,
    "us": Capability.ANNOUNCEMENTS_US,
}


@dataclass(frozen=True)
class EntityRoute:
    entity_ref: str
    short_name: str
    market: str  # core.entity.market 原样值
    cik: int | None = None


def market_group(route: EntityRoute) -> str | None:
    """返回 'cn' / 'hk' / 'us'，或 `None`（本次跳过：日 / 韩 / 其余台股）。
    有 `cik` 的实体（含台积电、日月光）一律归 'us'，不看 `market` 字段——它们在
    港股/台股上市，但同时向 SEC 报送 20-F/6-K（Task 8）。"""
    if route.cik is not None:
        return "us"
    return _MARKET_GROUP.get(route.market)


def entity_routes(conn: psycopg.Connection) -> list[EntityRoute]:
    """把 `core.entity` 的每一行翻成一条 `EntityRoute`——`ingest_announcements`
    与 `docs announcements` CLI 共用同一份枚举逻辑，不各写一套。

    代码列不是按供应商名字取的，是按"这条路由归哪个市场分组"取的：`edgar_cik`
    优先（决定 `market_group` 恒为 'us'，与该函数自己的判定规则一致）；否则按
    `market` 落在 A 股集合时取 `tushare_code`；落在 'HKEX' 时取 `wind_code`——
    这两列都是 `DocumentWriter._resolve_entity_id` 认的四选一，只是港股这一列
    目前多半还没有种子数据（P0 已知缺口，见计划文档），到位之前这些实体的路由
    会被下面的 `None` 分支自然跳过，不是这里的判断有问题。没有任何一列能定出
    市场分组的实体（数据还没补全，或者本来就不在这次接入范围内，比如日股/韩股）
    直接跳过，不是错误。
    """
    rows = conn.execute(
        "SELECT name_short, name_full, market, tushare_code, wind_code, edgar_cik  FROM core.entity"
    ).fetchall()
    routes: list[EntityRoute] = []
    for name_short, name_full, market, tushare_code, wind_code, edgar_cik in rows:
        short_name = name_short or name_full
        if edgar_cik:
            routes.append(
                EntityRoute(
                    entity_ref=str(edgar_cik),
                    short_name=short_name,
                    market=market or "",
                    cik=int(edgar_cik),
                )
            )
        elif market in _MARKET_GROUP and _MARKET_GROUP[market] == "cn" and tushare_code:
            routes.append(
                EntityRoute(entity_ref=tushare_code, short_name=short_name, market=market)
            )
        elif market == "HKEX" and wind_code:
            routes.append(EntityRoute(entity_ref=wind_code, short_name=short_name, market=market))
    return routes


@dataclass(frozen=True)
class AnnouncementSources:
    cninfo: CninfoLayerDeps | None = None
    ifind: IfindNoticeAdapter | None = None
    hkexnews: HkexnewsLayerDeps | None = None
    edgar: EdgarLayerDeps | None = None


@dataclass
class AnnouncementRunReport:
    by_layer: dict[str, int] = field(default_factory=dict)
    indexed: int = 0
    parsed_docs: int = 0
    parsed_pages: int = 0
    validation_failed: int = 0
    stored_unparsed: int = 0
    skipped_budget: int = 0
    no_attachment: int = 0
    backlogged: dict[str, str] = field(default_factory=dict)  # entity_ref → 原因
    failed: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_NEW_ROW_OUTCOMES = frozenset({IndexOutcome.INSERTED, IndexOutcome.SUPERSEDED_DEGRADED})


def _ifind_degraded(
    ifind: IfindNoticeAdapter,
    index: IndexWriter,
    blob: BlobStore,
    *,
    route: EntityRoute,
    since: date,
    until: date,
    ingest_run_id: str,
) -> int:
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=until)
    raw = ifind.fetch(ctx, short_name=route.short_name, since=since, until=until)
    record_snapshot(index.conn, raw, ingest_run_id=ingest_run_id, blob=blob)
    written = 0
    for entry in ifind.parse_entries(
        raw, entity_ref=route.entity_ref, short_names=[route.short_name]
    ):
        outcome, _ = index.write(entry)
        if outcome in _NEW_ROW_OUTCOMES:
            written += 1
    return written


def ingest_announcements(
    conn: psycopg.Connection,
    *,
    entities: Sequence[EntityRoute],
    sources: AnnouncementSources,
    blob: BlobStore,
    health: SourceHealth,
    backlog: Backlog,
    partition_date: date,
    backfill_since: date,
    download_entities: frozenset[str] = DOWNLOAD_ENTITIES,
    tiers: frozenset[ParseTier],
    budget: PageBudget,
    ingest_run_id: str,
    cost_per_page_cny: Decimal | None = None,
    refetch: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AnnouncementRunReport:
    report = AnnouncementRunReport()
    index = IndexWriter(conn, ingest_run_id=ingest_run_id, now=now)
    router = SourceRouter(health, clock=now)

    for route in entities:
        group = market_group(route)
        if group is None:
            report.warnings.append(
                f"{route.entity_ref}: 市场 {route.market!r} 暂不支持（日/韩需要 API key，"
                "其余台股缺官方历史/PDF 接口），本次跳过"
            )
            continue

        capability = _CAPABILITY_BY_GROUP[group]
        watermark_source = f"announcements:{group}:{route.entity_ref}"
        floor_source = f"announcements-floor:{group}:{route.entity_ref}"
        cursor = read_watermark(conn, watermark_source, partition_date)
        floor_raw = read_watermark(conn, floor_source, partition_date)
        floor = date.fromisoformat(floor_raw) if floor_raw else None
        since = date.fromisoformat(cursor) + timedelta(days=1) if cursor else backfill_since
        if cursor and (floor is None or floor > backfill_since):
            # 游标只管"往后追新"，不保证更早的区间也覆盖过（首跑窗口可能比现在的
            # backfill_since 晚）。覆盖下界（floor）没到 backfill_since 就整段重取一次，
            # 已入库的行由 IndexWriter 幂等跳过；没有下界记录的老游标同样重取。
            since = min(since, backfill_since)

        if refetch:
            # 补下载：目录早已建好（游标已覆盖），但当时没下载全文——忽略游标整段重取，
            # 已下载并落了 document 的公告由各层的 already_downloaded 规则跳过。
            since = backfill_since
        open_items = {i.entity_ref: i for i in backlog.open_items(capability)}
        if route.entity_ref in open_items:
            since = min(since, open_items[route.entity_ref].since)
        if since > partition_date:
            continue  # 已经处理到这个分区，没有新区间

        download = route.entity_ref in download_entities
        layers = _layers_for(
            group,
            route,
            sources,
            index=index,
            blob=blob,
            since=since,
            until=partition_date,
            tiers=tiers,
            budget=budget,
            download=download,
            ingest_run_id=ingest_run_id,
            cost_per_page_cny=cost_per_page_cny,
        )
        if not layers:
            report.warnings.append(f"{route.entity_ref}: 没有配置任何可用来源，本次跳过")
            continue

        try:
            routed = router.run(capability, layers)
        except AllLayersFailed as exc:
            failure = _last_failure(exc)
            report.failed[route.entity_ref] = str(exc)
            backlog.add(capability, route.entity_ref, since=since, failure=failure, now=now())
            report.backlogged[route.entity_ref] = f"{failure.kind.value}: {failure.message[:200]}"
            continue

        report.by_layer[routed.layer] = report.by_layer.get(routed.layer, 0) + 1
        if isinstance(routed.value, int):  # 降级层：只返回写了几条目录
            report.indexed += routed.value
        else:  # 主源：LayerReport
            report.indexed += routed.value.indexed
            report.parsed_docs += routed.value.parsed_docs
            report.parsed_pages += routed.value.parsed_pages
            report.validation_failed += routed.value.validation_failed
            report.stored_unparsed += routed.value.stored_unparsed
            report.skipped_budget += routed.value.skipped_budget
            report.no_attachment += routed.value.no_attachment

        if routed.degraded:
            # 降级层成功：目录有了，全文还没到——记入待补，不推进游标。
            backlog.add(
                capability,
                route.entity_ref,
                since=since,
                failure=_degraded_placeholder_failure(),
                now=now(),
            )
            report.backlogged[route.entity_ref] = "degraded: 全文待主源恢复后补齐"
        else:
            backlog.resolve(capability, route.entity_ref, covered_since=since, now=now())
            write_watermark(conn, watermark_source, partition_date, partition_date.isoformat())
            covered_from = since if floor is None else min(floor, since)
            write_watermark(conn, floor_source, partition_date, covered_from.isoformat())

    return report


def _layers_for(
    group: str,
    route: EntityRoute,
    sources: AnnouncementSources,
    *,
    index: IndexWriter,
    blob: BlobStore,
    since: date,
    until: date,
    tiers: frozenset[ParseTier],
    budget: PageBudget,
    download: bool,
    ingest_run_id: str,
    cost_per_page_cny: Decimal | None,
) -> list[Layer[Any]]:
    layers: list[Layer[Any]] = []
    if group == "cn":
        cn_since_dt = datetime.combine(since, datetime.min.time(), tzinfo=UTC)
        cn_until_dt = datetime.combine(until, datetime.min.time(), tzinfo=UTC)
        cninfo_deps = sources.cninfo
        if cninfo_deps is not None:
            layers.append(
                Layer(
                    "cninfo",
                    "cninfo",
                    False,
                    lambda: run_cninfo_layer(
                        index.conn,
                        deps=cninfo_deps,
                        index=index,
                        entity_ref=route.entity_ref,
                        stock_code=route.entity_ref.split(".")[0],
                        since=cn_since_dt,
                        until=cn_until_dt,
                        tiers=tiers,
                        budget=budget,
                        download=download,
                        ingest_run_id=ingest_run_id,
                        dry_run=False,
                        cost_per_page_cny=cost_per_page_cny,
                    ),
                )
            )
        ifind = sources.ifind
        if ifind is not None:
            layers.append(
                Layer(
                    "ifind-notice",
                    "ifind",
                    True,
                    lambda: _ifind_degraded(
                        ifind,
                        index,
                        blob,
                        route=route,
                        since=since,
                        until=until,
                        ingest_run_id=ingest_run_id,
                    ),
                )
            )
    elif group == "hk" and sources.hkexnews is not None:
        hkexnews_deps = sources.hkexnews
        since_dt = datetime.combine(since, datetime.min.time(), tzinfo=UTC)
        until_dt = datetime.combine(until, datetime.min.time(), tzinfo=UTC)
        layers.append(
            Layer(
                "hkexnews",
                "hkexnews",
                False,
                lambda: run_hkexnews_layer(
                    index.conn,
                    deps=hkexnews_deps,
                    index=index,
                    entity_ref=route.entity_ref,
                    # 披露易的 prefix.do 按港交所标准 5 位代码精确匹配（"00020"，
                    # 不是 "0020"）；wind_code 里存的代码没有统一补零，2026-09-29
                    # 实盘验收对商汤科技（0020.HK）实测发现——zfill 只在不足 5 位
                    # 时补零，5 位数代码原样通过。
                    stock_code=route.entity_ref.split(".")[0].zfill(5),
                    since=since_dt,
                    until=until_dt,
                    tiers=tiers,
                    budget=budget,
                    download=download,
                    ingest_run_id=ingest_run_id,
                    dry_run=False,
                    cost_per_page_cny=cost_per_page_cny,
                ),
            )
        )
    elif group == "us" and sources.edgar is not None and route.cik is not None:
        edgar_deps = sources.edgar
        cik = route.cik
        layers.append(
            Layer(
                "edgar",
                "edgar",
                False,
                lambda: run_edgar_layer(
                    index.conn,
                    deps=edgar_deps,
                    index=index,
                    entity_ref=route.entity_ref,
                    cik=cik,
                    tiers=tiers,
                    budget=budget,
                    download=download,
                    ingest_run_id=ingest_run_id,
                    dry_run=False,
                    since=since,
                    cost_per_page_cny=cost_per_page_cny,
                ),
            )
        )
    return layers


def _last_failure(exc: AllLayersFailed) -> Failure:
    failures = [f for _, f in exc.attempts if f is not None]
    if failures:
        return failures[-1]
    return Failure(FailureKind.UNAVAILABLE, str(exc)[:500], timedelta(0))


def _degraded_placeholder_failure() -> Failure:
    """降级层本身成功了（有目录），只是没有全文——用一个中性的"待补"记号复用
    `Backlog.add` 的接口，不代表任何真实的失败分类。"""
    return Failure(FailureKind.REJECTED, "主源不可用，降级层已写目录，全文待补", timedelta(0))
