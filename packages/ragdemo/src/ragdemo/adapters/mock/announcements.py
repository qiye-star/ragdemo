"""Mock 公告供应商。

数据取自真实公告的脱敏节选——Mock 太理想会让 P1 的评测数字虚高，
选型时才发现落差（adr/0005 的风险一节）。必须包含：正文段落、表格、更正公告。
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ragdemo.adapters.announcements import (
    NormalizedBlock,
    NormalizedDocument,
    known_at_for,
)
from ragdemo.adapters.base import FetchContext, RawResponse

CST = timezone(timedelta(hours=8))

_DOCS: tuple[dict[str, Any], ...] = (
    {
        "id": "SSE-688256-2024Q3",
        "entity_ref": "688256.SH",
        "doc_type": "quarterly",
        "title": "寒武纪 2024 年第三季度报告",
        "period": "2024Q3",
        "publish_at": "2024-10-28T18:32:00+08:00",
        "is_correction": False,
        "supersedes": None,
        "page_count": 24,
        "blocks": [
            ("title", "第三节 主营业务", 1, 11, 1),
            ("paragraph",
             "报告期内公司智能计算集群系统业务实现营业收入 12,340 万元，同比增长 58.2%，"
             "主要系云端训练芯片出货量提升所致。", 1, 12, None),
            ("table",
             "| 业务分部 | 收入(万元) | 同比 |\n| 智能计算 | 12,340 | +58.2% |\n"
             "| 其他 | 1,020 | -3.1% |", 1, 13, None),
            ("paragraph",
             "研发费用 1,890 万元，同比增长 22.4%，主要用于下一代训练芯片流片。", 1, 14, None),
        ],
    },
    {
        "id": "SSE-688256-2024Q3-CORR",
        "entity_ref": "688256.SH",
        "doc_type": "announcement",
        "title": "关于 2024 年第三季度报告更正的公告",
        "period": "2024Q3",
        "publish_at": "2025-01-15T19:00:00+08:00",
        "is_correction": True,
        "supersedes": "SSE-688256-2024Q3",
        "page_count": 2,
        "blocks": [
            ("paragraph",
             "经复核，公司 2024 年第三季度智能计算集群系统业务营业收入应为 12,500 万元，"
             "原披露 12,340 万元有误，特此更正。", 1, 1, None),
        ],
    },
)


class MockAnnouncementProvider:
    provider = "mock-announcements"

    def __init__(
        self,
        documents: Sequence[dict[str, Any]] | None = None,
        *,
        disclosure_lag: timedelta = timedelta(0),
    ) -> None:
        self._docs = tuple(documents) if documents is not None else _DOCS
        self._lag = disclosure_lag

    @property
    def disclosure_lag(self) -> timedelta:
        return self._lag

    def health(self) -> bool:
        return True

    def list_documents(
        self,
        ctx: FetchContext,
        since: datetime,
        until: datetime,
        entity_refs: Sequence[str] | None = None,
    ) -> Iterator[RawResponse]:
        for doc in self._docs:
            published = datetime.fromisoformat(doc["publish_at"])
            if not (since <= published <= until):
                continue
            if entity_refs is not None and doc["entity_ref"] not in entity_refs:
                continue
            yield RawResponse(
                provider=self.provider,
                endpoint="/documents",
                params={"since": since.isoformat(), "until": until.isoformat()},
                payload=doc,
                http_status=200,
                fetched_at=datetime.now(CST),
            )

    def fetch_document(self, ctx: FetchContext, provider_doc_id: str) -> RawResponse:
        doc = next(d for d in self._docs if d["id"] == provider_doc_id)
        return RawResponse(
            provider=self.provider, endpoint=f"/documents/{provider_doc_id}",
            params={}, payload=doc, http_status=200, fetched_at=datetime.now(CST),
        )

    def known_at(self, record: Any) -> datetime:  # noqa: ANN401
        return known_at_for(datetime.fromisoformat(record["publish_at"]), self._lag)

    def normalize(self, raw: RawResponse) -> NormalizedDocument:
        doc = raw.payload
        blocks = [
            NormalizedBlock(
                ordinal=i,
                block_type=block_type,
                section_path=_section_path(doc["blocks"], i),
                content=content,
                page=page,
                level=level,
            )
            for i, (block_type, content, _, page, level) in enumerate(doc["blocks"])
        ]
        body = "\n".join(b.content for b in blocks)
        return NormalizedDocument(
            provider_doc_id=doc["id"],
            entity_ref=doc["entity_ref"],
            doc_type=doc["doc_type"],
            title=doc["title"],
            period=doc["period"],
            publish_at=datetime.fromisoformat(doc["publish_at"]),
            language="zh",
            source_url=None,
            raw_bytes_ref=None,
            content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            is_correction=bool(doc["is_correction"]),
            supersedes_provider_doc_id=doc["supersedes"],
            page_count=doc["page_count"],
            blocks=blocks,
        )


def _section_path(raw_blocks: Sequence[tuple[Any, ...]], index: int) -> str:
    """向前找最近的 title 块作为章节路径。"""
    for i in range(index, -1, -1):
        if raw_blocks[i][0] == "title":
            return str(raw_blocks[i][1])
    return ""
