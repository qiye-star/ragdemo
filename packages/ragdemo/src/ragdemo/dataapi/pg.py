"""数据接口契约的数据库实现（阶段 2.10）：只经 `asof` 视图读时点事实。

与 `MockDataApi` 通过同一套契约测试（tests/contracts/dataapi_contract.py，
`tests/contracts/test_pg_dataapi.py` 把 `sample.SAMPLE` 种进临时库再跑）。

三条规则的落点（契约模块文档）：
1. `as_of` 强制：每个方法第一行 `require_as_of`，随后 `as_of_session` 把它绑定到事务，
   `asof.*` 视图据此过滤 `known_at` / `superseded_at`；
2. 溯源：事实带 `FactCitation`（含 `snapshot_id`），结构化记录带 `RecordCitation`，
   原文块带 `BlockCitation`；
3. 私有材料不可见：视图上的行级隔离（`app.user` 为空 = 只看公共数据）；
   `can_show_raw = false` 的来源不返回正文，只返回元数据与溯源。

已知限制（与 Mock 一致，见契约文档"已知限制"）：实体、传导规则不是时点表——没有
`known_at`，`SourceCitation.known_at` 为 `None`；实体环节归属取 `core.entity.l3_node`。

`search_blocks` 委托给注入的 `RetrievalService`（过滤 → BM25 + 向量 → 融合 → 重排）；
没有注入检索服务时该方法抛 `RuntimeError`，其余方法不受影响。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg

from ragdemo.dataapi.contract import (
    AnnouncementInfo,
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
    FxQuote,
    MetricDefinition,
    PriceBar,
    PropagationRule,
    RecordCitation,
    Relation,
    SegmentValue,
    ShareholderHolding,
    SourceCitation,
    require_as_of,
)
from ragdemo.metrics.derived import derive
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest
from ragdemo_core.db.session import as_of_session

_MAX_FX_STALE_DAYS = 7


def _bbox(raw: Sequence[Any] | None) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    if raw is None:
        return None
    a, b, c, d = (Decimal(str(x)) for x in raw)
    return (a, b, c, d)


class PgDataApi:
    def __init__(
        self, conn: psycopg.Connection, *, retrieval: RetrievalService | None = None
    ) -> None:
        self._conn = conn
        self._retrieval = retrieval

    # --- 实体 ---------------------------------------------------------------------

    def resolve_entity(self, name: str, *, as_of: datetime) -> list[EntityMatch]:
        require_as_of(as_of)
        wanted = name.strip()
        rows = self._conn.execute(
            """
            SELECT entity_id, name_full, how, conf FROM (
              SELECT entity_id, name_full, '全称' AS how, 1.0 AS conf FROM core.entity
               WHERE lower(name_full) = lower(%(w)s)
              UNION ALL
              SELECT entity_id, name_full, '证券代码', 1.0 FROM core.entity
               WHERE lower(tushare_code) = lower(%(w)s)
              UNION ALL
              SELECT entity_id, name_full, '简称', 0.95 FROM core.entity
               WHERE lower(name_short) = lower(%(w)s)
              UNION ALL
              SELECT e.entity_id, e.name_full, '别名', 0.9 FROM core.entity_alias a
                JOIN core.entity e USING (entity_id) WHERE lower(a.alias) = lower(%(w)s)
            ) hits
            """,
            {"w": wanted},
        ).fetchall()
        best: dict[str, EntityMatch] = {}
        for entity_id, name_full, how, conf in rows:
            confidence = Decimal(str(conf))
            current = best.get(entity_id)
            if current is None or confidence > current.confidence:
                best[entity_id] = EntityMatch(entity_id, name_full, how, confidence)
        return sorted(best.values(), key=lambda m: (-m.confidence, m.entity_id))

    def list_entities(self, *, as_of: datetime, node: str | None = None) -> list[EntityInfo]:
        require_as_of(as_of)
        rows = self._conn.execute(
            "SELECT entity_id, name_full, name_short, market, l3_node, primary_node"
            " FROM core.entity WHERE (%(n)s::text IS NULL OR %(n)s = ANY(l3_node))"
            " ORDER BY entity_id",
            {"n": node},
        ).fetchall()
        return [
            EntityInfo(
                r[0], r[1], r[2], r[3], tuple(r[4]), r[5], SourceCitation("manual:seed", None)
            )
            for r in rows
        ]

    # --- 口径与事实 ---------------------------------------------------------------

    def get_metric_definition(self, metric_id: str, *, as_of: datetime) -> MetricDefinition | None:
        require_as_of(as_of)
        with as_of_session(self._conn, as_of):
            row = self._conn.execute(
                "SELECT metric_id, metric_name, definition, unit, frequency, version, author,"
                " known_at FROM asof.node_metric_version WHERE metric_id = %s"
                " ORDER BY version DESC LIMIT 1",
                (metric_id,),
            ).fetchone()
        if row is None:
            return None
        return MetricDefinition(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            SourceCitation(f"manual:{row[6]}", row[7]),
        )

    def query_fin_fact(
        self, entity_id: str, metric_id: str, *, as_of: datetime, period: str | None = None
    ) -> list[FactValue]:
        require_as_of(as_of)
        with as_of_session(self._conn, as_of):
            rows = self._conn.execute(
                "SELECT entity_id, metric_id, period, period_end, value, unit, currency, source,"
                " known_at, snapshot_id FROM asof.fin_fact"
                " WHERE entity_id = %s AND metric_id = %s AND (%s::text IS NULL OR period = %s)"
                " ORDER BY period_end",
                (entity_id, metric_id, period, period),
            ).fetchall()
        return [
            FactValue(
                r[0],
                r[1],
                r[2],
                r[3],
                Decimal(r[4]),
                r[5],
                r[6],
                FactCitation(r[1], r[7], r[8], r[9]),
            )
            for r in rows
        ]

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
        facts = {f.period: f for f in self.query_fin_fact(entity_id, metric_id, as_of=as_of)}
        return derive(kind, facts, period, metric_id=metric_id)

    def get_prices(
        self, entity_id: str, *, start: date, end: date, as_of: datetime
    ) -> list[PriceBar]:
        require_as_of(as_of)
        if start > end:
            raise ValueError(f"start {start} 晚于 end {end}")
        with as_of_session(self._conn, as_of):
            rows = self._conn.execute(
                "SELECT entity_id, trade_date, open, high, low, close, volume, adj_factor,"
                " is_suspended, source, known_at FROM asof.price_daily"
                " WHERE entity_id = %s AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
                (entity_id, start, end),
            ).fetchall()

        def num(x: Any) -> Decimal | None:  # noqa: ANN401
            return None if x is None else Decimal(x)

        return [
            PriceBar(
                r[0],
                r[1],
                num(r[2]),
                num(r[3]),
                num(r[4]),
                Decimal(r[5]),
                num(r[6]),
                Decimal(r[7]),
                bool(r[8]),
                SourceCitation(r[9], r[10]),
            )
            for r in rows
        ]

    # --- 文档与原文块 -------------------------------------------------------------

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
        if self._retrieval is None:
            raise RuntimeError("PgDataApi 没有注入 RetrievalService，无法做原文检索")
        cfg = RetrievalConfig(candidate_k=max(50, k), fusion_k=max(50, k), top_k=k)
        result = self._retrieval.search(
            RetrievalRequest(
                query=query,
                as_of=as_of,
                entity_ids=list(entity_ids) or None,
                doc_types=list(doc_types) or None,
                config=cfg,
            )
        )
        hit_ids = [
            b.matched_child_ids[0] if b.matched_child_ids else b.block_id for b in result.blocks
        ]
        if not hit_ids:
            return []
        with as_of_session(self._conn, as_of):
            rows = {
                r[0]: r
                for r in self._conn.execute(
                    "SELECT b.block_id, b.doc_id, d.title, b.page, b.section_path, b.content,"
                    " b.can_show_raw FROM asof.doc_block b JOIN asof.document d USING (doc_id)"
                    " WHERE b.block_id = ANY(%s)",
                    (hit_ids,),
                ).fetchall()
            }
        hits: list[BlockHit] = []
        for evidence, block_id in zip(result.blocks, hit_ids, strict=True):
            row = rows.get(block_id)
            if row is None:
                continue
            hits.append(
                BlockHit(
                    block_id,
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    float(evidence.score),
                    row[5][:80] if row[6] else None,
                    BlockCitation(block_id, row[3]),
                )
            )
        return hits

    @staticmethod
    def _doc_info(r: Sequence[Any]) -> DocumentInfo:
        return DocumentInfo(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], bool(r[8]), r[9])

    _DOC_COLUMNS = (
        "d.doc_id, d.entity_id, d.doc_type, d.title, d.publish_at, d.source, d.source_url,"
        " d.page_count, d.can_show_raw, d.known_at"
    )

    def get_block(self, block_id: int, *, as_of: datetime) -> BlockDetail | None:
        require_as_of(as_of)
        with as_of_session(self._conn, as_of):
            row = self._conn.execute(
                f"SELECT b.block_id, b.doc_id, b.block_type::text, b.page, b.bbox, b.section_path,"
                " b.content, b.content_desc, b.can_show_raw, b.parent_block_id,"
                f" {self._DOC_COLUMNS} FROM asof.doc_block b JOIN asof.document d USING (doc_id)"
                " WHERE b.block_id = %s",
                (block_id,),
            ).fetchone()
            if row is None:
                return None
            parent_content = None
            if row[9] is not None:
                parent = self._conn.execute(
                    "SELECT content FROM asof.doc_block WHERE block_id = %s", (row[9],)
                ).fetchone()
                parent_content = parent[0] if parent else None
        show = bool(row[8])
        return BlockDetail(
            row[0],
            row[1],
            row[2],
            row[3],
            _bbox(row[4]),
            row[5],
            row[6] if show else None,
            row[7] if show else None,
            parent_content if show else None,
            BlockCitation(row[0], row[3]),
            self._doc_info(row[10:]),
        )

    def get_document(self, doc_id: int, *, as_of: datetime) -> DocumentInfo | None:
        require_as_of(as_of)
        with as_of_session(self._conn, as_of):
            row = self._conn.execute(
                f"SELECT {self._DOC_COLUMNS} FROM asof.document d WHERE d.doc_id = %s",
                (doc_id,),
            ).fetchone()
        return self._doc_info(row) if row else None

    # --- 产业链 -------------------------------------------------------------------

    def get_relations(
        self, entity_id: str, *, as_of: datetime, relation_type: str | None = None
    ) -> list[Relation]:
        require_as_of(as_of)
        with as_of_session(self._conn, as_of):
            rows = self._conn.execute(
                "SELECT from_entity, to_entity, relation_type::text, strength, share_estimate,"
                " confidence::text, evidence_block, source, known_at FROM asof.entity_relation"
                " WHERE (from_entity = %(e)s OR to_entity = %(e)s)"
                " AND (%(t)s::text IS NULL OR relation_type::text = %(t)s)"
                " ORDER BY known_at, from_entity, to_entity",
                {"e": entity_id, "t": relation_type},
            ).fetchall()
        return [
            Relation(
                r[0],
                r[1],
                r[2],
                None if r[3] is None else Decimal(r[3]),
                None if r[4] is None else Decimal(r[4]),
                r[5],
                tuple(r[6] or ()),
                SourceCitation(r[7], r[8]),
            )
            for r in rows
        ]

    def match_rules(
        self, event_type: str, trigger_node: str, *, as_of: datetime
    ) -> list[PropagationRule]:
        require_as_of(as_of)
        rows = self._conn.execute(
            "SELECT rule_id, trigger_node, trigger_event, affected_node, direction, lag_days,"
            " mechanism, weight_field, confidence::text, author, evidence_block"
            " FROM core.propagation_rule WHERE enabled AND trigger_event = %s AND trigger_node = %s"
            " ORDER BY rule_id",
            (event_type, trigger_node),
        ).fetchall()
        return [
            PropagationRule(
                r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], tuple(r[10] or ())
            )
            for r in rows
        ]

    # --- 公告目录（v1.1）-----------------------------------------------------------

    def list_announcements(
        self,
        entity_id: str,
        *,
        as_of: datetime,
        since: date | None = None,
        until: date | None = None,
    ) -> list[AnnouncementInfo]:
        require_as_of(as_of)
        with as_of_session(self._conn, as_of):
            rows = self._conn.execute(
                "SELECT entity_id, source, title, publish_date, has_full_text, doc_id, url,"
                " known_at"
                " FROM asof.announcement_index WHERE entity_id = %s"
                " AND (%s::date IS NULL OR publish_date >= %s)"
                " AND (%s::date IS NULL OR publish_date <= %s)"
                " ORDER BY publish_date, known_at, index_id",
                (entity_id, since, since, until, until),
            ).fetchall()
        return [
            AnnouncementInfo(
                r[0],
                r[1],
                r[2],
                r[3],
                bool(r[4]),
                r[5] if r[4] else None,  # 没有全文就没有可关联的文档
                r[6],
                r[7],
            )
            for r in rows
        ]

    # --- 股东 / 分部 / 汇率（v1.2）--------------------------------------------------

    def query_shareholders(
        self, entity_id: str, *, as_of: datetime, period: str | None = None
    ) -> list[ShareholderHolding]:
        require_as_of(as_of)
        with as_of_session(self._conn, as_of):
            rows = self._conn.execute(
                "SELECT entity_id, period, period_end, rank, holder_name, shares, ratio, source,"
                " known_at, snapshot_id, known_at_basis FROM asof.shareholder_holding"
                " WHERE entity_id = %s AND (%s::text IS NULL OR period = %s)"
                " ORDER BY period_end, rank",
                (entity_id, period, period),
            ).fetchall()
        return [
            ShareholderHolding(
                r[0],
                r[1],
                r[2],
                r[3],
                r[4],
                Decimal(r[5]),
                Decimal(r[6]),
                RecordCitation(r[7], r[8], r[9], r[10]),
            )
            for r in rows
        ]

    def query_segments(
        self,
        entity_id: str,
        *,
        as_of: datetime,
        period: str | None = None,
        dimension: str | None = None,
    ) -> list[SegmentValue]:
        require_as_of(as_of)
        with as_of_session(self._conn, as_of):
            rows = self._conn.execute(
                "SELECT entity_id, period, period_end, dimension, segment_name, revenue,"
                " gross_profit, revenue_share, currency, source, known_at, snapshot_id,"
                " known_at_basis FROM asof.segment_revenue WHERE entity_id = %s"
                " AND (%s::text IS NULL OR period = %s) AND (%s::text IS NULL OR dimension = %s)"
                " ORDER BY period_end, dimension, revenue DESC",
                (entity_id, period, period, dimension, dimension),
            ).fetchall()

        def num(x: Any) -> Decimal | None:  # noqa: ANN401
            return None if x is None else Decimal(x)

        return [
            SegmentValue(
                r[0],
                r[1],
                r[2],
                r[3],
                r[4],
                Decimal(r[5]),
                num(r[6]),
                num(r[7]),
                r[8],
                RecordCitation(r[9], r[10], r[11], r[12]),
            )
            for r in rows
        ]

    def get_fx_rate(
        self, base_ccy: str, quote_ccy: str, *, on: date, as_of: datetime
    ) -> FxQuote | None:
        require_as_of(as_of)
        with as_of_session(self._conn, as_of):
            row = self._conn.execute(
                "SELECT base_ccy, quote_ccy, rate_date, rate, source, known_at, snapshot_id"
                " FROM asof.fx_rate WHERE base_ccy = %s AND quote_ccy = %s AND rate_date <= %s"
                " ORDER BY rate_date DESC LIMIT 1",
                (base_ccy, quote_ccy, on),
            ).fetchone()
        if row is None or (on - row[2]).days > _MAX_FX_STALE_DAYS:
            return None
        return FxQuote(
            row[0], row[1], row[2], Decimal(row[3]), RecordCitation(row[4], row[5], row[6])
        )
