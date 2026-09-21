"""Dagster Definitions 与传感器：job 目标存在、writer 资源惰性构造。

对应两条 review 意见：
1. `new_document_sensor` 返回 RunRequest 时必须有 job/jobs/asset_selection 目标，
   否则 `SensorDefinition.evaluate_tick` 会在真的命中文档的那一刻抛异常。
2. `writer` 资源不能在 `Definitions(...)` 调用（即模块 import）时就连库，
   必须等 Dagster 初始化该资源时才连接。
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from dagster import DagsterInstance, build_sensor_context

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


def test_new_document_sensor_has_a_job_target() -> None:
    """回归 Finding 1：没有 job 目标时 `job_name` 会抛
    `DagsterInvalidDefinitionError`，而不是返回 None——所以用是否抛异常
    （而非 is None）来断言目标已声明。"""
    from ragdemo.ingest.definitions import _document_reaction_job, defs, new_document_sensor

    assert new_document_sensor.has_jobs
    assert new_document_sensor.job_name == "document_reaction_placeholder"
    assert new_document_sensor.job is _document_reaction_job
    assert defs.jobs is not None
    assert _document_reaction_job in defs.jobs


def test_definitions_module_imports_without_dsn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归 Finding 2：`writer` 资源必须惰性构造，模块 import 不该连库，
    因而也不该要求 RAGDEMO_DSN 已设置。"""
    import importlib

    monkeypatch.delenv("RAGDEMO_DSN", raising=False)
    import ragdemo.ingest.definitions as definitions_module

    importlib.reload(definitions_module)
    assert definitions_module.defs is not None


@pytest.mark.db
def test_sensor_evaluate_tick_returns_run_request_for_real_document(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端版本：对真实 `core.document` 行驱动 evaluate_tick，
    证明补上 job 目标后命中文档不再崩溃，而是正常产出 RunRequest。"""
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.document (doc_type, title, publish_at, source, content_hash,"
        " version_group_id, valid_from, known_at, ingest_run_id) VALUES"
        " ('announcement','测试公告','2024-10-28T09:00:00+08:00','mock','hash-1',"
        " 1, '2024-10-28', '2024-10-28T09:00:00+08:00', 'test')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("RAGDEMO_DSN", temp_db)
    from ragdemo.ingest.definitions import new_document_sensor

    ctx = build_sensor_context(instance=DagsterInstance.ephemeral())
    result = new_document_sensor.evaluate_tick(ctx)

    assert result.run_requests is not None
    assert len(result.run_requests) == 1
    assert result.run_requests[0].run_key == "docs-1"
