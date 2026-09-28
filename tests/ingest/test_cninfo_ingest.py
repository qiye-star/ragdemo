"""巨潮资讯网公告入库（`ragdemo docs ingest-cninfo --yes` 的实际工作，adr/0013）。

公告列表与每份 PDF 都要留 provider_snapshot（CLAUDE.md §1.5）：列表响应内联存
jsonb；PDF 字节已经在 blob 里，快照只记 `response_ref` 指向它——与 EDGAR 全文
入库（`tests/ingest/test_edgar_ingest.py`）同一个模式。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest

from ragdemo.adapters.cninfo import CninfoAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.ingest.cninfo_docs import ingest_cninfo_announcements, list_announcements
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

TOPSEARCH_688041 = [{"code": "688041", "orgId": "9900048365", "zwjc": "海光信息"}]
ANNOUNCEMENT_A = {
    "secCode": "688041",
    "orgId": "9900048365",
    "announcementId": "1224906228",
    "announcementTitle": "海光信息技术股份有限公司2025年第三次临时股东会决议公告",
    "announcementTime": 1767024000000,
    "adjunctUrl": "finalpage/2025-12-30/1224906228.PDF",
    "adjunctType": "PDF",
}
ANNOUNCEMENT_B = {
    "secCode": "688041",
    "orgId": "9900048365",
    "announcementId": "1224906245",
    "announcementTitle": "北京市中伦律师事务所关于海光信息技术股份有限公司"
    "2025年第三次临时股东会的法律意见书",
    "announcementTime": 1767024000000,
    "adjunctUrl": "finalpage/2025-12-30/1224906245.PDF",
    "adjunctType": "PDF",
}


@pytest.fixture
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code)"
        " VALUES ('CN.688041', '海光信息技术股份有限公司', 'listed', 'SSE', '算力',"
        " 'AI芯片', ARRAY['云端训练芯片'], '云端训练芯片', '688041.SH')"
    )
    c.commit()
    return c


def _query_client() -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "topSearch" in request.url.path:
            return httpx.Response(200, json=TOPSEARCH_688041)
        body = dict(httpx.QueryParams(request.content.decode()))
        if body["column"] == "sse":
            return httpx.Response(200, json={"announcements": [], "totalpages": 0})
        return httpx.Response(
            200,
            json={"announcements": [ANNOUNCEMENT_A, ANNOUNCEMENT_B], "totalpages": 1},
        )

    return HttpClient(
        "cninfo",
        "https://www.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _static_client(calls: list[str]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=f"%PDF-fake {request.url.path}".encode())

    return HttpClient(
        "cninfo-static",
        "https://static.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _window() -> tuple[datetime, datetime]:
    return datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)


def test_list_announcements_does_not_touch_the_database() -> None:
    since, until = _window()
    refs = list_announcements(
        CninfoAdapter(_query_client()),
        stock_code="688041",
        since=since,
        until=until,
        ingest_run_id="t",
    )
    assert {r.announcement_id for r in refs} == {"1224906228", "1224906245"}


@pytest.mark.db
def test_list_and_every_pdf_are_snapshotted(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    calls: list[str] = []
    since, until = _window()
    written = ingest_cninfo_announcements(
        conn,
        adapter=CninfoAdapter(_query_client()),
        static_client=_static_client(calls),
        blob=blob,
        stock_code="688041",
        entity_ref="688041.SH",
        source="cninfo",
        since=since,
        until=until,
        limit=None,
        ingest_run_id="t",
    )
    assert len(written) == 2 and len(calls) == 2

    list_snapshots = conn.execute(
        "SELECT count(*) FROM core.provider_snapshot"
        " WHERE provider = 'cninfo' AND response IS NOT NULL"
    ).fetchone()
    assert list_snapshots is not None and list_snapshots[0] >= 1, "公告列表响应也要留底"

    rows = conn.execute(
        "SELECT d.raw_ref, d.entity_id, s.response_ref, s.provider"
        "  FROM core.document d JOIN core.provider_snapshot s USING (snapshot_id)"
        " WHERE d.source = 'cninfo'"
    ).fetchall()
    assert len(rows) == 2, "每份公告文档都必须带 snapshot_id"
    for raw_ref, entity_id, response_ref, provider in rows:
        assert entity_id == "CN.688041"
        assert provider == "cninfo-static"
        assert response_ref == raw_ref
        assert blob.get(cast(str, response_ref)).startswith(b"%PDF-fake")


@pytest.mark.db
def test_known_at_is_the_day_end_not_midnight(conn: psycopg.Connection, tmp_path: Path) -> None:
    """回归测试：真正落库的 known_at 要经过 source_registry.time_precision='day'
    的保守上界换算，不能是巨潮原始给的当日 0 点（这正是 adr/0013 发现的 bug）。
    """
    blob = LocalBlobStore(tmp_path)
    since, until = _window()
    ingest_cninfo_announcements(
        conn,
        adapter=CninfoAdapter(_query_client()),
        static_client=_static_client([]),
        blob=blob,
        stock_code="688041",
        entity_ref="688041.SH",
        source="cninfo",
        since=since,
        until=until,
        limit=1,
        ingest_run_id="t",
    )
    row = conn.execute(
        "SELECT known_at, publish_at FROM core.document WHERE source = 'cninfo' LIMIT 1"
    ).fetchone()
    assert row is not None
    known_at, publish_at = row
    local_publish_at = publish_at.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local_publish_at.hour, local_publish_at.minute) == (0, 0), (
        "原始 publish_at 就是巨潮给的午夜（北京时间）"
    )
    local_known_at = known_at.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local_known_at.hour, local_known_at.minute, local_known_at.second) == (23, 59, 59), (
        "known_at 必须是发布当日 23:59:59（北京时间）保守上界，不是原始午夜"
    )


@pytest.mark.db
def test_limit_caps_how_many_documents_get_written(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    blob = LocalBlobStore(tmp_path)
    since, until = _window()
    written = ingest_cninfo_announcements(
        conn,
        adapter=CninfoAdapter(_query_client()),
        static_client=_static_client([]),
        blob=blob,
        stock_code="688041",
        entity_ref="688041.SH",
        source="cninfo",
        since=since,
        until=until,
        limit=1,
        ingest_run_id="t",
    )
    assert len(written) == 1
