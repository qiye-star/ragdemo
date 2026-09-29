"""把 `sample.SAMPLE` 种进一个已迁移的数据库，供 `PgDataApi` 跑同一套契约测试。

契约模块文档的约定：真实实现把同一份数据集种进临时库再构造，与 Mock 跑同一套断言，
行为才等价。这里用直接 INSERT（显式主键 `OVERRIDING SYSTEM VALUE`，让样本里的
`doc_id` / `block_id` / `snapshot_id` / `rule_id` 与断言一致），不走写入中间件——
写入中间件有自己的测试；这里要的是"库里就是这份数据"。

只用于测试与本地演示，不要对生产库调用（会插入示意数据）。
"""

from __future__ import annotations

import psycopg

from ragdemo.dataapi.sample import (
    BASELINE,
    SAMPLE,
    SampleDataset,
    SampleDocument,
)
from ragdemo.embed.mock import MockEmbedder

_RUN = "sample-seed"


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def seed_sample(conn: psycopg.Connection, dataset: SampleDataset = SAMPLE) -> None:
    """插入数据集并提交。库必须已跑完全部迁移，且是空库（主键显式指定，重复种会冲突）。"""
    _snapshots(conn, dataset)
    _entities(conn, dataset)
    _metrics(conn, dataset)
    _facts(conn, dataset)
    _prices(conn, dataset)
    _documents_and_blocks(conn, dataset)
    _relations_and_rules(conn, dataset)
    _announcements(conn, dataset)
    _structured(conn, dataset)
    conn.commit()


def _snapshots(conn: psycopg.Connection, d: SampleDataset) -> None:
    ids = {f.snapshot_id for f in d.facts}
    ids |= {h.snapshot_id for h in d.holdings if h.snapshot_id is not None}
    ids |= {s.snapshot_id for s in d.segments if s.snapshot_id is not None}
    ids |= {f.snapshot_id for f in d.fx_rates if f.snapshot_id is not None}
    for snapshot_id in sorted(ids):
        conn.execute(
            "INSERT INTO core.provider_snapshot (snapshot_id, provider, endpoint, params, response,"
            " ingest_run_id, fetched_at) OVERRIDING SYSTEM VALUE"
            " VALUES (%s, 'sample', 'seed', '{}'::jsonb, '{}'::jsonb, %s, now())",
            (snapshot_id, _RUN),
        )


def _entities(conn: psycopg.Connection, d: SampleDataset) -> None:
    for e in d.entities:
        conn.execute(
            "INSERT INTO core.entity (entity_id, name_full, name_short, entity_type, market,"
            " tushare_code, l1_layer, l2_segment, l3_node, primary_node) VALUES"
            " (%s, %s, %s, 'listed', %s, %s, '算力', '示意', %s, %s)",
            (
                e.entity_id,
                e.name_full,
                e.name_short,
                e.market,
                e.tushare_code,
                list(e.l3_nodes),
                e.primary_node,
            ),
        )
        for alias in e.aliases:
            conn.execute(
                "INSERT INTO core.entity_alias (alias, entity_id, alias_type, source)"
                " VALUES (%s, %s, 'nickname', 'sample')",
                (alias, e.entity_id),
            )


def _metrics(conn: psycopg.Connection, d: SampleDataset) -> None:
    latest: dict[str, tuple[int, str, str, str, str]] = {}
    for m in d.metrics:
        prev = latest.get(m.metric_id)
        if prev is None or m.version > prev[0]:
            latest[m.metric_id] = (m.version, m.metric_name, m.definition, m.unit, m.frequency)
    for metric_id, (_, name, definition, unit, frequency) in latest.items():
        conn.execute(
            "INSERT INTO core.node_metric (metric_id, metric_name, metric_role, frequency,"
            " source_type, definition, unit) VALUES (%s, %s, 'confirming', %s, 'filing', %s, %s)",
            (metric_id, name, frequency, definition, unit),
        )
    # 迁移 018 已按 core.node_metric 现值生成过 v1，这里的空库没有，所以样本的每一版都补进去。
    conn.execute("DELETE FROM core.node_metric_version")
    for m in d.metrics:
        conn.execute(
            "INSERT INTO core.node_metric_version (metric_id, version, metric_name, metric_role,"
            " frequency, source_type, definition, unit, author, valid_from, known_at,"
            " superseded_at, source, ingest_run_id) VALUES (%s, %s, %s, 'confirming', %s, 'filing',"
            " %s, %s, %s, %s, %s, %s, 'manual:seed', %s)",
            (
                m.metric_id,
                m.version,
                m.metric_name,
                m.frequency,
                m.definition,
                m.unit,
                m.author,
                m.known_at.date() if m.known_at > BASELINE else BASELINE.date(),
                m.known_at,
                m.superseded_at,
                _RUN,
            ),
        )


