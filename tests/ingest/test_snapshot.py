"""原始响应留存：永不修改、永不删除，且不含密钥。"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.base import RawResponse
from ragdemo.ingest.snapshot import MAX_INLINE_PAYLOAD_BYTES, save_snapshot
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.commit()
    return c


def _raw(payload: object, params: dict[str, object] | None = None) -> RawResponse:
    return RawResponse(
        provider="tushare", endpoint="/income", params=params or {"token": "sk-secret"},
        payload=payload, http_status=200, fetched_at=datetime.now(UTC),
    )


@pytest.mark.db
def test_snapshot_stores_payload_and_redacts_params(conn: psycopg.Connection) -> None:
    sid = save_snapshot(conn, _raw([{"a": 1}]), ingest_run_id="r1")
    params, response = conn.execute(
        "SELECT params, response FROM core.provider_snapshot WHERE snapshot_id = %s", (sid,)
    ).fetchone()  # type: ignore[misc]
    assert params["token"] == "[REDACTED]"
    assert response == [{"a": 1}]


@pytest.mark.db
def test_oversized_payload_goes_to_response_ref(conn: psycopg.Connection) -> None:
    """超过 1MB 的响应不进 jsonb 列，避免单表膨胀。"""
    big = [{"x": "y" * 1000} for _ in range(2000)]
    sid = save_snapshot(conn, _raw(big), ingest_run_id="r1")
    response, ref = conn.execute(
        "SELECT response, response_ref FROM core.provider_snapshot WHERE snapshot_id = %s",
        (sid,),
    ).fetchone()  # type: ignore[misc]
    assert response is None
    assert ref is not None and str(MAX_INLINE_PAYLOAD_BYTES) not in ref


@pytest.mark.db
def test_fetched_at_is_the_provider_fetch_moment_not_insert_time(
    conn: psycopg.Connection,
) -> None:
    """fetched_at 必须来自 RawResponse，不能悄悄退化成入库时刻（schema DEFAULT now()）。"""
    moment = datetime(2020, 1, 1, tzinfo=UTC)
    raw = RawResponse(
        provider="tushare", endpoint="/income", params={"token": "sk-secret"},
        payload=[{"a": 1}], http_status=200, fetched_at=moment,
    )
    sid = save_snapshot(conn, raw, ingest_run_id="r1")
    (fetched_at,) = conn.execute(
        "SELECT fetched_at FROM core.provider_snapshot WHERE snapshot_id = %s", (sid,)
    ).fetchone()  # type: ignore[misc]
    assert fetched_at == moment


@pytest.mark.db
def test_oversized_payload_is_actually_uploaded_when_blob_is_given(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """传了 blob 时，response_ref 必须真的指向可读回的内容，而不是空占位 key。"""
    blob = LocalBlobStore(tmp_path)
    big = [{"x": "y" * 1000} for _ in range(2000)]
    sid = save_snapshot(conn, _raw(big), ingest_run_id="r1", blob=blob)
    (ref,) = conn.execute(
        "SELECT response_ref FROM core.provider_snapshot WHERE snapshot_id = %s", (sid,)
    ).fetchone()  # type: ignore[misc]
    assert ref is not None
    assert blob.exists(ref)
    assert json.loads(blob.get(ref)) == big


@pytest.mark.db
def test_oversized_payload_without_blob_keeps_prior_placeholder_only_behavior(
    conn: psycopg.Connection,
) -> None:
    """不传 blob（默认）时行为必须和修复前完全一致：只占位，不落盘。"""
    big = [{"x": "y" * 1000} for _ in range(2000)]
    sid = save_snapshot(conn, _raw(big), ingest_run_id="r1")
    (ref,) = conn.execute(
        "SELECT response_ref FROM core.provider_snapshot WHERE snapshot_id = %s", (sid,)
    ).fetchone()  # type: ignore[misc]
    assert ref is not None


@pytest.mark.db
def test_non_json_native_types_do_not_raise_at_insert(conn: psycopg.Connection) -> None:
    """size 检查用 default=str 容忍 Decimal/date；插入必须用同一份编码，否则
    psycopg 自己的 JSON 编码器会在这里报 TypeError（size 检查通过≠能插入）。"""
    payload = {"amount": Decimal("1.5"), "as_of": date(2024, 1, 1)}
    sid = save_snapshot(conn, _raw(payload), ingest_run_id="r1")
    (response,) = conn.execute(
        "SELECT response FROM core.provider_snapshot WHERE snapshot_id = %s", (sid,)
    ).fetchone()  # type: ignore[misc]
    assert response == {"amount": "1.5", "as_of": "2024-01-01"}
