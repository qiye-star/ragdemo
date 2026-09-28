"""Wind 漏收核对（A 股，公告双源接入计划 Task 11）：Wind 有、巨潮目录没有 → 疑似漏收。
不写目录——这只是一次核对，不是又一条数据入库路径。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.wind_docs import WindDocsAdapter
from ragdemo.ingest.announcement_index import known_at_for_day, normalize_title
from ragdemo.quality.announcement_coverage import check_announcement_coverage
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, name_short, entity_type, market,"
        " l1_layer, l2_segment, l3_node, primary_node, tushare_code) VALUES"
        " ('CN.688041', '海光信息技术股份有限公司', '海光信息', 'listed', 'SSE', '算力',"
        "  'AI芯片', ARRAY['云端训练芯片'], '云端训练芯片', '688041.SH'),"
        " ('HK.09988', '阿里巴巴集团控股有限公司', '阿里巴巴', 'listed', 'HKEX', '应用',"
        "  '电商', ARRAY['电商平台'], '电商平台', NULL)"
    )
    c.commit()
    yield c
    c.close()


def _wind(items: list[dict[str, Any]]) -> WindDocsAdapter:
    gateway = MockMcpGateway(
        tools=[ToolSpec("get_company_announcements", "", {})],
        responses={"get_company_announcements": {"data": {"items": items}}},
    )
    return WindDocsAdapter(gateway)


def _insert_cninfo_index_row(
    conn: psycopg.Connection, *, entity_id: str, title: str, publish_date: date
) -> None:
    title_norm = normalize_title(title, ["海光信息", "海光"])
    conn.execute(
        "INSERT INTO core.announcement_index (entity_id, source, source_ref, title,"
        " title_norm, publish_date, has_full_text, valid_from, known_at, ingest_run_id)"
        " VALUES (%s,'cninfo',%s,%s,%s,%s,true,%s,%s,'t')",
        (
            entity_id,
            f"ref-{title}-{publish_date}",
            title,
            title_norm,
            publish_date,
            publish_date,
            known_at_for_day(publish_date),
        ),
    )
    conn.commit()


@pytest.mark.db
def test_wind_item_missing_from_cninfo_is_reported(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    wind = _wind(
        [
            {
                "title": "海光信息:2026年半年度报告",
                "date": "2026-08-14",
                "url": "https://wind.example/#/NA/abc123",
                "content": "摘录",
            }
        ]
    )
    gaps = check_announcement_coverage(
        conn,
        wind=wind,
        blob=LocalBlobStore(tmp_path),
        since=date(2026, 8, 1),
        until=date(2026, 8, 31),
        partition_date=date(2026, 9, 28),
        ingest_run_id="t",
    )
    assert len(gaps) == 1
    assert gaps[0].entity_ref == "688041.SH"
    assert gaps[0].publish_date == date(2026, 8, 14)

    metric = conn.execute(
        "SELECT value, passed, threshold FROM quality.quality_metric"
        " WHERE metric = 'announcement_coverage_gap'"
    ).fetchone()
    assert metric == (1.0, False, 0.0)


@pytest.mark.db
def test_matching_tolerates_one_day_offset(conn: psycopg.Connection, tmp_path: Path) -> None:
    _insert_cninfo_index_row(
        conn,
        entity_id="CN.688041",
        title="海光信息:2026年半年度报告",
        publish_date=date(2026, 8, 14),
    )
    wind = _wind(
        [
            {
                # Wind 报的日期比巨潮晚一天——同一份公告，两边对时间的口径本来就可能差一天。
                "title": "海光信息:2026年半年度报告",
                "date": "2026-08-15",
                "url": "https://wind.example/#/NA/abc123",
                "content": "摘录",
            }
        ]
    )
    gaps = check_announcement_coverage(
        conn,
        wind=wind,
        blob=LocalBlobStore(tmp_path),
        since=date(2026, 8, 1),
        until=date(2026, 8, 31),
        partition_date=date(2026, 9, 28),
        ingest_run_id="t",
    )
    assert gaps == []


@pytest.mark.db
def test_coverage_check_never_writes_the_index(conn: psycopg.Connection, tmp_path: Path) -> None:
    wind = _wind(
        [
            {
                "title": "海光信息:2026年半年度报告",
                "date": "2026-08-14",
                "url": "https://wind.example/#/NA/abc123",
                "content": "摘录",
            }
        ]
    )
    before = conn.execute("SELECT count(*) FROM core.announcement_index").fetchone()
    check_announcement_coverage(
        conn,
        wind=wind,
        blob=LocalBlobStore(tmp_path),
        since=date(2026, 8, 1),
        until=date(2026, 8, 31),
        partition_date=date(2026, 9, 28),
        ingest_run_id="t",
    )
    after = conn.execute("SELECT count(*) FROM core.announcement_index").fetchone()
    assert before == after == (0,)


@pytest.mark.db
def test_coverage_check_only_runs_for_cn_market_entities(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    gateway = MockMcpGateway(
        tools=[ToolSpec("get_company_announcements", "", {})],
        responses={"get_company_announcements": {"data": {"items": []}}},
    )
    wind = WindDocsAdapter(gateway)
    check_announcement_coverage(
        conn,
        wind=wind,
        blob=LocalBlobStore(tmp_path),
        since=date(2026, 8, 1),
        until=date(2026, 8, 31),
        partition_date=date(2026, 9, 28),
        ingest_run_id="t",
    )
    # 只有 688041.SH（market='SSE'）该被问到；09988.HK 没有 tushare_code、market 也
    # 不在 A 股集合里，核对逻辑不该向 Wind 问它——一次调用对应一家实体一个月。
    assert len(gateway.calls) == 1
