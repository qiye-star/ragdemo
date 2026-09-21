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
from ragdemo_core.blob import BlobStore

MAX_INLINE_PAYLOAD_BYTES = 1_000_000


def save_snapshot(
    conn: psycopg.Connection,
    raw: RawResponse,
    *,
    ingest_run_id: str,
    blob: BlobStore | None = None,
) -> int:
    """落库一条原始响应，返回 snapshot_id。

    超过 MAX_INLINE_PAYLOAD_BYTES 的响应不写 jsonb 列，只记一个对象存储 key。
    未传 blob 时（默认）只占位，不实际上传——保持与早期调用方一致的行为；
    传了 blob 才会把编码后的字节真正落到对象存储，让 response_ref 指向真实内容。
    """
    # 用 default=str 容忍 Decimal / date 等非 JSON 原生类型（仅用于测量与落库，不改变语义）。
    encoded = json.dumps(raw.payload, ensure_ascii=False, default=str)
    encoded_bytes = encoded.encode("utf-8")
    oversized = len(encoded_bytes) > MAX_INLINE_PAYLOAD_BYTES
    response_ref = f"snapshot/{raw.provider}/{uuid.uuid4().hex}.json" if oversized else None

    # 存进 jsonb 列的必须和用来测量大小的是同一份编码结果——再次 json.loads 回普通
    # Python 对象，保证 Jsonb(...) 不会在 psycopg 自己的编码器里对 Decimal/date 之类
    # 的类型报 TypeError。
    stored_payload = json.loads(encoded)

    if oversized:
        if blob is not None:
            assert response_ref is not None
            blob.put(response_ref, encoded_bytes)
        response_jsonb = None
    else:
        response_jsonb = Jsonb(stored_payload)

    row = conn.execute(
        "INSERT INTO core.provider_snapshot (provider, endpoint, params, response,"
        " response_ref, http_status, ingest_run_id, cost_cents, fetched_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING snapshot_id",
        (
            raw.provider,
            raw.endpoint,
            Jsonb(strip_secrets(raw.params)),
            response_jsonb,
            response_ref,
            raw.http_status,
            ingest_run_id,
            raw.cost_cents,
            raw.fetched_at,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])