def _facts(conn: psycopg.Connection, d: SampleDataset) -> None:
    for f in d.facts:
        conn.execute(
            "INSERT INTO core.fin_fact (entity_id, metric_id, period, period_end, value, unit,"
            " currency, valid_from, known_at, superseded_at, source, ingest_run_id, snapshot_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                f.entity_id,
                f.metric_id,
                f.period,
                f.period_end,
                f.value,
                f.unit,
                f.currency,
                f.period_end,
                f.known_at,
                f.superseded_at,
                f.source,
                _RUN,
                f.snapshot_id,
            ),
        )


def _prices(conn: psycopg.Connection, d: SampleDataset) -> None:
    for p in d.prices:
        conn.execute(
            "INSERT INTO core.price_daily (entity_id, trade_date, close, adj_factor, valid_from,"
            " known_at, source, ingest_run_id) VALUES (%s, %s, %s, 1, %s, %s, 'tushare', %s)",
            (p.entity_id, p.trade_date, p.close, p.trade_date, p.known_at, _RUN),
        )


def _insert_document(conn: psycopg.Connection, doc: SampleDocument) -> None:
    conn.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at, source,"
        " source_url, content_hash, version_group_id, page_count, owner_user, can_show_raw,"
        " valid_from, known_at, ingest_run_id) OVERRIDING SYSTEM VALUE"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            doc.doc_id,
            doc.entity_id,
            doc.doc_type,
            doc.title,
            doc.publish_at,
            doc.source,
            doc.source_url,
            f"sample-{doc.doc_id}",
            doc.doc_id,
            doc.page_count,
            doc.owner_user,
            doc.can_show_raw,
            doc.publish_at.date(),
            doc.known_at,
            _RUN,
        ),
    )


def _documents_and_blocks(conn: psycopg.Connection, d: SampleDataset) -> None:
    docs = {doc.doc_id: doc for doc in d.documents}
    # 公告目录的 doc_id 引用的全文文档不在 `documents` 里：补占位文档（有块无内容不需要）。
    for a in d.announcements:
        if a.doc_id is not None and a.doc_id not in docs:
            docs[a.doc_id] = SampleDocument(
                a.doc_id, a.entity_id, "announcement", a.title, a.known_at, a.source, True, None, 1
            )
    for doc in sorted(docs.values(), key=lambda x: x.doc_id):
        _insert_document(conn, doc)

    embedder = MockEmbedder()
    ordinal: dict[int, int] = {}
    for b in sorted(d.blocks, key=lambda x: x.block_id):
        doc = docs[b.doc_id]
        ordinal[b.doc_id] = ordinal.get(b.doc_id, 0) + 1
        vec = _vector_literal(embedder.embed([b.content])[0])
        conn.execute(
            "INSERT INTO core.doc_block (block_id, doc_id, parent_block_id, block_type,"
            " section_path, ordinal, page, bbox, content, content_desc, embedding, is_leaf,"
            " entity_id, doc_type, publish_at, owner_user, can_show_raw, valid_from, known_at,"
            " source, ingest_run_id) OVERRIDING SYSTEM VALUE"
            " VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s,"
            " %s, %s, %s, %s)",
            (
                b.block_id,
                b.doc_id,
                b.block_type,
                b.section_path,
                ordinal[b.doc_id],
                b.page,
                None if b.bbox is None else [float(x) for x in b.bbox],
                b.content,
                b.content_desc,
                vec,
                b.is_leaf,
                doc.entity_id,
                doc.doc_type,
                doc.publish_at,
                doc.owner_user,
                doc.can_show_raw,
                doc.publish_at.date(),
                doc.known_at,
                doc.source,
                _RUN,
            ),
        )
    # 父子关系在全部块插入之后再设置（父块 id 可能比子块大）。
    for b in d.blocks:
        if b.parent_block_id is not None:
            conn.execute(
                "UPDATE core.doc_block SET parent_block_id = %s WHERE block_id = %s",
                (b.parent_block_id, b.block_id),
            )


