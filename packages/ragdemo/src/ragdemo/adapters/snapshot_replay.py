"""用已留存的 `provider_snapshot` 回放 Wind 网关（不发起任何真实调用、不消耗额度）。

Wind 的自然语言接口响应不稳定、解析规则会随实测迭代——解析规则改进后，用同一个问句
最近一次留存的原始响应重新解析入库，比重新调用 Wind 省额度，也保证"入库的正是留档的那份"。
问句是快照 `params.question`，工具名是 `endpoint`。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import psycopg

from ragdemo.adapters.base import RawResponse
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.mcp_gateway import ToolCall, ToolSpec


class NoSnapshot(AdapterError):
    """没有这个问句的留存响应——回放模式下不是网关故障，不应触发"连续失败中止"。"""


class SnapshotReplayGateway:
    provider = "wind-mcp"

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def health(self) -> bool:
        return True

    def list_tools(self) -> list[ToolSpec]:
        return []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolCall:
        question = str(arguments["question"])
        row = self._conn.execute(
            "SELECT response FROM core.provider_snapshot"
            " WHERE endpoint = %s AND params->>'question' = %s AND response IS NOT NULL"
            " ORDER BY snapshot_id DESC LIMIT 1",
            (name, question),
        ).fetchone()
        if row is None:
            raise NoSnapshot(f"没有 {name} 的留存响应：{question}")
        payload = row[0]
        text = json.dumps(payload, ensure_ascii=False)
        raw = RawResponse(
            provider=self.provider,
            endpoint=name,
            params={"question": question, "replay": True},
            payload=payload,
            http_status=200,
            fetched_at=datetime.now(UTC),
        )
        return ToolCall(tool=name, text=text, payload=payload, is_error=False, raw=raw)
