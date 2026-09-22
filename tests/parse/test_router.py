"""C 档路由：触发规则、月度预算、重试队列 24 小时超时标记。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from ragdemo.parse.router import (
    check_monthly_budget,
    list_retry_queue,
    load_active_policy,
    monthly_pages_spent,
    record_c_tier_pages,
    record_retry_pending,
    resolve_retry_pending,
    should_route_to_tier_c,
)
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
# 015 迁移自带的占位默认策略 known_at=now()（迁移执行时刻，即测试运行时刻），
# 不是一个固定的日历日期——AS_OF 必须晚于"测试运行时刻"才能看到那条策略,
# 不能写死成任意一个过去的日期（那条策略的 known_at 会比它更晚，判定为
# "还没生效"）。
AS_OF = datetime.now(UTC) + timedelta(days=1)


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.commit()
    return c


def _seed_policy(
    conn: psycopg.Connection,
    *,
    doc_type: str | None,
    confidence_below: float | None = 0.7,
    closure_below: float | None = 0.9,
    monthly_cap_cny: float = 500,
    enabled: bool = True,
    known_at: datetime = datetime(2025, 1, 1, tzinfo=UTC),
) -> None:
    conn.execute(
        "INSERT INTO core.parse_tier_policy"
        " (doc_type, confidence_below, closure_below, monthly_cap_cny, enabled, known_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (doc_type, confidence_below, closure_below, monthly_cap_cny, enabled, known_at),
    )
    conn.commit()


# --- load_active_policy -------------------------------------------------------


@pytest.mark.db
def test_seed_policy_is_loaded_for_any_doc_type(conn: psycopg.Connection) -> None:
    """015 迁移自带的占位默认策略（doc_type 通配）必须真的能被查到。"""
    policy = load_active_policy(conn, "quarterly", AS_OF)

    assert policy is not None
    assert policy.confidence_below == 0.7
    assert policy.closure_below == 0.9


@pytest.mark.db
def test_exact_doc_type_match_wins_over_wildcard(conn: psycopg.Connection) -> None:
    _seed_policy(conn, doc_type="annual_report", confidence_below=0.5)

    policy = load_active_policy(conn, "annual_report", AS_OF)

    assert policy is not None
    assert policy.doc_type == "annual_report"
    assert policy.confidence_below == 0.5


@pytest.mark.db
def test_superseded_policy_is_not_returned(conn: psycopg.Connection) -> None:
    old = datetime(2025, 1, 1, tzinfo=UTC)
    conn.execute(
        "INSERT INTO core.parse_tier_policy"
        " (doc_type, confidence_below, monthly_cap_cny, known_at, superseded_at) "
        "VALUES ('annual_report', 0.99, 500, %s, %s)",
        (old, datetime(2025, 6, 1, tzinfo=UTC)),
    )
    conn.commit()

    policy = load_active_policy(conn, "annual_report", AS_OF)

    # 只有 015 迁移自带的通配策略活着；专属策略已经在 as_of 之前被取代。
    assert policy is not None
    assert policy.doc_type is None


@pytest.mark.db
def test_no_matching_policy_returns_none(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM core.parse_tier_policy")
    conn.commit()

    assert load_active_policy(conn, "quarterly", AS_OF) is None


# --- should_route_to_tier_c ---------------------------------------------------


@pytest.mark.db
def test_low_confidence_triggers_tier_c(conn: psycopg.Connection) -> None:
    """G2：parse_confidence=0.5 自动进 C 档。"""
    policy = load_active_policy(conn, "quarterly", AS_OF)

    assert should_route_to_tier_c(
        policy, parse_confidence=0.5, table_closure_rate=None, has_numeric_table=False
    )


@pytest.mark.db
def test_high_confidence_does_not_trigger(conn: psycopg.Connection) -> None:
    policy = load_active_policy(conn, "quarterly", AS_OF)

    assert not should_route_to_tier_c(
        policy, parse_confidence=0.95, table_closure_rate=1.0, has_numeric_table=True
    )


@pytest.mark.db
def test_low_table_closure_with_numeric_table_triggers(conn: psycopg.Connection) -> None:
    policy = load_active_policy(conn, "quarterly", AS_OF)

    assert should_route_to_tier_c(
        policy, parse_confidence=0.95, table_closure_rate=0.5, has_numeric_table=True
    )


@pytest.mark.db
def test_low_table_closure_without_numeric_table_does_not_trigger(
    conn: psycopg.Connection,
) -> None:
    """表格闭合率低但块内不含数值——触发条件是"块内含数值且表格闭合率低"，
    两者都要满足。"""
    policy = load_active_policy(conn, "quarterly", AS_OF)

    assert not should_route_to_tier_c(
        policy, parse_confidence=0.95, table_closure_rate=0.5, has_numeric_table=False
    )


def test_disabled_policy_never_triggers() -> None:
    from ragdemo.parse.router import ParseTierPolicy

    disabled = ParseTierPolicy(
        policy_id=1,
        doc_type=None,
        confidence_below=0.99,
        closure_below=0.99,
        monthly_cap_cny=Decimal(500),
        enabled=False,
    )

    assert not should_route_to_tier_c(
        disabled, parse_confidence=0.1, table_closure_rate=0.1, has_numeric_table=True
    )


def test_no_policy_never_triggers() -> None:
    assert not should_route_to_tier_c(
        None, parse_confidence=0.1, table_closure_rate=0.1, has_numeric_table=True
    )


# --- 月度预算 ------------------------------------------------------------------


@pytest.mark.db
def test_monthly_pages_spent_sums_within_the_window(conn: psycopg.Connection) -> None:
    record_c_tier_pages(
        conn, doc_type="quarterly", pages=10, at=datetime(2026, 1, 5, tzinfo=UTC)
    )
    record_c_tier_pages(
        conn, doc_type="quarterly", pages=20, at=datetime(2026, 1, 20, tzinfo=UTC)
    )
    record_c_tier_pages(
        conn, doc_type="quarterly", pages=99, at=datetime(2026, 2, 1, tzinfo=UTC)
    )  # 下个月，不该被计入

    spent = monthly_pages_spent(
        conn,
        doc_type="quarterly",
        month_start=datetime(2026, 1, 1, tzinfo=UTC),
        month_end=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert spent == 30


@pytest.mark.db
def test_budget_allows_spend_within_cap(conn: psycopg.Connection) -> None:
    policy = load_active_policy(conn, "quarterly", AS_OF)
    assert policy is not None

    decision = check_monthly_budget(
        conn,
        policy,
        doc_type="quarterly",
        month_start=datetime(2026, 1, 1, tzinfo=UTC),
        month_end=datetime(2026, 2, 1, tzinfo=UTC),
        pages_about_to_spend=10,
        cost_per_page_cny=Decimal("0.5"),
    )

    assert decision.allowed is True


@pytest.mark.db
def test_budget_rejects_spend_once_cap_is_exhausted(conn: psycopg.Connection) -> None:
    """G3：把月度上限调到 0，再喂一份该进 C 档的文档——预算判定必须拒绝。"""
    _seed_policy(
        conn, doc_type="edge-case", monthly_cap_cny=0, known_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    policy = load_active_policy(conn, "edge-case", AS_OF)
    assert policy is not None

    decision = check_monthly_budget(
        conn,
        policy,
        doc_type="edge-case",
        month_start=datetime(2026, 1, 1, tzinfo=UTC),
        month_end=datetime(2026, 2, 1, tzinfo=UTC),
        pages_about_to_spend=1,
        cost_per_page_cny=Decimal("0.5"),
    )

    assert decision.allowed is False
    assert decision.cap_cny == 0


# --- 重试队列（G6）-------------------------------------------------------------


@pytest.mark.db
def test_first_failure_creates_a_queue_entry_with_a_24h_deadline(
    conn: psycopg.Connection,
) -> None:
    at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    record_retry_pending(conn, source="mock-announcements", provider_doc_ids=["doc-a"], at=at)

    (entry,) = list_retry_queue(conn, source="mock-announcements")
    assert entry.provider_doc_id == "doc-a"
    assert entry.first_failed_at == at
    assert entry.retry_deadline == at + timedelta(hours=24)
    assert entry.attempts == 1


@pytest.mark.db
def test_repeated_failure_advances_last_seen_but_not_the_deadline(
    conn: psycopg.Connection,
) -> None:
    first = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    second = datetime(2026, 1, 1, 16, 0, tzinfo=UTC)
    record_retry_pending(conn, source="mock-announcements", provider_doc_ids=["doc-a"], at=first)
    record_retry_pending(conn, source="mock-announcements", provider_doc_ids=["doc-a"], at=second)

    (entry,) = list_retry_queue(conn, source="mock-announcements")
    assert entry.first_failed_at == first
    assert entry.retry_deadline == first + timedelta(hours=24)
    assert entry.last_seen_at == second
    assert entry.attempts == 2


@pytest.mark.db
def test_resolved_document_is_removed_from_the_queue(conn: psycopg.Connection) -> None:
    at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    record_retry_pending(conn, source="mock-announcements", provider_doc_ids=["doc-a"], at=at)

    resolve_retry_pending(conn, source="mock-announcements", provider_doc_ids=["doc-a"])

    assert list_retry_queue(conn, source="mock-announcements") == []


@pytest.mark.db
def test_is_overdue_reflects_the_fixed_deadline(conn: psycopg.Connection) -> None:
    at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    record_retry_pending(conn, source="mock-announcements", provider_doc_ids=["doc-a"], at=at)
    (entry,) = list_retry_queue(conn, source="mock-announcements")

    assert entry.is_overdue(at + timedelta(hours=23)) is False
    assert entry.is_overdue(at + timedelta(hours=25)) is True
