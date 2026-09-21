"""原始响应留存。

任何「数据对不对」的争议都回到 provider_snapshot 重放。因此它永不修改、永不删除。
"""
from __future__ import annotations

import json
import uuid

import psycopg
from psycopg.types.json import Jsonb

from ragdemo.adapters.base import RawResponse
from ragdemo.adapters.secrets import strip_secrets

MAX_INLINE_PAYLOAD_BYTES = 1_000_000


def save_snapshot(conn: psycopg.Connection, raw: RawResponse, *, ingest_run_id: str) -> int:
    """落库一条原始响应，返回 snapshot_id。

    超过 MAX_INLINE_PAYLOAD_BYTES 的响应不写 jsonb 列，只记一个对象存储 key；
    实际上传由调用方完成（P1 阶段对象存储尚未接入，key 先占位）。
    """
    encoded = json.dumps(raw.payload, ensure_ascii=False, default=str)
    oversized = len(encoded.encode("utf-8")) > MAX_INLINE_PAYLOAD_BYTES
    response_ref = f"snapshot/{raw.provider}/{uuid.uuid4().hex}.json" if oversized else None

    row = conn.execute(
        "INSERT INTO core.provider_snapshot (provider, endpoint, params, response,"
        " response_ref, http_status, ingest_run_id, cost_cents) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING snapshot_id",
        (
            raw.provider,
            raw.endpoint,
            Jsonb(strip_secrets(raw.params)),
            None if oversized else Jsonb(raw.payload),
            response_ref,
            raw.http_status,
            ingest_run_id,
            raw.cost_cents,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])
