"""GET /api/quality/* 与 /api/parse/* 集成测试：口径必须与既有、已测的
`ragdemo.parse.router` 函数完全对齐——用它们当 oracle，而不是在接口层
重新实现一遍判定逻辑。两处口径漂了，应该是这里的断言先红。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from ragdemo.parse.router import RetryQueueEntry, load_active_policy, monthly_pages_spent

AS_OF = "2025-06-01T00:00:00Z"
AS_OF_DT = datetime(2025, 6, 1, tzinfo=UTC)


@pytest.mark.db
def test_quality_dashboard_excludes_rows_computed_after_as_of(
    client: TestClient, api_db: tuple[str, str]
) -> None:
    admin_dsn, _ = api_db
    early = datetime(2025, 5, 1, tzinfo=UTC)
    late = datetime(2025, 7, 1, tzinfo=UTC)  # 晚于 AS_OF——不该出现在结果里
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(
            "INSERT INTO quality.quality_metric"
            " (metric, source_id, partition_date, value, threshold, passed, computed_at)"
            " VALUES ('parse_success_rate', 'test-src', '2025-04-30', 0.99, 0.98, true, %s),"
            "        ('parse_success_rate', 'test-src', '2025-06-30', 0.10, 0.98, false, %s)",
            (early, late),
        )
        conn.commit()

    r = client.get("/api/quality/dashboard", params={"as_of": AS_OF})
    assert r.status_code == 200
    rows = [m for m in r.json()["metrics"] if m["source_id"] == "test-src"]
    assert len(rows) == 1
    assert rows[0]["value"] == pytest.approx(0.99)


@pytest.mark.db
def test_parse_policies_active_row_matches_load_active_policy(
    client: TestClient, api_db: tuple[str, str]
) -> None:
    admin_dsn, _ = api_db
    with psycopg.connect(admin_dsn) as conn:
        # 先插一条已被取代的旧策略，再插一条当前生效的——active_at_as_of
        # 必须精确指向后者，且它的 policy_id 必须与 load_active_policy
        # 直接查出来的结果一致。
        conn.execute(
            "INSERT INTO core.parse_tier_policy"
            " (doc_type, confidence_below, closure_below, monthly_cap_cny, enabled,"
            "  known_at, superseded_at)"
            " VALUES ('quarterly', 0.6, 0.85, 100, true,"
            "         '2025-01-01T00:00:00+00', '2025-03-01T00:00:00+00')"
        )
        (active_policy_id,) = conn.execute(
            "INSERT INTO core.parse_tier_policy"
            " (doc_type, confidence_below, closure_below, monthly_cap_cny, enabled, known_at)"
            " VALUES ('quarterly', 0.7, 0.9, 200, true, '2025-03-01T00:00:00+00')"
            " RETURNING policy_id"
        ).fetchone()  # type: ignore[misc]
        conn.commit()

        oracle = load_active_policy(conn, "quarterly", AS_OF_DT)
        assert oracle is not None
        assert oracle.policy_id == active_policy_id

    r = client.get("/api/parse/policies", params={"as_of": AS_OF})
    assert r.status_code == 200
    quarterly_rows = [p for p in r.json()["policies"] if p["doc_type"] == "quarterly"]
    active_rows = [p for p in quarterly_rows if p["active_at_as_of"]]
    assert len(active_rows) == 1
    assert active_rows[0]["policy_id"] == active_policy_id
    superseded_rows = [p for p in quarterly_rows if not p["active_at_as_of"]]
    assert len(superseded_rows) == 1


@pytest.mark.db
def test_budget_total_matches_monthly_pages_spent_including_duplicates(
    client: TestClient, api_db: tuple[str, str]
) -> None:
    """裸 sum(value)，哪怕会把重试写入的重复行算两遍——界面显示的数必须
    等于闸门用的数，这是暴露问题，不是修问题。"""
    admin_dsn, _ = api_db
    with psycopg.connect(admin_dsn) as conn:
        # 同一天两行——模拟一次 run 重试导致的重复写入。
        conn.execute(
            "INSERT INTO quality.quality_metric"
            " (metric, source_id, partition_date, value, passed, computed_at)"
            " VALUES ('c_tier_pages', 'annual_report', '2025-06-10', 30, true,"
            "         '2025-06-10T00:00:00+00'),"
            "        ('c_tier_pages', 'annual_report', '2025-06-10', 30, true,"
            "         '2025-06-10T00:01:00+00')"
        )
        conn.commit()

        oracle = monthly_pages_spent(
            conn,
            doc_type="annual_report",
            month_start=datetime(2025, 6, 1, tzinfo=UTC),
            month_end=datetime(2025, 7, 1, tzinfo=UTC),
        )
        assert oracle == 60  # 两行都被算了进去，30+30，不是去重后的 30

    # 用晚于两行 computed_at 的 as_of——早于 computed_at 的话这两行
    # 按约束 4 的语义本就"还没被计算出来"，budget 接口正确地看不见它们，
    # 那不是 bug，是与 AS_OF（模块常量，2025-06-01）不搭配的测试数据。
    later_as_of = "2025-06-15T00:00:00Z"
    r = client.get("/api/parse/budget", params={"as_of": later_as_of})
    assert r.status_code == 200
    bucket = next(b for b in r.json()["buckets"] if b["doc_type"] == "annual_report")
    assert bucket["pages_spent"] == oracle
    assert bucket["rows"] == 2


@pytest.mark.db
def test_retry_queue_overdue_matches_entry_is_overdue(
    client: TestClient, api_db: tuple[str, str]
) -> None:
    admin_dsn, _ = api_db
    first_failed = datetime(2025, 5, 1, tzinfo=UTC)
    deadline = first_failed + timedelta(hours=24)  # RETRY_WINDOW，早于 AS_OF——已逾期
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(
            "INSERT INTO core.parse_retry_queue"
            " (source, provider_doc_id, first_failed_at, retry_deadline, last_seen_at, attempts)"
            " VALUES ('mock-announcements', 'doc-overdue', %s, %s, %s, 3)",
            (first_failed, deadline, first_failed),
        )
        conn.commit()

    oracle = RetryQueueEntry(
        provider_doc_id="doc-overdue",
        first_failed_at=first_failed,
        retry_deadline=deadline,
        last_seen_at=first_failed,
        attempts=3,
    ).is_overdue(AS_OF_DT)
    assert oracle is True

    r = client.get("/api/parse/retry-queue", params={"as_of": AS_OF})
    assert r.status_code == 200
    entry = next(e for e in r.json()["entries"] if e["provider_doc_id"] == "doc-overdue")
    assert entry["overdue"] == oracle


@pytest.mark.db
def test_retry_queue_not_yet_overdue(client: TestClient, api_db: tuple[str, str]) -> None:
    admin_dsn, _ = api_db
    first_failed = datetime(2025, 5, 31, 23, tzinfo=UTC)
    deadline = first_failed + timedelta(hours=24)  # 晚于 AS_OF——尚未逾期
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(
            "INSERT INTO core.parse_retry_queue"
            " (source, provider_doc_id, first_failed_at, retry_deadline, last_seen_at, attempts)"
            " VALUES ('mock-announcements', 'doc-fresh', %s, %s, %s, 1)",
            (first_failed, deadline, first_failed),
        )
        conn.commit()

    r = client.get("/api/parse/retry-queue", params={"as_of": AS_OF})
    assert r.status_code == 200
    entry = next(e for e in r.json()["entries"] if e["provider_doc_id"] == "doc-fresh")
    assert entry["overdue"] is False


@pytest.mark.db
def test_warnings_aggregate_counts_budget_exceeded(
    client: TestClient, api_db: tuple[str, str]
) -> None:
    admin_dsn, _ = api_db
    with psycopg.connect(admin_dsn) as conn:
        (doc_id,) = conn.execute(
            "INSERT INTO core.document"
            " (doc_type, title, publish_at, source, content_hash, version_group_id,"
            "  parse_warnings, valid_from, known_at, ingest_run_id)"
            " VALUES ('annual_report', '预算超支测试文档', '2025-05-01T00:00:00+08', 'mock',"
            "         'h-warn-test', 0, '[\"budget_exceeded\"]'::jsonb, '2025-05-01',"
            "         '2025-05-01T00:00:00+08', 'r1') RETURNING doc_id"
        ).fetchone()  # type: ignore[misc]
        conn.execute(
            "UPDATE core.document SET version_group_id = doc_id WHERE doc_id = %s", (doc_id,)
        )
        conn.commit()

    r = client.get("/api/parse/warnings", params={"as_of": AS_OF})
    assert r.status_code == 200
    agg = {a["warning"]: a["documents"] for a in r.json()["aggregate"]}
    assert agg.get("budget_exceeded") == 1

    r2 = client.get("/api/parse/warnings", params={"as_of": AS_OF, "warning": "budget_exceeded"})
    assert r2.status_code == 200
    assert any(d["doc_id"] == doc_id for d in r2.json()["documents"])


@pytest.mark.db
def test_budget_month_param_scopes_to_requested_month(
    client: TestClient, api_db: tuple[str, str]
) -> None:
    admin_dsn, _ = api_db
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(
            "INSERT INTO quality.quality_metric"
            " (metric, source_id, partition_date, value, passed, computed_at)"
            " VALUES ('c_tier_pages', 'quarterly', '2025-04-15', 10, true,"
            "         '2025-04-15T00:00:00+00')"
        )
        conn.commit()

    # AS_OF 是六月，但显式请求四月——预算数字必须按请求的月份而不是 as_of 所在月。
    r = client.get("/api/parse/budget", params={"as_of": AS_OF, "month": "2025-04"})
    assert r.status_code == 200
    bucket = next(b for b in r.json()["buckets"] if b["doc_type"] == "quarterly")
    assert bucket["pages_spent"] == 10
    assert r.json()["month_start"] == "2025-04-01"
    assert r.json()["month_end"] == "2025-05-01"


@pytest.mark.db
def test_budget_invalid_month_format_returns_422(client: TestClient) -> None:
    r = client.get("/api/parse/budget", params={"as_of": AS_OF, "month": "not-a-month"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_param"
