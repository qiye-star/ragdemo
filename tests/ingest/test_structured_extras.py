"""股东 / 分部 / 汇率入库编排（阶段二 Task 7）。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from ragdemo.adapters.cfets import CfetsAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.wind_structured import (
    FUNDAMENTALS_TOOL,
    HOLDERS_TOOL,
    WindHoldersAdapter,
    WindSegmentsAdapter,
)
from ragdemo.ingest.structured_extras import (
    StructuredEntity,
    a_share_entities,
    ingest_fx,
    ingest_holders,
    ingest_segments,
    periods_back,
)
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
FIXTURES = Path("tests/fixtures/wind")
NOW = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
ENT = StructuredEntity("CN.688041", "688041.SH", "海光信息")


def _payload(name: str) -> Any:  # noqa: ANN401
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["payload"]


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        c.execute(
            "INSERT INTO core.entity (entity_id, name_full, name_short, entity_type, market,"
            " l1_layer, l2_segment, l3_node, primary_node, tushare_code) VALUES"
            " ('CN.688041', '海光信息技术股份有限公司', '海光信息', 'listed', 'SSE', '算力',"
            " 'AI芯片', ARRAY['通用服务器CPU'], '通用服务器CPU', '688041.SH')"
        )
        c.commit()
        yield c
        c.close()


def _announce(conn: psycopg.Connection, title: str, published: str) -> None:
    day = date.fromisoformat(published)
    known = datetime(day.year, day.month, day.day, 15, 59, 59, tzinfo=UTC)
    conn.execute(
        "INSERT INTO core.announcement_index (entity_id, source, source_ref, title, title_norm,"
        " publish_date, has_full_text, valid_from, known_at, ingest_run_id) VALUES"
        " ('CN.688041', 'cninfo', %s, %s, %s, %s, false, %s, %s, 't')",
        (title, title, title, day, day, known),
    )
    conn.commit()


def _holders_adapter(
    payload: dict[str, Any] | None = None, *, failing: bool = False
) -> tuple[WindHoldersAdapter, MockMcpGateway]:
    gateway = MockMcpGateway(
        tools=[ToolSpec(HOLDERS_TOOL, "", {})],
        responses={} if failing else {HOLDERS_TOOL: payload or _payload("holders_688041_2024fy")},
        failing={HOLDERS_TOOL: "积分不足"} if failing else {},
    )
    return WindHoldersAdapter(gateway), gateway


def _segments_adapter() -> tuple[WindSegmentsAdapter, MockMcpGateway]:
    gateway = MockMcpGateway(
        tools=[ToolSpec(FUNDAMENTALS_TOOL, "", {})],
        responses={FUNDAMENTALS_TOOL: _payload("segments_688041_2024fy")},
    )
    return WindSegmentsAdapter(gateway), gateway


def _count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_periods_back() -> None:
    assert periods_back(date(2026, 9, 29), 4, ("FY", "H1", "Q1", "Q3")) == [
        "2026H1",
        "2026Q1",
        "2025FY",
        "2025Q3",
    ]
    assert periods_back(date(2026, 9, 29), 3, ("FY", "H1")) == ["2026H1", "2025FY", "2025H1"]
    assert "2022FY" in periods_back(date(2026, 9, 29), 50, ("FY",))  # 2022-12-31 之后才算
    assert all(p >= "2023" for p in periods_back(date(2026, 9, 29), 50, ("H1", "Q1", "Q3")))


@pytest.mark.db
def test_a_share_entities_lists_listed_a_shares(conn: psycopg.Connection) -> None:
    assert a_share_entities(conn) == [ENT]
    assert a_share_entities(conn, ["000977.SZ"]) == []


@pytest.mark.db
def test_holders_without_a_disclosure_record_never_call_wind(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    adapter, gateway = _holders_adapter()
    summary = ingest_holders(
        conn,
        adapter=adapter,
        blob=LocalBlobStore(tmp_path),
        entities=[ENT],
        periods=["2024FY"],
        ingest_run_id="t",
        now=lambda: NOW,
    )
    assert summary.skipped_no_disclosure == ["688041.SH/2024FY"]
    assert gateway.calls == [] and summary.inserted == 0


@pytest.mark.db
def test_holders_are_written_with_first_disclosure_known_at_and_a_snapshot(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    _announce(conn, "海光信息技术股份有限公司2024年年度报告", "2025-03-01")
    _announce(conn, "海光信息技术股份有限公司2024年年度报告(更正后)", "2025-04-20")
    adapter, gateway = _holders_adapter()
    kwargs: dict[str, Any] = dict(
        adapter=adapter,
        blob=LocalBlobStore(tmp_path),
        entities=[ENT],
        periods=["2024FY"],
        ingest_run_id="t",
        now=lambda: NOW,
    )
    summary = ingest_holders(conn, **kwargs)
    assert summary.inserted == 1 and summary.wind_calls == 1
    rows = conn.execute(
        "SELECT count(*), min(known_at), max(known_at), min(snapshot_id) IS NOT NULL,"
        " min(known_at_basis) FROM core.shareholder_holding"
    ).fetchone()
    assert rows == (
        10,
        datetime(2025, 3, 1, 15, 59, 59, tzinfo=UTC),
        datetime(2025, 3, 1, 15, 59, 59, tzinfo=UTC),
        True,
        "first_periodic_report",
    )
    snap = conn.execute(
        "SELECT DISTINCT s.provider, s.endpoint FROM core.shareholder_holding h"
        " JOIN core.provider_snapshot s USING (snapshot_id)"
    ).fetchall()
    assert snap == [("wind", HOLDERS_TOOL)]

    # 重跑：库里已有 → 不再调用 Wind
    again = ingest_holders(conn, **kwargs)
    assert again.already_present == 1 and again.wind_calls == 0 and len(gateway.calls) == 1
    # 显式刷新：调用 Wind，内容相同 → UNCHANGED，行数不变
    refreshed = ingest_holders(conn, refresh=True, **kwargs)
    assert refreshed.unchanged == 1 and _count(conn, "core.shareholder_holding") == 10


@pytest.mark.db
def test_a_rejected_response_keeps_the_snapshot_but_writes_nothing(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    _announce(conn, "海光信息技术股份有限公司2024年年度报告", "2025-03-01")
    bad = _payload("holders_688041_2024fy")
    bad["data"]["data"][0]["rows"][3][5] = 9  # 名次缺号
    adapter, _ = _holders_adapter(bad)
    summary = ingest_holders(
        conn,
        adapter=adapter,
        blob=LocalBlobStore(tmp_path),
        entities=[ENT],
        periods=["2024FY"],
        ingest_run_id="t",
        now=lambda: NOW,
    )
    assert "688041.SH/2024FY" in summary.rejected and summary.inserted == 0
    assert _count(conn, "core.shareholder_holding") == 0
    assert _count(conn, "core.provider_snapshot") == 1  # 原始响应仍然留档


@pytest.mark.db
def test_repeated_gateway_failures_abort_the_batch(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    for year in (2023, 2024, 2025):
        _announce(conn, f"海光信息技术股份有限公司{year}年年度报告", f"{year + 1}-03-01")
    adapter, gateway = _holders_adapter(failing=True)
    summary = ingest_holders(
        conn,
        adapter=adapter,
        blob=LocalBlobStore(tmp_path),
        entities=[ENT],
        periods=["2025FY", "2024FY", "2023FY", "2022FY"],
        ingest_run_id="t",
        now=lambda: NOW,
    )
    assert summary.aborted is not None and len(gateway.calls) == 3


def _seg_kwargs(conn: psycopg.Connection, tmp_path: Path, lookup: Any) -> dict[str, Any]:  # noqa: ANN401
    adapter, _ = _segments_adapter()
    return dict(
        adapter=adapter,
        blob=LocalBlobStore(tmp_path),
        entities=[ENT],
        periods=["2024FY"],
        ingest_run_id="t",
        now=lambda: NOW,
        revenue_lookup=lookup,
    )


@pytest.mark.db
def test_segments_reconciled_with_fin_fact_revenue_are_written(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    _announce(conn, "海光信息技术股份有限公司2024年年度报告", "2025-03-01")
    summary = ingest_segments(conn, **_seg_kwargs(conn, tmp_path, lambda *_: Decimal("9162150000")))
    assert summary.inserted == 1 and summary.unreconciled_to_fin_fact == []
    dims = conn.execute(
        "SELECT dimension, count(*) FROM core.segment_revenue GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert dims == [("product", 2), ("region", 1)]


@pytest.mark.db
def test_segments_that_disagree_with_fin_fact_revenue_are_rejected(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    _announce(conn, "海光信息技术股份有限公司2024年年度报告", "2025-03-01")
    summary = ingest_segments(
        conn,
        **_seg_kwargs(conn, tmp_path, lambda *_: Decimal("14376890000")),  # 2025 年的营收
    )
    assert "688041.SH/2024FY" in summary.rejected and _count(conn, "core.segment_revenue") == 0


@pytest.mark.db
def test_segments_without_fin_fact_are_flagged_not_silently_trusted(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    _announce(conn, "海光信息技术股份有限公司2024年年度报告", "2025-03-01")
    summary = ingest_segments(conn, **_seg_kwargs(conn, tmp_path, lambda *_: None))
    assert summary.inserted == 1
    assert summary.unreconciled_to_fin_fact == ["688041.SH/2024FY"]


def _fx_adapter(status: int = 200) -> CfetsAdapter:
    fixture = {
        "USD/CNY": "ccpr_usdcny.json",
        "100JPY/CNY": "ccpr_jpycny.json",
        "CNY/KRW": "ccpr_cnykrw.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="<html>403</html>")
        body = (Path("tests/fixtures/cfets") / fixture[request.url.params["currency"]]).read_text(
            encoding="utf-8"
        )
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    client = HttpClient(
        "cfets",
        "https://www.chinamoney.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )
    return CfetsAdapter(client)


@pytest.mark.db
def test_fx_ingest_writes_normalised_rates_with_snapshots_and_is_idempotent(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    kwargs: dict[str, Any] = dict(
        adapter=_fx_adapter(),
        blob=LocalBlobStore(tmp_path),
        start=date(2026, 9, 14),
        end=date(2026, 9, 29),
        ingest_run_id="t",
        now=lambda: NOW,
        pairs=("USD/CNY", "100JPY/CNY", "CNY/KRW"),
    )
    first = ingest_fx(conn, **kwargs)
    assert first.aborted is None and first.inserted > 20 and first.revised == 0
    ccys = conn.execute("SELECT DISTINCT base_ccy FROM core.fx_rate ORDER BY 1").fetchall()
    assert ccys == [("JPY",), ("KRW",), ("USD",)]
    orphan = conn.execute("SELECT count(*) FROM core.fx_rate WHERE snapshot_id IS NULL").fetchone()
    assert orphan == (0,)
    second = ingest_fx(conn, **kwargs)
    assert second.inserted == 0 and second.unchanged == first.inserted


@pytest.mark.db
def test_fx_waf_block_aborts_without_writing_garbage(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    summary = ingest_fx(
        conn,
        adapter=_fx_adapter(403),
        blob=LocalBlobStore(tmp_path),
        start=date(2026, 9, 14),
        end=date(2026, 9, 29),
        ingest_run_id="t",
        now=lambda: NOW,
        pairs=("USD/CNY", "CNY/KRW"),
    )
    assert summary.aborted is not None and _count(conn, "core.fx_rate") == 0
