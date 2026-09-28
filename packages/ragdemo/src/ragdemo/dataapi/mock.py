"""数据接口契约的内存 Mock 实现：让 Agent 负责人不等真实数据就能开工。

默认装载 `sample.SAMPLE`，也可以传入自己的 `SampleDataset`。它和真实实现通过**同一套**
契约测试（tests/contracts/dataapi_contract.py），时点过滤、可见性、溯源格式的行为与
真实实现一致；检索排序只是朴素的关键词计数，不模拟 BM25 + 向量 + 重排的真实质量。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from ragdemo.dataapi.contract import (
    BlockCitation,
    BlockDetail,
    BlockHit,
    DerivedKind,
    DerivedValue,
    DocumentInfo,
    EntityInfo,
    EntityMatch,
    FactCitation,
    FactValue,
    MetricDefinition,
    PriceBar,
    PropagationRule,
    Relation,
    SourceCitation,
    require_as_of,
)
from ragdemo.dataapi.sample import (
    SAMPLE,
    SampleBlock,
    SampleDataset,
    SampleDocument,
    SampleFact,
)
from ragdemo.metrics.derived import prior_year_period, yoy


def _visible(known_at: datetime, superseded_at: datetime | None, as_of: datetime) -> bool:
    return known_at <= as_of and (superseded_at is None or superseded_at > as_of)


class MockDataApi:
    def __init__(self, dataset: SampleDataset = SAMPLE) -> None:
        self._d = dataset

    # --- 实体 ---------------------------------------------------------------------

    def resolve_entity(self, name: str, *, as_of: datetime) -> list[EntityMatch]:
        require_as_of(as_of)
        wanted = name.strip().casefold()
        matches: list[EntityMatch] = []
        for e in self._d.entities:
            if e.known_at > as_of:
                continue
            candidates = [
                (e.name_full, "全称", Decimal("1.0")),
                (e.tushare_code, "证券代码", Decimal("1.0")),
                (e.name_short, "简称", Decimal("0.95")),
                *((a, "别名", Decimal("0.9")) for a in e.aliases),
            ]
            hits = [(how, conf) for text, how, conf in candidates if text.casefold() == wanted]
            if hits:
                how, conf = max(hits, key=lambda h: h[1])
                matches.append(EntityMatch(e.entity_id, e.name_full, how, conf))
        return sorted(matches, key=lambda m: (-m.confidence, m.entity_id))

    def list_entities(self, *, as_of: datetime, node: str | None = None) -> list[EntityInfo]:
        require_as_of(as_of)
        return [
            EntityInfo(
                e.entity_id,
                e.name_full,
                e.name_short,
                e.market,
                e.l3_nodes,
                e.primary_node,
                SourceCitation("manual:seed", e.known_at),
            )
            for e in self._d.entities
            if e.known_at <= as_of and (node is None or node in e.l3_nodes)
        ]

    # --- 口径与事实 ---------------------------------------------------------------

    def get_metric_definition(self, metric_id: str, *, as_of: datetime) -> MetricDefinition | None:
        require_as_of(as_of)
        for m in self._d.metrics:
            if m.metric_id == metric_id and _visible(m.known_at, m.superseded_at, as_of):
                return MetricDefinition(
                    m.metric_id,
                    m.metric_name,
                    m.definition,
                    m.unit,
                    m.frequency,
                    m.version,
                    SourceCitation(f"manual:{m.author}", m.known_at),
                )
        return None

    @staticmethod
    def _fact(f: SampleFact) -> FactValue:
        return FactValue(
            f.entity_id,
            f.metric_id,
            f.period,
            f.period_end,
            f.value,
            f.unit,
            f.currency,
            FactCitation(f.metric_id, f.source, f.known_at, f.snapshot_id),
        )

    def query_fin_fact(
        self, entity_id: str, metric_id: str, *, as_of: datetime, period: str | None = None
    ) -> list[FactValue]:
        require_as_of(as_of)
        facts = [
            self._fact(f)
            for f in self._d.facts
            if f.entity_id == entity_id
            and f.metric_id == metric_id
            and (period is None or f.period == period)
            and _visible(f.known_at, f.superseded_at, as_of)
        ]
        return sorted(facts, key=lambda f: f.period_end)

    def query_derived(
        self,
        entity_id: str,
        metric_id: str,
        kind: DerivedKind,
        *,
        as_of: datetime,
        period: str,
    ) -> DerivedValue | None:
        require_as_of(as_of)
        if kind != "yoy":
            raise ValueError(f"未知的派生类型 {kind!r}（契约 v1 只有 yoy）")
        current = self.query_fin_fact(entity_id, metric_id, as_of=as_of, period=period)
        if not current:
            return None
        base = self.query_fin_fact(
            entity_id, metric_id, as_of=as_of, period=prior_year_period(period)
        )
        return yoy(current[0], base[0] if base else None)

    def get_prices(
        self, entity_id: str, *, start: date, end: date, as_of: datetime
    ) -> list[PriceBar]:
        require_as_of(as_of)
        if start > end:
            raise ValueError(f"start {start} 晚于 end {end}")
        return [
            PriceBar(
                p.entity_id,
                p.trade_date,
                None,
                None,
                None,
                p.close,
                None,
                Decimal(1),
                False,
                SourceCitation("tushare", p.known_at),
            )
            for p in sorted(self._d.prices, key=lambda p: p.trade_date)
            if p.entity_id == entity_id and start <= p.trade_date <= end and p.known_at <= as_of
        ]

    # --- 文档与原文块 -------------------------------------------------------------

    def _public_doc(self, doc_id: int, as_of: datetime) -> SampleDocument | None:
        for d in self._d.documents:
            if d.doc_id == doc_id and d.owner_user is None and d.known_at <= as_of:
                return d
        return None

    @staticmethod
    def _doc_info(d: SampleDocument) -> DocumentInfo:
        return DocumentInfo(
            d.doc_id,
            d.entity_id,
            d.doc_type,
            d.title,
            d.publish_at,
            d.source,
            d.source_url,
            d.page_count,
            d.can_show_raw,
            d.known_at,
        )

    def search_blocks(
        self,
        query: str,
        *,
        as_of: datetime,
        entity_ids: Sequence[str] = (),
        doc_types: Sequence[str] = (),
        k: int = 10,
    ) -> list[BlockHit]:
        require_as_of(as_of)
        if k <= 0:
            raise ValueError(f"k 必须为正整数，收到 {k}")
        terms = [t for t in query.split() if t]
        hits: list[BlockHit] = []
        for b in self._d.blocks:
            doc = self._public_doc(b.doc_id, as_of)
            if not b.is_leaf or doc is None:
                continue
            if entity_ids and doc.entity_id not in entity_ids:
                continue
            if doc_types and doc.doc_type not in doc_types:
                continue
            score = sum(b.content.count(t) for t in terms)
            if score:
                hits.append(
                    BlockHit(
                        b.block_id,
                        b.doc_id,
                        doc.title,
                        b.page,
                        b.section_path,
                        float(score),
                        b.content[:80] if doc.can_show_raw else None,
                        BlockCitation(b.block_id, b.page),
                    )
                )
        return sorted(hits, key=lambda h: (-h.score, h.block_id))[:k]

    def _block(self, block_id: int) -> SampleBlock | None:
        return next((b for b in self._d.blocks if b.block_id == block_id), None)

    def get_block(self, block_id: int, *, as_of: datetime) -> BlockDetail | None:
        require_as_of(as_of)
        b = self._block(block_id)
        doc = self._public_doc(b.doc_id, as_of) if b else None
        if b is None or doc is None:
            return None
        parent = self._block(b.parent_block_id) if b.parent_block_id else None
        show = doc.can_show_raw
        return BlockDetail(
            b.block_id,
            b.doc_id,
            b.block_type,
            b.page,
            b.bbox,
            b.section_path,
            b.content if show else None,
            b.content_desc if show else None,
            parent.content if (parent and show) else None,
            BlockCitation(b.block_id, b.page),
            self._doc_info(doc),
        )

    def get_document(self, doc_id: int, *, as_of: datetime) -> DocumentInfo | None:
        require_as_of(as_of)
        doc = self._public_doc(doc_id, as_of)
        return self._doc_info(doc) if doc else None

    # --- 产业链 -------------------------------------------------------------------

    def get_relations(
        self, entity_id: str, *, as_of: datetime, relation_type: str | None = None
    ) -> list[Relation]:
        require_as_of(as_of)
        return [
            Relation(
                r.from_entity,
                r.to_entity,
                r.relation_type,
                r.strength,
                None,
                r.confidence,
                r.evidence_blocks,
                SourceCitation(r.source, r.known_at),
            )
            for r in self._d.relations
            if entity_id in (r.from_entity, r.to_entity)
            and r.known_at <= as_of
            and (relation_type is None or r.relation_type == relation_type)
        ]

    def match_rules(
        self, event_type: str, trigger_node: str, *, as_of: datetime
    ) -> list[PropagationRule]:
        require_as_of(as_of)
        return [
            s.rule
            for s in self._d.rules
            if s.enabled
            and s.rule.trigger_event == event_type
            and s.rule.trigger_node == trigger_node
        ]
