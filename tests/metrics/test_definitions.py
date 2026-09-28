"""口径版本化（总计划阶段 1 任务 1.2）。

改口径 = 追加新版本、旧版本保留。以早于新版本 known_at 的 as_of 查询，看到的必须是
旧口径——否则回测会用上当时还不存在的定义，这是一种不报错的前视偏差。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from ragdemo.metrics.definitions import (
    METRIC_BASELINE_KNOWN_AT,
    AiRevenueRuleSpec,
    MetricSpec,
    OutOfOrderVersion,
    PublishOutcome,
    SourceMapSpec,
    ai_revenue_rule_as_of,
    metric_as_of,
    publish_ai_revenue_rule,
    publish_metric,
    publish_source_map,
    source_maps_as_of,
)
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
CST = timezone(timedelta(hours=8))
T1 = datetime(2026, 9, 28, 10, 0, tzinfo=CST)  # 第二版口径写入的时刻
BEFORE_T1 = T1 - timedelta(days=1)
AFTER_T1 = T1 + timedelta(days=1)

REVENUE = MetricSpec(
    metric_id="revenue_total",
    metric_name="营业收入",
    metric_role="confirming",
    frequency="quarterly",
    source_type="filing",
    definition="合并报表口径营业收入",
    unit="CNY",
)


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer, l2_segment,"
        " l3_node, primary_node) VALUES ('CN.688041', '海光信息', 'listed', '算力', 'AI芯片',"
        " ARRAY['云端训练芯片'], '云端训练芯片')"
    )
    c.commit()
    yield c
    c.close()


def _baseline(conn: psycopg.Connection) -> None:
    publish_metric(
        conn, REVENUE, author="seed", known_at=METRIC_BASELINE_KNOWN_AT, ingest_run_id="t"
    )


# --- 指标定义 --------------------------------------------------------------------


@pytest.mark.db
def test_as_of_before_new_version_sees_old_definition(conn: psycopg.Connection) -> None:
    _baseline(conn)
    changed = MetricSpec(
        **{**REVENUE.__dict__, "definition": "合并报表口径营业收入（含其他业务收入）"}
    )
    result = publish_metric(
        conn,
        changed,
        author="founder",
        known_at=T1,
        change_note="口径扩到其他业务",
        ingest_run_id="t",
    )
    assert result.outcome is PublishOutcome.NEW_VERSION and result.version == 2

    old = metric_as_of(conn, "revenue_total", as_of=BEFORE_T1)
    new = metric_as_of(conn, "revenue_total", as_of=AFTER_T1)
    assert old is not None and new is not None
    assert (old.version, old.spec.definition) == (1, REVENUE.definition)
    assert (new.version, new.spec.definition) == (2, changed.definition)
    assert new.author == "founder" and new.change_note == "口径扩到其他业务"


@pytest.mark.db
def test_current_row_tracks_latest_version(conn: psycopg.Connection) -> None:
    _baseline(conn)
    changed = MetricSpec(**{**REVENUE.__dict__, "unit": "CNY_10K"})
    publish_metric(conn, changed, author="founder", known_at=T1, ingest_run_id="t")
    row = conn.execute(
        "SELECT unit FROM core.node_metric WHERE metric_id = 'revenue_total'"
    ).fetchone()
    assert row is not None and row[0] == "CNY_10K"


@pytest.mark.db
def test_baseline_version_is_visible_at_any_historical_as_of(conn: psycopg.Connection) -> None:
    _baseline(conn)
    found = metric_as_of(conn, "revenue_total", as_of=datetime(2015, 1, 1, tzinfo=CST))
    assert found is not None and found.version == 1


@pytest.mark.db
def test_republishing_identical_content_is_a_no_op(conn: psycopg.Connection) -> None:
    _baseline(conn)
    result = publish_metric(conn, REVENUE, author="founder", known_at=T1, ingest_run_id="t")
    assert result.outcome is PublishOutcome.UNCHANGED and result.version == 1
    count = conn.execute("SELECT count(*) FROM core.node_metric_version").fetchone()
    assert count is not None and count[0] == 1


@pytest.mark.db
def test_version_known_before_the_live_one_is_rejected(conn: psycopg.Connection) -> None:
    publish_metric(conn, REVENUE, author="seed", known_at=T1, ingest_run_id="t")
    changed = MetricSpec(**{**REVENUE.__dict__, "definition": "改"})
    with pytest.raises(OutOfOrderVersion):
        publish_metric(conn, changed, author="founder", known_at=BEFORE_T1, ingest_run_id="t")


@pytest.mark.db
def test_unknown_metric_as_of_returns_none(conn: psycopg.Connection) -> None:
    assert metric_as_of(conn, "no_such_metric", as_of=AFTER_T1) is None


def test_naive_known_at_is_rejected() -> None:
    with pytest.raises(ValueError, match="时区"):
        publish_metric(None, REVENUE, author="x", known_at=datetime(2026, 1, 1), ingest_run_id="t")  # type: ignore[arg-type]


# --- 供应商字段映射 --------------------------------------------------------------


@pytest.mark.db
def test_scale_factor_change_is_versioned(conn: psycopg.Connection) -> None:
    _baseline(conn)
    base = SourceMapSpec(metric_id="revenue_total", provider="tushare", provider_field="revenue")
    publish_source_map(
        conn, base, author="seed", known_at=METRIC_BASELINE_KNOWN_AT, ingest_run_id="t"
    )
    scaled = SourceMapSpec(**{**base.__dict__, "scale_factor": Decimal("10000")})
    result = publish_source_map(conn, scaled, author="founder", known_at=T1, ingest_run_id="t")
    assert result.outcome is PublishOutcome.NEW_VERSION

    (old,) = source_maps_as_of(conn, "revenue_total", as_of=BEFORE_T1)
    (new,) = source_maps_as_of(conn, "revenue_total", as_of=AFTER_T1)
    assert old.spec.scale_factor == Decimal(1) and new.spec.scale_factor == Decimal(10000)
    current = conn.execute(
        "SELECT scale_factor FROM core.metric_source_map"
        " WHERE metric_id = 'revenue_total' AND provider = 'tushare'"
    ).fetchone()
    assert current is not None and Decimal(current[0]) == Decimal(10000)


# --- AI 收入拆分规则 -------------------------------------------------------------


@pytest.mark.db
def test_ai_revenue_rule_has_one_live_rule_per_entity(conn: psycopg.Connection) -> None:
    first = AiRevenueRuleSpec(
        entity_id="CN.688041",
        rule_text="CPU 与 DCU 收入全部计入 AI 相关收入",
        source_type="segment_report",
        confidence="medium",
        effective_from=date(2024, 1, 1),
    )
    publish_ai_revenue_rule(conn, first, author="founder", known_at=BEFORE_T1, ingest_run_id="t")
    second = AiRevenueRuleSpec(**{**first.__dict__, "rule_text": "仅 DCU 收入计入 AI 相关收入"})
    publish_ai_revenue_rule(conn, second, author="founder", known_at=T1, ingest_run_id="t")

    before = ai_revenue_rule_as_of(conn, "CN.688041", as_of=BEFORE_T1 + timedelta(hours=1))
    after = ai_revenue_rule_as_of(conn, "CN.688041", as_of=AFTER_T1)
    assert before is not None and after is not None
    assert before.spec.rule_text == first.rule_text and after.spec.rule_text == second.rule_text
    live = conn.execute(
        "SELECT count(*) FROM core.ai_revenue_rule WHERE superseded_at IS NULL"
    ).fetchone()
    assert live is not None and live[0] == 1


# --- 迁移 018 对存量行的回填 -----------------------------------------------------


@pytest.mark.db
def test_migration_backfills_existing_rows_as_baseline_version(
    temp_db: str, tmp_path: Path
) -> None:
    """已有库（018 之前就有 node_metric / metric_source_map 行）迁移后，每行都有
    一个在任何 as_of 下可见的版本 1，基线时刻与代码常量一致。"""
    before_018 = tmp_path / "migrations"
    before_018.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name < "018":
            shutil.copy(path, before_018 / path.name)

    with psycopg.connect(temp_db) as c:
        migrate(c, before_018)
        c.execute(
            "INSERT INTO core.node_metric (metric_id, metric_name, metric_role, frequency,"
            " source_type, definition, unit) VALUES"
            " ('revenue_total', '营业收入', 'confirming', 'quarterly', 'filing', '口径', 'CNY')"
        )
        c.execute(
            "INSERT INTO core.metric_source_map (metric_id, provider, provider_field)"
            " VALUES ('revenue_total', 'tushare', 'revenue')"
        )
        c.commit()
        migrate(c, MIGRATIONS)

        found = metric_as_of(c, "revenue_total", as_of=datetime(2010, 1, 1, tzinfo=CST))
        assert (
            found is not None and found.version == 1 and found.known_at == METRIC_BASELINE_KNOWN_AT
        )
        (mapping,) = source_maps_as_of(c, "revenue_total", as_of=datetime(2010, 1, 1, tzinfo=CST))
        assert mapping.version == 1
