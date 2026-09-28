"""给 Agent 的数据接口契约 v1（总计划阶段 1B；文档见 docs/9.28-plan/数据接口契约-v1.md）。

三条对所有接口都成立的规则：
1. **强制 `as_of`**：关键字参数、必须带时区、没有默认值。只返回 `as_of` 时刻已知的数据
   （`known_at <= as_of` 且未被取代），回测时物理上看不到未来。
2. **每个数值都带溯源**：事实数值带 `FactCitation`（`[metric_id | source | known_at]`，
   外加回放原始响应用的 `snapshot_id`）；原文内容带 `BlockCitation`（`[block_id | page]`）；
   派生值带参与计算的全部原始事实的溯源。
3. **只读、只公开数据**：私有上传材料（`owner_user` / `owner_tenant` 非空）在任何接口都
   不可见；`can_show_raw = false` 的来源不返回正文，只返回元数据与溯源。

查不到返回 `None` / 空列表，不抛异常；参数不合法（naive `as_of`、未知的派生类型等）
抛 `ValueError`。数值一律 `Decimal`，不用 float——财务数字在 float 里会悄悄漂移。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

CONTRACT_VERSION = "1"

DerivedKind = Literal["yoy"]  # v1 只有累计同比；环比 / TTM 在阶段 2.5 作为增量加入


def require_as_of(as_of: datetime) -> None:
    """契约规则 1：`as_of` 必须带时区。每个接口实现的第一行都调用它。"""
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError(f"as_of 必须是带时区的 datetime，收到 {as_of!r}")


# --- 溯源 -------------------------------------------------------------------------


@dataclass(frozen=True)
class FactCitation:
    metric_id: str
    source: str
    known_at: datetime
    snapshot_id: int | None  # 回放原始响应（core.provider_snapshot）；历史存量行可能为 None

    def render(self) -> str:
        return f"[{self.metric_id} | {self.source} | {self.known_at.isoformat()}]"


@dataclass(frozen=True)
class BlockCitation:
    block_id: int
    page: int | None

    def render(self) -> str:
        return f"[{self.block_id} | {self.page if self.page is not None else '—'}]"


@dataclass(frozen=True)
class SourceCitation:
    """非数值记录（实体、关系、规则、口径）的来源：写入方与可知时刻。"""

    source: str
    known_at: datetime | None  # 传导规则尚未版本化，没有 known_at（见契约文档"已知限制"）


# --- 实体 -------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityInfo:
    entity_id: str
    name_full: str
    name_short: str | None
    market: str | None
    l3_nodes: tuple[str, ...]
    primary_node: str
    citation: SourceCitation


@dataclass(frozen=True)
class EntityMatch:
    entity_id: str
    name_full: str
    matched_on: str  # 命中的是全称 / 简称 / 别名 / 证券代码
    confidence: Decimal  # 0–1


# --- 口径与事实 -------------------------------------------------------------------


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    metric_name: str
    definition: str
    unit: str
    frequency: str
    version: int
    citation: SourceCitation  # source = 修改人，known_at = 这一版生效的时刻


@dataclass(frozen=True)
class FactValue:
    entity_id: str
    metric_id: str
    period: str  # 2024Q3（累计至三季度末） / 2024FY
    period_end: date
    value: Decimal
    unit: str
    currency: str | None
    citation: FactCitation


@dataclass(frozen=True)
class DerivedValue:
    entity_id: str
    metric_id: str
    kind: DerivedKind
    period: str
    value: Decimal | None  # 算不出来时为 None，原因写在 note 里——不编一个数
    note: str | None
    inputs: tuple[FactValue, ...]  # 参与计算的原始事实，各自带溯源


@dataclass(frozen=True)
class PriceBar:
    entity_id: str
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: Decimal | None
    adj_factor: Decimal
    is_suspended: bool
    citation: SourceCitation


# --- 文档与原文块 -----------------------------------------------------------------


@dataclass(frozen=True)
class DocumentInfo:
    doc_id: int
    entity_id: str | None
    doc_type: str
    title: str
    publish_at: datetime
    source: str
    source_url: str | None
    page_count: int | None
    can_show_raw: bool
    known_at: datetime


@dataclass(frozen=True)
class BlockHit:
    block_id: int
    doc_id: int
    doc_title: str
    page: int | None
    section_path: str
    score: float
    preview: str | None  # can_show_raw = false 时为 None
    citation: BlockCitation


@dataclass(frozen=True)
class BlockDetail:
    block_id: int
    doc_id: int
    block_type: str
    page: int | None
    bbox: tuple[Decimal, Decimal, Decimal, Decimal] | None  # 归一化到 [0,1]
    section_path: str
    content: str | None  # can_show_raw = false 时为 None
    content_desc: str | None
    parent_content: str | None  # 父块（整小节）正文，供生成用；引用仍用本块 block_id
    citation: BlockCitation
    document: DocumentInfo


# --- 公告目录（v1.1，公告双源接入计划 Task 14）------------------------------------


@dataclass(frozen=True)
class AnnouncementInfo:
    """`core.announcement_index` 的只读投影——目录条目不一定有全文（Wind/iFind
    降级层、暂不下载的实体都只建目录），`has_full_text` 与 `doc_id` 必须一致：
    `has_full_text = False` 时 `doc_id` 恒为 `None`，没有全文就没有可关联的文档。
    `known_at` 由各来源按自己的时间精度算好（cninfo 到日、披露易到分钟、EDGAR 到
    秒），这里原样暴露，不重算——第二条契约规则要求的溯源就是这个字段本身。"""

    entity_id: str
    source: str  # cninfo / wind / ifind / hkexnews / edgar
    title: str
    publish_date: date
    has_full_text: bool
    doc_id: int | None
    url: str | None
    known_at: datetime


# --- 产业链 -----------------------------------------------------------------------


@dataclass(frozen=True)
class Relation:
    from_entity: str
    to_entity: str
    relation_type: str
    strength: Decimal | None
    share_estimate: Decimal | None
    confidence: str
    evidence_blocks: tuple[int, ...]
    citation: SourceCitation  # source 以 manual: 开头的是手工录入关系（乐观偏差，见 03 §6.3）


@dataclass(frozen=True)
class PropagationRule:
    rule_id: int
    trigger_node: str
    trigger_event: str
    affected_node: str
    direction: int
    lag_days: int
    mechanism: str
    weight_field: str | None
    confidence: str
    author: str
    evidence_blocks: tuple[int, ...] = field(default_factory=tuple)


# --- 接口 -------------------------------------------------------------------------


@runtime_checkable
class DataApi(Protocol):
    def resolve_entity(self, name: str, *, as_of: datetime) -> list[EntityMatch]:
        """名称 / 简称 / 别名 / 证券代码 → 候选实体，按置信度降序；未命中返回空列表。"""
        ...

    def list_entities(self, *, as_of: datetime, node: str | None = None) -> list[EntityInfo]:
        """实体清单；传 `node` 只返回挂在该三级环节上的实体。"""
        ...

    def get_metric_definition(self, metric_id: str, *, as_of: datetime) -> MetricDefinition | None:
        """`as_of` 时刻生效的那一版口径定义。"""
        ...

    def query_fin_fact(
        self, entity_id: str, metric_id: str, *, as_of: datetime, period: str | None = None
    ) -> list[FactValue]:
        """`as_of` 时刻已知的财务事实（被更正的取更正后的值），按 period_end 升序。"""
        ...

    def query_derived(
        self,
        entity_id: str,
        metric_id: str,
        kind: DerivedKind,
        *,
        as_of: datetime,
        period: str,
    ) -> DerivedValue | None:
        """代码计算的派生值（v1：累计同比）。本期数据查不到返回 None。"""
        ...

    def get_prices(
        self, entity_id: str, *, start: date, end: date, as_of: datetime
    ) -> list[PriceBar]:
        """[start, end] 内 `as_of` 时刻已知的日行情，按交易日升序。"""
        ...

    def search_blocks(
        self,
        query: str,
        *,
        as_of: datetime,
        entity_ids: Sequence[str] = (),
        doc_types: Sequence[str] = (),
        k: int = 10,
    ) -> list[BlockHit]:
        """原文检索（过滤 → BM25 + 向量 → 融合 → 重排），最多 k 条，按相关度降序。"""
        ...

    def get_block(self, block_id: int, *, as_of: datetime) -> BlockDetail | None: ...

    def get_document(self, doc_id: int, *, as_of: datetime) -> DocumentInfo | None: ...

    def get_relations(
        self, entity_id: str, *, as_of: datetime, relation_type: str | None = None
    ) -> list[Relation]:
        """以该实体为起点或终点的产业链关系。"""
        ...

    def match_rules(
        self, event_type: str, trigger_node: str, *, as_of: datetime
    ) -> list[PropagationRule]:
        """某环节发生某类事件时适用的传导规则（只返回启用中的）。"""
        ...

    def list_announcements(
        self,
        entity_id: str,
        *,
        as_of: datetime,
        since: date | None = None,
        until: date | None = None,
    ) -> list[AnnouncementInfo]:
        """`entity_id` 的公告目录（v1.1），按 `publish_date` 升序。只返回 `as_of`
        时刻已知、未被取代的现行行——降级层（Wind/iFind）的行被主源（cninfo 等）
        取代后不再出现；`since`/`until` 按 `publish_date` 收窄，不传则不限制。"""
        ...
