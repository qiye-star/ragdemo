"""告警出口：渠道格式、冷却、渠道故障不吞告警、落库（阶段 4.3）。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from ragdemo.ops.alerts import Alert, raise_alert, webhook_payload
from ragdemo.ops.quality_alerts import evaluate_quality_alerts
from ragdemo_core.db.migrate import migrate

NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
ALERT = Alert("run_failed", "critical", "run:abc", "行情分区失败", "AllLayersFailed: 688041.SH")


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, Path("db/migrations"))
        yield c


class _Recorder:
    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._status = status
        self._body = body if body is not None else {"errcode": 0}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            return httpx.Response(self._status, json=self._body)

        return httpx.MockTransport(handler)


def test_payload_shapes_for_each_channel_kind() -> None:
    assert webhook_payload("wecom", ALERT)["msgtype"] == "text"
    assert "行情分区失败" in webhook_payload("dingtalk", ALERT)["text"]["content"]
    assert webhook_payload("feishu", ALERT)["msg_type"] == "text"
    generic = webhook_payload("generic", ALERT)
    assert generic["key"] == "run:abc" and generic["severity"] == "critical"
    with pytest.raises(ValueError):
        webhook_payload("email", ALERT)


@pytest.mark.db
def test_alert_is_logged_stored_and_posted_to_the_webhook(conn: psycopg.Connection) -> None:
    rec = _Recorder()
    out = raise_alert(
        conn,
        ALERT,
        env={"ALERT_WEBHOOK_URL": "https://hook.invalid/x", "ALERT_WEBHOOK_KIND": "wecom"},
        now=NOW,
        transport=rec.transport(),
    )
    assert out == {"log": "ok", "webhook": "ok", "db": "ok"}
    assert len(rec.requests) == 1 and "行情分区失败" in rec.requests[0]["text"]["content"]
    row = conn.execute(
        "SELECT kind, severity, alert_key, suppressed FROM audit.alert_log"
    ).fetchone()
    assert row == ("run_failed", "critical", "run:abc", False)


@pytest.mark.db
def test_same_key_within_the_cooldown_is_recorded_but_not_pushed_again(
    conn: psycopg.Connection,
) -> None:
    rec = _Recorder()
    env = {"ALERT_WEBHOOK_URL": "https://hook.invalid/x", "ALERT_WEBHOOK_KIND": "generic"}
    raise_alert(conn, ALERT, env=env, now=NOW, transport=rec.transport())
    second = raise_alert(
        conn, ALERT, env=env, now=NOW + timedelta(hours=1), transport=rec.transport()
    )
    assert second["webhook"] == "skipped: 冷却中" and len(rec.requests) == 1
    later = raise_alert(
        conn, ALERT, env=env, now=NOW + timedelta(hours=7), transport=rec.transport()
    )
    assert later["webhook"] == "ok" and len(rec.requests) == 2  # 冷却过后再次推送
    n = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE suppressed) FROM audit.alert_log"
    ).fetchone()
    assert n == (3, 1)


@pytest.mark.db
def test_without_a_webhook_the_alert_still_goes_to_log_and_db(conn: psycopg.Connection) -> None:
    out = raise_alert(conn, ALERT, env={}, now=NOW)
    assert out["webhook"].startswith("skipped: 未配置") and out["db"] == "ok"


@pytest.mark.db
def test_a_broken_channel_is_recorded_and_never_raises(conn: psycopg.Connection) -> None:
    env = {"ALERT_WEBHOOK_URL": "https://hook.invalid/x", "ALERT_WEBHOOK_KIND": "generic"}
    http_500 = raise_alert(conn, ALERT, env=env, now=NOW, transport=_Recorder(500).transport())
    assert http_500["webhook"] == "error: HTTP 500" and http_500["db"] == "ok"

    biz = raise_alert(
        conn,
        Alert("run_failed", "warning", "k2", "t"),
        env=env,
        now=NOW,
        transport=_Recorder(200, {"errcode": 93000, "errmsg": "invalid webhook url"}).transport(),
    )
    assert biz["webhook"].startswith("error: 渠道返回 93000")  # 200 但业务失败也要识别

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    down = raise_alert(
        conn,
        Alert("run_failed", "warning", "k3", "t"),
        env=env,
        now=NOW,
        transport=httpx.MockTransport(boom),
    )
    assert down["webhook"] == "error: ConnectError"


def test_without_a_database_connection_the_alert_is_still_logged() -> None:
    out = raise_alert(None, ALERT, env={}, now=NOW)
    assert out["log"] == "ok" and "db" not in out


# --- quality alerts ---------------------------------------------------------------------


def _metric(
    conn: psycopg.Connection, metric: str, passed: bool, day: date, source: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO quality.quality_metric (metric, source_id, partition_date, value, threshold,"
        " passed) VALUES (%s, %s, %s, 0.5, 0.9, %s)",
        (metric, source, day, passed),
    )
    conn.commit()


@pytest.mark.db
def test_failed_metrics_become_alerts_with_reconcile_marked_critical(
    conn: psycopg.Connection,
) -> None:
    day = date(2026, 9, 29)
    _metric(conn, "leaf_length_compliance_rate", False, day)
    _metric(conn, "ingest_reconciliation_rate", False, day, "cninfo")
    _metric(conn, "parse_success_rate", True, day)
    _metric(conn, "orphan_block_rate", False, day - timedelta(days=1))  # 别的分区日：不算
    alerts = evaluate_quality_alerts(conn, day, cap_cny=Decimal(5000), now=NOW)
    by_kind = {a.kind: a for a in alerts}
    assert set(by_kind) == {"quality", "reconcile"}
    assert by_kind["reconcile"].severity == "critical" and "cninfo" in by_kind["reconcile"].title
    assert by_kind["quality"].severity == "warning"
    assert by_kind["quality"].key.endswith("2026-09-29")


@pytest.mark.db
def test_budget_levels_map_to_alerts(conn: psycopg.Connection) -> None:
    day = date(2026, 9, 29)

    def spend(cents: int) -> None:
        conn.execute(
            "INSERT INTO core.provider_snapshot (provider, endpoint, params, response,"
            " ingest_run_id, fetched_at, cost_cents) VALUES ('textin', 'x', '{}', '{}'::jsonb, 't', %s, %s)",
            (NOW - timedelta(hours=1), cents),
        )
        conn.commit()

    assert evaluate_quality_alerts(conn, day, cap_cny=Decimal(100), now=NOW) == []
    spend(8500)  # 85 元 / 100 元
    (warn,) = evaluate_quality_alerts(conn, day, cap_cny=Decimal(100), now=NOW)
    assert warn.kind == "budget" and warn.severity == "warning"
    spend(2000)  # 105 元
    (exceeded,) = evaluate_quality_alerts(conn, day, cap_cny=Decimal(100), now=NOW)
    assert exceeded.severity == "critical" and "已用尽" in exceeded.title
