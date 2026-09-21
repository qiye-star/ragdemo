"""公告供应商的归一化模型与协议。

这份模型由我们的检索需求定义，不由任何供应商定义（adr/0005）。
选定供应商后只需写一个实现 AnnouncementProvider 的 adapter，
并原样通过 tests/contracts/announcement_contract.py 的 6 条契约。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from ragdemo.adapters.base import FetchContext, RawResponse, require_aware

BLOCK_TYPES = frozenset({"paragraph", "table", "figure", "title"})


@dataclass(frozen=True)
class NormalizedBlock:
    ordinal: int
    block_type: str
    section_path: str
    content: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    level: int | None = None

    def __post_init__(self) -> None:
        if self.block_type not in BLOCK_TYPES:
            raise ValueError(f"未知 block_type: {self.block_type!r}")
        if not self.content.strip():
            raise ValueError(f"第 {self.ordinal} 块内容为空")


@dataclass(frozen=True)
class NormalizedDocument:
    provider_doc_id: str
    entity_ref: str | None
    doc_type: str
    title: str
    period: str | None
    publish_at: datetime
    language: str
    source_url: str | None
    raw_bytes_ref: str | None
    content_hash: str
    is_correction: bool
    supersedes_provider_doc_id: str | None
    page_count: int | None
    blocks: Sequence[NormalizedBlock]

    def __post_init__(self) -> None:
        require_aware(self.publish_at, "publish_at")
        ordinals = [b.ordinal for b in self.blocks]
        if ordinals != list(range(len(ordinals))):
            raise ValueError(f"{self.provider_doc_id} 的 block ordinal 不连续: {ordinals}")


def known_at_for(publish_at: datetime, disclosure_lag: timedelta) -> datetime:
    """把发布时刻换算成我们能获知的时刻。见 docs/03-point-in-time.md §1.4。"""
    require_aware(publish_at, "publish_at")
    if disclosure_lag < timedelta(0):
        raise ValueError(f"disclosure_lag 不能为负: {disclosure_lag}")
    return publish_at + disclosure_lag


@runtime_checkable
class AnnouncementProvider(Protocol):
    provider: str

    @property
    def disclosure_lag(self) -> timedelta:
        """相对真实发布时间的获知延迟。实时推送为 0；T+1 批量为 1 天。"""

    def health(self) -> bool: ...

    def list_documents(
        self,
        ctx: FetchContext,
        since: datetime,
        until: datetime,
        entity_refs: Sequence[str] | None = None,
    ) -> Iterator[RawResponse]: ...

    def fetch_document(self, ctx: FetchContext, provider_doc_id: str) -> RawResponse: ...

    def normalize(self, raw: RawResponse) -> NormalizedDocument: ...

    def known_at(self, record: Any) -> datetime: ...  # noqa: ANN401
