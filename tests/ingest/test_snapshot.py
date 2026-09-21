"""原始响应留存：永不修改、永不删除，且不含密钥。"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.base import RawResponse
from ragdemo.ingest.snapshot import MAX_INLINE_PAYLOAD_BYTES, save_snapshot
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
