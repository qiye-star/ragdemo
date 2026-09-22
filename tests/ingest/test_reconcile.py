"""对账记录与增量游标：差异必须有归因，游标可 upsert。"""

from __future__ import annotations

from datetime import UTC, date, timedelta
from datetime import datetime as dt
from pathlib import Path

import psycopg
import pytest

from ragdemo.ingest.reconcile import (
    RECONCILE_METRIC,
    compute_ingest_latency,
    read_watermark,
    reconcile_counts,
    write_watermark,
)
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
PARTITION = date(2024, 10, 28)
SINCE = dt(2024, 10, 27, 16, 0, tzinfo=UTC)
UNTIL = dt(2024, 10, 28, 16, 0, tzinfo=UTC)


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.commit()
    return c


@pytest.mark.db
def test_matching_counts_pass_with_no_note_required(conn: psycopg.Connection) -> None:
    result = reconcile_counts(conn, PARTITION, "tushare", expected=10, actual=10)

    assert result.passed is True
    assert result.value == 0.0
    assert result.note is None


@pytest.mark.db
def test_mismatch_without_a_note_fails(conn: psycopg.Connection) -> None:
    """F1：diff != 0 且没有归因，判定失败——不能悄悄放行。"""
    result = reconcile_counts(conn, PARTITION, "tushare", expected=10, actual=7)

    assert result.passed is False
    assert result.value == -3.0


@pytest.mark.db
def test_mismatch_with_a_note_passes(conn: psycopg.Connection) -> None:
    """有归因的差异是被允许的状态，不是被放宽的标准。"""
    result = reconcile_counts(
        conn, PARTITION, "mock-announcements", expected=10, actual=8, note="budget_exhausted"
    )

    assert result.passed is True
    assert result.note == "budget_exhausted"


@pytest.mark.db
def test_result_is_persisted_to_quality_metric(conn: psycopg.Connection) -> None:
    reconcile_counts(conn, PARTITION, "tushare", expected=5, actual=5)

    metric, value, passed = conn.execute(
        "SELECT metric, value, passed FROM quality.quality_metric WHERE metric = %s",
        (RECONCILE_METRIC,),
    ).fetchone()  # type: ignore[misc]
    assert metric == RECONCILE_METRIC
    assert value == 0.0
    assert passed is True


@pytest.mark.db
def test_watermark_round_trips(conn: psycopg.Connection) -> None:
    assert read_watermark(conn, "tushare", PARTITION) is None

    write_watermark(conn, "tushare", PARTITION, "20241028")

    assert read_watermark(conn, "tushare", PARTITION) == "20241028"


@pytest.mark.db
def test_watermark_rewrite_updates_the_same_row_not_a_new_one(conn: psycopg.Connection) -> None:
    write_watermark(conn, "tushare", PARTITION, "20241028")
    write_watermark(conn, "tushare", PARTITION, "20241029")

    assert read_watermark(conn, "tushare", PARTITION) == "20241029"
    (count,) = conn.execute(
        "SELECT count(*) FROM core.ingest_watermark WHERE source_id = %s AND partition_date = %s",
        ("tushare", PARTITION),
    ).fetchone()  # type: ignore[misc]
    assert count == 1


@pytest.mark.db
def test_watermark_is_scoped_per_source(conn: psycopg.Connection) -> None:
    write_watermark(conn, "tushare", PARTITION, "cursor-a")
    write_watermark(conn, "edgar", PARTITION, "cursor-b")

    assert read_watermark(conn, "tushare", PARTITION) == "cursor-a"
    assert read_watermark(conn, "edgar", PARTITION) == "cursor-b"


@pytest.mark.db
def test_watermark_read_finds_the_latest_row_at_or_before_the_partition(
    conn: psycopg.Connection,
) -> None:
    """管线暂停几天后恢复：今天这个分区还没写过游标行，读的应该是最近一次
    成功运行（更早的 partition_date）留下的游标，而不是 None——否则每次
    从暂停中恢复都会被当成"第一次跑"，白白重新全量拉一遍。"""
    earlier = date(2024, 10, 20)
    write_watermark(conn, "tushare", earlier, "20241020")

    assert read_watermark(conn, "tushare", PARTITION) == "20241020"


@pytest.mark.db
def test_watermark_read_ignores_rows_after_the_given_partition(conn: psycopg.Connection) -> None:
    """不能"偷看"未来分区写下的游标——否则回填历史分区时会被今天的游标
    误判成"这段历史已经处理过"，从而跳过本该补的数据。"""
    later = date(2024, 11, 1)
    write_watermark(conn, "tushare", later, "20241101")

    assert read_watermark(conn, "tushare", PARTITION) is None


# --- compute_ingest_latency（F6：接入延迟，按源分别设阈值）------------------


def _seed_document(
    conn: psycopg.Connection, *, doc_id: int, source: str, publish_at: dt, ingested_at: dt
) -> None:
    conn.execute(
        "INSERT INTO core.document (doc_id, doc_type, title, publish_at, source,"
        " content_hash, version_group_id, valid_from, known_at, ingest_run_id, ingested_at) "
        "OVERRIDING SYSTEM VALUE VALUES (%s,'announcement','t',%s,%s,%s,%s,%s,%s,'r1',%s)",
        (
            doc_id,
            publish_at,
            source,
            f"h{doc_id}",
            doc_id,
            publish_at.date(),
            publish_at,
            ingested_at,
        ),
    )
    conn.commit()


@pytest.mark.db
def test_latency_passes_within_threshold(conn: psycopg.Connection) -> None:
    publish_at = dt(2024, 10, 28, 10, 0, tzinfo=UTC)
    _seed_document(
        conn, doc_id=1, source="mock-announcements", publish_at=publish_at,
        ingested_at=publish_at + timedelta(minutes=5),
    )

    result = compute_ingest_latency(
        conn, PARTITION, "mock-announcements", SINCE, UNTIL, p95_threshold_minutes=15.0
    )

    assert result.passed is True
    assert result.value <= 15.0


@pytest.mark.db
def test_latency_fails_when_p95_exceeds_the_sources_threshold(conn: psycopg.Connection) -> None:
    publish_at = dt(2024, 10, 28, 10, 0, tzinfo=UTC)
    _seed_document(
        conn, doc_id=1, source="mock-announcements", publish_at=publish_at,
        ingested_at=publish_at + timedelta(minutes=30),
    )

    result = compute_ingest_latency(
        conn, PARTITION, "mock-announcements", SINCE, UNTIL, p95_threshold_minutes=15.0
    )

    assert result.passed is False


@pytest.mark.db
def test_latency_with_no_threshold_always_passes_but_still_records(
    conn: psycopg.Connection,
) -> None:
    """EDGAR 这类日频源：记录延迟但不设阻断阈值——一刀切会让它永远红着。"""
    publish_at = dt(2024, 10, 28, 10, 0, tzinfo=UTC)
    _seed_document(
        conn, doc_id=1, source="edgar", publish_at=publish_at,
        ingested_at=publish_at + timedelta(hours=20),
    )

    result = compute_ingest_latency(conn, PARTITION, "edgar", SINCE, UNTIL)

    assert result.passed is True
    assert result.threshold is None
    assert result.value > 15.0  # 数字如实记录，只是没有阈值判定失败


@pytest.mark.db
def test_latency_with_no_documents_in_window_vacuously_passes(conn: psycopg.Connection) -> None:
    result = compute_ingest_latency(
        conn, PARTITION, "mock-announcements", SINCE, UNTIL, p95_threshold_minutes=15.0
    )

    assert result.passed is True
    assert result.value == -1.0
