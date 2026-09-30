"""日度调度、运维作业与失败告警传感器（阶段 4.2 / 4.3）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from dagster import (
    DagsterInstance,
    DefaultScheduleStatus,
    ScheduleDefinition,
    build_schedule_context,
)

from ragdemo.ingest.definitions import defs
from ragdemo.ingest.schedules import (
    ALL_SCHEDULES,
    leak_replay_job,
    prices_schedule,
    quality_alerts_job,
    stage2_verify_job,
)
from ragdemo_core.db.migrate import migrate

EXPECTED = {
    "prices_schedule": ("0 17 * * 1-5", DefaultScheduleStatus.RUNNING),
    "announcements_schedule": ("30 20 * * *", DefaultScheduleStatus.RUNNING),
    "stage2_verify_schedule": ("30 21 * * *", DefaultScheduleStatus.RUNNING),
    "leak_replay_schedule": ("30 3 * * *", DefaultScheduleStatus.RUNNING),
    "quality_alerts_schedule": ("0 22 * * *", DefaultScheduleStatus.RUNNING),
    "announcement_reconcile_schedule": ("0 23 * * 0", DefaultScheduleStatus.RUNNING),
    "wind_facts_schedule": ("0 21 * * 1", DefaultScheduleStatus.STOPPED),
}


def test_every_schedule_is_registered_with_the_agreed_cron_and_timezone() -> None:
    assert defs.schedules is not None
    by_name = {s.name: s for s in defs.schedules}
    assert set(EXPECTED) <= set(by_name)
    for name, (cron, status) in EXPECTED.items():
        s = by_name[name]
        assert isinstance(s, ScheduleDefinition)
        assert s.cron_schedule == cron and s.execution_timezone == "Asia/Shanghai", name
        assert s.default_status == status, name
    assert len(ALL_SCHEDULES) == len(EXPECTED)


def test_the_wind_credit_consuming_job_is_off_by_default() -> None:
    """耗额度的调度不能默认开着——需要人工在 Dagster 里打开。"""
    assert EXPECTED["wind_facts_schedule"][1] is DefaultScheduleStatus.STOPPED
    assert defs.get_job_def("wind_facts_job") is not None


def test_prices_schedule_requests_todays_partition_in_beijing_time() -> None:
    context = build_schedule_context(
        scheduled_execution_time=datetime(2026, 9, 30, 9, 0, tzinfo=UTC),
        repository_def=defs.get_repository_def(),
    )
    result = prices_schedule.evaluate_tick(context)
    assert result.run_requests
    key = result.run_requests[0].partition_key
    today_cst = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    assert key == today_cst


def test_failure_alert_sensor_and_document_sensor_are_registered() -> None:
    assert defs.sensors is not None
    assert {"alert_on_run_failure", "new_document_sensor"} <= {s.name for s in defs.sensors}


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, Path("db/migrations"))
        yield c


@pytest.mark.db
def test_ops_run_on_an_empty_database_and_alert_only_when_there_is_something_to_say(
    conn: psycopg.Connection,
) -> None:
    instance = DagsterInstance.ephemeral()
    for job in (stage2_verify_job, leak_replay_job, quality_alerts_job):
        result = job.execute_in_process(resources={"conn": conn}, instance=instance)
        assert result.success, job.name
    assert conn.execute("SELECT count(*) FROM audit.alert_log").fetchone() == (0,)


@pytest.mark.db
def test_quality_alerts_job_raises_and_records_an_alert_for_a_failed_metric(
    conn: psycopg.Connection,
) -> None:
    today = datetime.now(timezone(timedelta(hours=8))).date()
    conn.execute(
        "INSERT INTO quality.quality_metric (metric, partition_date, value, threshold, passed)"
        " VALUES ('ingest_reconciliation_rate', %s, 0.5, 0.99, false)",
        (today,),
    )
    conn.commit()
    result = quality_alerts_job.execute_in_process(
        resources={"conn": conn}, instance=DagsterInstance.ephemeral()
    )
    assert result.success
    row = conn.execute("SELECT kind, severity FROM audit.alert_log").fetchone()
    assert row == ("reconcile", "critical")
    assert isinstance(today, date)
