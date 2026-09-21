"""实体解析三层降级（docs/04-ingestion.md §4）。

1. 代码精确匹配 → high
2. 别名精确匹配（唯一）→ high
3. 上下文打分（共现实体）→ medium

三层都不确定就**不猜**，进 core.entity_resolution_queue 等人工处理。
模糊匹配（pg_trgm）只用于给人工提供候选，绝不自动采纳——把「中兴新材」
匹配成「中兴通讯」这类错误一旦入库，污染会扩散到关系图和观点。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

FUZZY_SIMILARITY_FLOOR = 0.4


@dataclass(frozen=True)
class Candidate:
    entity_id: str
    score: float
    reason: str


@dataclass(frozen=True)
class Resolution:
    entity_id: str | None
    confidence: str
    layer: str
    candidates: list[Candidate] = field(default_factory=list)


class EntityResolver:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def resolve_code(self, code: str) -> Resolution:
        """第 1 层：供应商代码精确匹配。"""
        row = self.conn.execute(
            "SELECT entity_id FROM core.entity "
            " WHERE tushare_code = %s OR ifind_code = %s OR wind_code = %s OR edgar_cik = %s",
            (code, code, code, code),
        ).fetchone()
        if row is None:
            return Resolution(None, "low", "code")
        return Resolution(str(row[0]), "high", "code")

    def resolve_name(
        self,
        name: str,
        *,
        context_entities: Sequence[str] = (),
        doc_type: str | None = None,
    ) -> Resolution:
        """第 2–3 层：别名精确匹配，命中多个时用上下文消歧。"""
        exact = [
            str(r[0])
            for r in self.conn.execute(
                "SELECT entity_id FROM core.entity_alias WHERE alias = %s", (name,)
            ).fetchall()
        ]

        if len(exact) == 1:
            return Resolution(exact[0], "high", "alias")

        if len(exact) > 1:
            candidates = [Candidate(e, 1.0, "别名精确匹配") for e in exact]
            in_context = [e for e in exact if e in context_entities]
            if len(in_context) == 1:
                return Resolution(in_context[0], "medium", "context", candidates)
            return Resolution(None, "low", "alias_ambiguous", candidates)

        return Resolution(None, "low", "fuzzy", self._fuzzy_candidates(name))

    def _fuzzy_candidates(self, name: str) -> list[Candidate]:
        """模糊候选只供人工参考，永不自动采纳。"""
        rows = self.conn.execute(
            "SELECT entity_id, similarity(alias, %s) AS s FROM core.entity_alias "
            " WHERE similarity(alias, %s) > %s ORDER BY s DESC LIMIT 5",
            (name, name, FUZZY_SIMILARITY_FLOOR),
        ).fetchall()
        return [Candidate(str(r[0]), float(r[1]), "模糊匹配（需人工确认）") for r in rows]

    def enqueue_unresolved(
        self,
        resolution: Resolution,
        *,
        raw_ref: str,
        context: Mapping[str, Any],
        source: str,
        ingest_run_id: str,
    ) -> int:
        """写入人工消歧队列。队列长度是数据质量的日常监控指标。"""
        if resolution.entity_id is not None:
            raise ValueError("已解析的实体不应进入人工队列")
        row = self.conn.execute(
            "INSERT INTO core.entity_resolution_queue "
            " (raw_ref, context, candidates, source, ingest_run_id) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (
                raw_ref,
                Jsonb(dict(context)),
                Jsonb(
                    [
                        {"entity_id": c.entity_id, "score": c.score, "reason": c.reason}
                        for c in resolution.candidates
                    ]
                ),
                source,
                ingest_run_id,
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])