def _relations_and_rules(conn: psycopg.Connection, d: SampleDataset) -> None:
    for r in d.relations:
        conn.execute(
            "INSERT INTO core.entity_relation (from_entity, to_entity, relation_type, strength,"
            " evidence_block, valid_from, known_at, source, ingest_run_id, confidence)"
            " VALUES (%s, %s, %s::core.relation_type, %s, %s, %s, %s, %s, %s,"
            " %s::core.confidence)",
            (
                r.from_entity,
                r.to_entity,
                r.relation_type,
                r.strength,
                list(r.evidence_blocks),
                r.known_at.date(),
                r.known_at,
                r.source,
                _RUN,
                r.confidence,
            ),
        )
    for s in d.rules:
        rule = s.rule
        conn.execute(
            "INSERT INTO core.propagation_rule (rule_id, trigger_node, trigger_event,"
            " affected_node, direction, lag_days, mechanism, weight_field, confidence,"
            " evidence_block, author, enabled) OVERRIDING SYSTEM VALUE"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::core.confidence, %s, %s, %s)",
            (
                rule.rule_id,
                rule.trigger_node,
                rule.trigger_event,
                rule.affected_node,
                rule.direction,
                rule.lag_days,
                rule.mechanism,
                rule.weight_field,
                rule.confidence,
                list(rule.evidence_blocks),
                rule.author,
                s.enabled,
            ),
        )


def _announcements(conn: psycopg.Connection, d: SampleDataset) -> None:
    for i, a in enumerate(d.announcements, start=1):
        conn.execute(
            "INSERT INTO core.announcement_index (entity_id, source, source_ref, title,"
            " title_norm, publish_date, url, has_full_text, doc_id, valid_from, known_at,"
            " superseded_at, ingest_run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s)",
            (
                a.entity_id,
                a.source,
                f"sample-{i}",
                a.title,
                a.title,
                a.publish_date,
                a.url,
                a.has_full_text,
                a.doc_id,
                a.publish_date,
                a.known_at,
                a.superseded_at,
                _RUN,
            ),
        )


def _structured(conn: psycopg.Connection, d: SampleDataset) -> None:
    for h in d.holdings:
        conn.execute(
            "INSERT INTO core.shareholder_holding (entity_id, period, period_end, rank,"
            " holder_name, shares, ratio, valid_from, known_at, superseded_at, known_at_basis,"
            " source, snapshot_id, ingest_run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s, %s, %s)",
            (
                h.entity_id,
                h.period,
                h.period_end,
                h.rank,
                h.holder_name,
                h.shares,
                h.ratio,
                h.period_end,
                h.known_at,
                h.superseded_at,
                h.known_at_basis,
                h.source,
                h.snapshot_id,
                _RUN,
            ),
        )
    for s in d.segments:
        conn.execute(
            "INSERT INTO core.segment_revenue (entity_id, period, period_end, dimension,"
            " segment_name, revenue, gross_profit, revenue_share, currency, valid_from, known_at,"
            " superseded_at, known_at_basis, source, snapshot_id, ingest_run_id) VALUES (%s, %s,"
            " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                s.entity_id,
                s.period,
                s.period_end,
                s.dimension,
                s.segment_name,
                s.revenue,
                s.gross_profit,
                s.revenue_share,
                s.currency,
                s.period_end,
                s.known_at,
                s.superseded_at,
                s.known_at_basis,
                s.source,
                s.snapshot_id,
                _RUN,
            ),
        )
    for f in d.fx_rates:
        conn.execute(
            "INSERT INTO core.fx_rate (base_ccy, quote_ccy, rate_date, rate, valid_from, known_at,"
            " superseded_at, source, snapshot_id, ingest_run_id) VALUES (%s, %s, %s, %s, %s, %s,"
            " %s, %s, %s, %s)",
            (
                f.base_ccy,
                f.quote_ccy,
                f.rate_date,
                f.rate,
                f.rate_date,
                f.known_at,
                f.superseded_at,
                f.source,
                f.snapshot_id,
                _RUN,
            ),
        )
