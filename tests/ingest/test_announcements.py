"""公告路由编排（公告双源接入计划 Task 9）：按市场分派、A 股两层降级、待补、游标。

A 股原本设计了三层（cninfo → wind-docs → ifind-notice），2026-09-29 实盘验收时
发现这个账号的 Wind 服务范围只覆盖财务/行情/股东/分部数据、不含公告，
`wind-docs` 这一层已从路由里移除（ADR-0013 修订），现在只有 cninfo → ifind-notice
两层。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from ragdemo.adapters.cninfo import CninfoAdapter
from ragdemo.adapters.hkexnews import HkexnewsAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.ifind_notice import IfindNoticeAdapter
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.ingest.announcements import (
    DOWNLOAD_ENTITIES,
    AnnouncementRunReport,
    AnnouncementSources,
    EntityRoute,
    entity_routes,
    ingest_announcements,
    market_group,
)
from ragdemo.ingest.cninfo_docs import CninfoLayerDeps
from ragdemo.ingest.hkexnews_docs import HkexnewsLayerDeps
from ragdemo.ingest.parse_policy import ParseTier
from ragdemo.parse.textin import MockDocumentParser, PageBudget
from ragdemo.sources.backlog import Backlog
from ragdemo.sources.health import MemorySourceHealth
from ragdemo.sources.routing import Capability
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
TOPSEARCH_688041 = [{"code": "688041", "orgId": "9900048365", "zwjc": "海光信息"}]
HKEX_PREFIX = (
    'cb({"more":"0","stockInfo":[{"stockId":1000015694,"code":"09988","name":"阿里巴巴"}]});'
)


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) VALUES"
        " ('CN.688041', '海光信息', 'listed', 'SSE', '算力', 'AI芯片', ARRAY['通用服务器CPU'],"
        "  '通用服务器CPU', '688041.SH')"
    )
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, wind_code) VALUES"
        " ('HK.9988', '阿里巴巴', 'listed', 'HKEX', '算力', '算力服务', ARRAY['公有云'],"
        "  '公有云', '09988.HK')"
    )
    c.commit()
    yield c
    c.close()


@pytest.fixture
def state(temp_db: str, conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db, autocommit=True)
    yield c
    c.close()


def _http(handler: Any, base_url: str = "https://www.cninfo.com.cn") -> HttpClient:  # noqa: ANN401
    return HttpClient(
        "test",
        base_url,
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _cninfo_deps(handler: Any, blob: LocalBlobStore) -> CninfoLayerDeps:  # noqa: ANN401
    def static_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-fake")

    return CninfoLayerDeps(
        adapter=CninfoAdapter(_http(handler)),
        static_client=_http(static_handler, "https://static.cninfo.com.cn"),
        parser=MockDocumentParser(),
        blob=blob,
    )


def _cninfo_ok(rows: list[dict[str, object]]) -> Any:  # noqa: ANN401

    def handler(request: httpx.Request) -> httpx.Response:
        if "topSearch" in request.url.path:
            return httpx.Response(200, json=TOPSEARCH_688041)
        body = dict(httpx.QueryParams(request.content.decode()))
        if body["column"] == "sse":
            return httpx.Response(200, json={"announcements": [], "totalpages": 0})
        return httpx.Response(200, json={"announcements": rows, "totalpages": 1})

    return handler


def _cninfo_down() -> Any:  # noqa: ANN401
    def handler(request: httpx.Request) -> httpx.Response:
        if "topSearch" in request.url.path:
            return httpx.Response(200, json=TOPSEARCH_688041)
        return httpx.Response(500, json={"error": "boom"})

    return handler


def _ifind(payload: Any = None, failing: bool = False) -> IfindNoticeAdapter:  # noqa: ANN401
    gateway = MockMcpGateway(
        tools=[ToolSpec("search_notice", "", {})],
        responses={} if failing else {"search_notice": payload},
        failing={"search_notice": "无权限"} if failing else {},
    )
    return IfindNoticeAdapter(gateway)


CN_ANNOUNCEMENT = {
    "secCode": "688041",
    "orgId": "9900048365",
    "announcementId": "1",
    "announcementTitle": "海光信息技术股份有限公司2025年第三季度报告",
    "announcementTime": 1767024000000,
    "adjunctUrl": "finalpage/2026-09-15/1.PDF",
    "adjunctType": "PDF",
}
IFIND_ANNOUNCEMENT = {
    "data": (
        '[{"公告标题": "海光信息：董事会决议公告", "公告片段内容": "摘录", "日期": "2026-08-20"}]'
    )
}


def _route_cn() -> EntityRoute:
    return EntityRoute(entity_ref="688041.SH", short_name="海光信息", market="SSE")


def _sources(
    *,
    cninfo: CninfoLayerDeps | None = None,
    ifind: IfindNoticeAdapter | None = None,
) -> AnnouncementSources:
    return AnnouncementSources(cninfo=cninfo, ifind=ifind)


def _run(
    conn: psycopg.Connection,
    health: MemorySourceHealth,
    backlog: Backlog,
    sources: AnnouncementSources,
    tmp_path: Path,
    *,
    entities: list[EntityRoute] | None = None,
    partition_date: date = date(2026, 9, 28),
    backfill_since: date = date(2026, 1, 1),
    now: datetime = NOW,
) -> AnnouncementRunReport:
    return ingest_announcements(
        conn,
        entities=entities or [_route_cn()],
        sources=sources,
        blob=LocalBlobStore(tmp_path / "degraded-blob"),
        health=health,
        backlog=backlog,
        partition_date=partition_date,
        backfill_since=backfill_since,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        ingest_run_id="t",
        now=lambda: now,
    )


@pytest.mark.db
def test_cninfo_healthy_writes_index_and_advances_watermark(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    health, backlog = MemorySourceHealth(), Backlog(state)
    deps = _cninfo_deps(_cninfo_ok([CN_ANNOUNCEMENT]), LocalBlobStore(tmp_path))
    report = _run(conn, health, backlog, _sources(cninfo=deps), tmp_path)
    conn.commit()
    assert report.indexed == 1 and report.failed == {} and report.backlogged == {}
    live = conn.execute(
        "SELECT source FROM core.announcement_index WHERE superseded_at IS NULL"
    ).fetchall()
    assert live == [("cninfo",)]

    # 同一分区重跑：游标已经推进到 partition_date，不应再发起任何请求。
    calls: list[str] = []

    def _record_500(r: httpx.Request) -> httpx.Response:
        calls.append(r.url.path)
        return httpx.Response(500)

    empty_deps = _cninfo_deps(_record_500, LocalBlobStore(tmp_path))
    _run(conn, health, backlog, _sources(cninfo=empty_deps), tmp_path)
    assert calls == []


@pytest.mark.db
def test_cninfo_down_falls_back_to_ifind_and_backlogs_full_text(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    health, backlog = MemorySourceHealth(), Backlog(state)
    deps = _cninfo_deps(_cninfo_down(), LocalBlobStore(tmp_path))
    report = _run(
        conn, health, backlog, _sources(cninfo=deps, ifind=_ifind(IFIND_ANNOUNCEMENT)), tmp_path
    )
    conn.commit()
    assert report.indexed == 1 and "688041.SH" in report.backlogged
    row = conn.execute(
        "SELECT source, has_full_text FROM core.announcement_index WHERE superseded_at IS NULL"
    ).fetchone()
    assert row == ("ifind", False)
    (item,) = backlog.open_items(Capability.ANNOUNCEMENTS_CN)
    assert item.entity_ref == "688041.SH"


@pytest.mark.db
def test_hk_entity_routes_to_hkexnews_not_cninfo(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    import json as _json

    health, backlog = MemorySourceHealth(), Backlog(state)

    def hk_handler(request: httpx.Request) -> httpx.Response:
        if "prefix.do" in request.url.path:
            return httpx.Response(200, text=HKEX_PREFIX)
        return httpx.Response(200, json={"recordCnt": 0, "result": _json.dumps([])})

    hk_deps = HkexnewsLayerDeps(
        adapter=HkexnewsAdapter(_http(hk_handler, "https://www1.hkexnews.hk")),
        static_client=_http(
            lambda r: httpx.Response(200, content=b"x"), "https://www1.hkexnews.hk"
        ),
        parser=MockDocumentParser(),
        blob=LocalBlobStore(tmp_path),
    )
    sources = AnnouncementSources(hkexnews=hk_deps)
    route = EntityRoute(entity_ref="09988.HK", short_name="阿里巴巴", market="HKEX")
    report = _run(conn, health, backlog, sources, tmp_path, entities=[route])
    assert report.indexed == 0 and report.failed == {} and report.warnings == []


@pytest.mark.db
def test_hk_stock_code_is_zero_padded_to_five_digits_for_hkexnews(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    """`wind_code` 里存的港股代码没有统一补零到披露易要求的 5 位（"0020" 不是
    "00020"）——2026-09-29 实盘验收对商汤科技（0020.HK）实测发现
    `prefix.do` 精确匹配 5 位代码，传 "0020" 会报"找不到 stockId"。"""
    import json as _json

    health, backlog = MemorySourceHealth(), Backlog(state)
    seen_names: list[str] = []

    def hk_handler(request: httpx.Request) -> httpx.Response:
        if "prefix.do" in request.url.path:
            seen_names.append(dict(request.url.params).get("name", ""))
            return httpx.Response(
                200,
                text=(
                    'cb({"more":"0","stockInfo":'
                    '[{"stockId":1000000001,"code":"00020","name":"商汤科技"}]});'
                ),
            )
        return httpx.Response(200, json={"recordCnt": 0, "result": _json.dumps([])})

    hk_deps = HkexnewsLayerDeps(
        adapter=HkexnewsAdapter(_http(hk_handler, "https://www1.hkexnews.hk")),
        static_client=_http(
            lambda r: httpx.Response(200, content=b"x"), "https://www1.hkexnews.hk"
        ),
        parser=MockDocumentParser(),
        blob=LocalBlobStore(tmp_path),
    )
    sources = AnnouncementSources(hkexnews=hk_deps)
    route = EntityRoute(entity_ref="0020.HK", short_name="商汤科技", market="HKEX")
    report = _run(conn, health, backlog, sources, tmp_path, entities=[route])
    assert report.failed == {}
    assert seen_names == ["00020"]


@pytest.mark.db
def test_unsupported_market_is_skipped_with_a_warning_not_a_failure(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    health, backlog = MemorySourceHealth(), Backlog(state)
    route = EntityRoute(entity_ref="005930.KS", short_name="三星电子", market="KRX")
    report = _run(conn, health, backlog, AnnouncementSources(), tmp_path, entities=[route])
    assert report.failed == {} and any("005930.KS" in w for w in report.warnings)


def test_market_group_maps_by_market_or_cik() -> None:
    assert market_group(EntityRoute("688041.SH", "海光信息", "SSE")) == "cn"
    assert market_group(EntityRoute("09988.HK", "阿里巴巴", "HKEX")) == "hk"
    assert market_group(EntityRoute("NVDA", "英伟达", "NASDAQ")) == "us"
    assert market_group(EntityRoute("2330.TW", "台积电", "TWSE", cik=1046179)) == "us"
    assert market_group(EntityRoute("005930.KS", "三星电子", "KRX")) is None


def test_download_entities_pilot_scope() -> None:
    assert {"688041.SH", "000977.SZ"} == DOWNLOAD_ENTITIES


@pytest.mark.db
def test_entity_routes_picks_the_right_code_column_per_market(conn: psycopg.Connection) -> None:
    """CN.688041/HK.9988 由 `conn` 夹具预先插入；这里再补一家 edgar_cik 优先于
    market 判断的实体，和一家两列都没有、该被跳过的实体。"""
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, edgar_cik) VALUES"
        " ('US.NVDA', 'NVIDIA', 'listed', 'NASDAQ', '算力', 'AI芯片', ARRAY['GPU'], 'GPU',"
        "  '1045810')"
    )
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node) VALUES"
        " ('KR.005930', '三星电子', 'listed', 'KRX', '算力', '存储芯片', ARRAY['DRAM'], 'DRAM')"
    )
    conn.commit()

    routes = {r.entity_ref: r for r in entity_routes(conn)}
    assert routes["688041.SH"].market == "SSE"
    assert routes["09988.HK"].market == "HKEX"
    assert routes["1045810"].cik == 1045810
    # 三星既没有 tushare_code 也没有 wind_code——两列都缺，该被跳过，不是错误。
    assert len(routes) == 3


CN_ANNOUNCEMENT_SAME_AS_IFIND = {
    "secCode": "688041",
    "orgId": "9900048365",
    "announcementId": "2",
    # 与 IFIND_ANNOUNCEMENT 归一化后同一份公告：同实体、同日、去掉"海光信息:"/
    # "海光信息："前缀后标题相同——巨潮恢复时应该取代 ifind 那一行，而不是并存两条。
    "announcementTitle": "海光信息:董事会决议公告",
    "announcementTime": 1787155200000,  # 2026-08-20 00:00 北京时间
    "adjunctUrl": "finalpage/2026-08-20/2.PDF",
    "adjunctType": "PDF",
}


@pytest.mark.db
def test_recovery_refetches_backlog_window_and_supersedes_degraded_rows(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    health, backlog = MemorySourceHealth(), Backlog(state)
    down_deps = _cninfo_deps(_cninfo_down(), LocalBlobStore(tmp_path))
    _run(
        conn,
        health,
        backlog,
        _sources(cninfo=down_deps, ifind=_ifind(IFIND_ANNOUNCEMENT)),
        tmp_path,
        partition_date=date(2026, 9, 15),
        backfill_since=date(2026, 8, 1),
    )
    conn.commit()
    assert backlog.open_items(Capability.ANNOUNCEMENTS_CN) != []

    later = date(2026, 9, 28)
    seen_since: list[str] = []

    def capturing_handler(request: httpx.Request) -> httpx.Response:
        if "hisAnnouncement" in request.url.path:
            body = dict(httpx.QueryParams(request.content.decode()))
            seen_since.append(body.get("seDate", ""))
        response: httpx.Response = _cninfo_ok([CN_ANNOUNCEMENT_SAME_AS_IFIND])(request)
        return response

    ok_deps = _cninfo_deps(capturing_handler, LocalBlobStore(tmp_path))
    _run(
        conn,
        health,
        backlog,
        _sources(cninfo=ok_deps, ifind=_ifind(IFIND_ANNOUNCEMENT)),
        tmp_path,
        partition_date=later,
        backfill_since=date(2026, 8, 1),
        now=NOW + timedelta(hours=1),  # 冷却期（不可用默认 5 分钟）已过
    )
    conn.commit()
    assert backlog.open_items(Capability.ANNOUNCEMENTS_CN) == []
    assert any(s.startswith("2026-08") for s in seen_since), (
        "巨潮恢复后要把区间补回到最早的缺口起点，不是只从上次成功的 partition_date 继续"
    )
    ifind_row = conn.execute(
        "SELECT superseded_at IS NOT NULL FROM core.announcement_index WHERE source = 'ifind'"
    ).fetchone()
    assert ifind_row == (True,), "巨潮的行到达后，ifind 那条降级行要被取代"


@pytest.mark.db
def test_only_download_entities_get_full_text_others_index_only(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    health, backlog = MemorySourceHealth(), Backlog(state)
    deps = _cninfo_deps(_cninfo_ok([CN_ANNOUNCEMENT]), LocalBlobStore(tmp_path))
    _run(conn, health, backlog, _sources(cninfo=deps), tmp_path)
    conn.commit()
    row = conn.execute("SELECT has_full_text FROM core.announcement_index").fetchone()
    assert row == (True,), "688041.SH 在 DOWNLOAD_ENTITIES 里，应该拿到全文"


def _recording_handler(rows: list[dict[str, object]], seen: list[str]) -> Any:  # noqa: ANN401
    inner = _cninfo_ok(rows)

    def handler(request: httpx.Request) -> httpx.Response:
        if "topSearch" not in request.url.path:
            body = dict(httpx.QueryParams(request.content.decode()))
            if body["column"] != "sse":
                seen.append(body["seDate"])
        response: httpx.Response = inner(request)
        return response

    return handler


@pytest.mark.db
def test_earlier_backfill_since_refetches_the_uncovered_earlier_window_once(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    """回归：游标只往后追，首跑窗口之前的公告永远补不回来（688041 从 2024-04-12
    起、000977 从 2025-02-18 起）。覆盖下界（floor）没到 backfill_since 要整段重取。"""
    health, backlog = MemorySourceHealth(), Backlog(state)
    seen: list[str] = []

    def deps() -> CninfoLayerDeps:
        return _cninfo_deps(_recording_handler([CN_ANNOUNCEMENT], seen), LocalBlobStore(tmp_path))

    _run(conn, health, backlog, _sources(cninfo=deps()), tmp_path, backfill_since=date(2026, 6, 1))
    assert seen[-1].startswith("2026-06-01")

    # 第二天、更早的 backfill_since：必须从 2026-01-01 起整段重取。
    _run(
        conn,
        health,
        backlog,
        _sources(cninfo=deps()),
        tmp_path,
        partition_date=date(2026, 9, 29),
        backfill_since=date(2026, 1, 1),
    )
    assert seen[-1].startswith("2026-01-01")

    # 第三天：下界已到 2026-01-01，只追新（游标次日）。
    _run(
        conn,
        health,
        backlog,
        _sources(cninfo=deps()),
        tmp_path,
        partition_date=date(2026, 9, 30),
        backfill_since=date(2026, 1, 1),
    )
    assert seen[-1].startswith("2026-09-30")


@pytest.mark.db
def test_legacy_cursor_without_floor_is_refetched_from_backfill_since(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    from ragdemo.ingest.reconcile import write_watermark

    write_watermark(conn, "announcements:cn:688041.SH", date(2026, 9, 28), "2026-09-28")
    conn.commit()
    seen: list[str] = []
    deps = _cninfo_deps(_recording_handler([CN_ANNOUNCEMENT], seen), LocalBlobStore(tmp_path))
    _run(
        conn,
        MemorySourceHealth(),
        Backlog(state),
        _sources(cninfo=deps),
        tmp_path,
        partition_date=date(2026, 9, 29),
        backfill_since=date(2026, 1, 1),
    )
    assert seen[-1].startswith("2026-01-01")
