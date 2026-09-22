"""文档管线的质量门禁（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 D）。

方案 §2.5 说得最直接的一句是「没有监控的管线等于没有数据」，更关键的是
「失败即阻断下游物化，而非仅告警」——告警会被忽略，阻断不会。这里每个
`阻断=True` 的检查失败时，`block_embeddings`（依赖 `doc_blocks_loaded`）
在同一次 run 里不会被物化：Dagster 的 blocking asset check 语义就是
"这一步失败，下游在本次 run 里不跑"。

七项里六项在这里实现；"每源应到/实到对账"（`doc_normalized` 上的检查）
留给阶段 F——它依赖 `core.ingest_watermark`/供应商"应到条数"这类阶段 F
才会引入的数据，在阶段 D 就做这一项只能编造一个假的"应到"数字，不如
明确留白（计划里 D 和 F 的先后顺序标注为"D 在 E/F 之前"，但这一条具体检查
的数据依赖决定它必须等 F）。

本文件刻意不用 `from __future__ import annotations`：与 `assets_docs.py`
同样的原因——Dagster 在装饰期对 `context` 参数做类型校验时直接比较
`Parameter.annotation`，不解析 PEP 563 的延迟字符串注解。
"""

from datetime import date

import psycopg
from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    ResourceParam,
    asset_check,
)

from ragdemo.ingest.assets_docs import block_embeddings, doc_blocks_loaded
from ragdemo.ingest.partitions import partition_window
from ragdemo.parse.confidence import table_looks_closed
from ragdemo.parse.config import ChunkConfig
from ragdemo.quality.metrics import MetricResult, record_metric

PARSE_SUCCESS_THRESHOLD = 0.98
TABLE_CLOSURE_THRESHOLD = 0.95
PARSE_CONFIDENCE_P50_THRESHOLD = 0.8
LEAF_LENGTH_COMPLIANCE_THRESHOLD = 0.9
VECTOR_COVERAGE_THRESHOLD = 1.0

_DEFAULT_CHUNK_CFG = ChunkConfig()


def _partition_date(context: AssetCheckExecutionContext) -> date:
    return date.fromisoformat(context.partition_key)


@asset_check(asset=doc_blocks_loaded, blocking=True)
def parse_success_rate_check(
    context: AssetCheckExecutionContext, conn: ResourceParam[psycopg.Connection]
) -> AssetCheckResult:
    """一份文档解析失败时，`prepare_documents` 仍然会写一行 document（留个
    记号，见 `assets_docs.py` 的注释），但它不会有任何 `doc_block`——零块
    就是"这份文档解析失败"的结构性信号，不依赖对 `parse_engine` 里各种
    失败 marker 字符串做匹配（那个列表会随失败分支增加而漂移，容易漏）。
    """
    since, until = partition_window(context)
    total, zero_block = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE block_count = 0) FROM ("
        "  SELECT d.doc_id, count(b.block_id) AS block_count"
        "    FROM core.document d LEFT JOIN core.doc_block b ON b.doc_id = d.doc_id"
        "   WHERE d.publish_at > %s AND d.publish_at <= %s"
        "   GROUP BY d.doc_id"
        ") t",
        (since, until),
    ).fetchone()  # type: ignore[misc]
    rate = 1.0 if total == 0 else 1.0 - (zero_block / total)
    passed = rate >= PARSE_SUCCESS_THRESHOLD
    record_metric(
        conn,
        _partition_date(context),
        MetricResult("parse_success_rate", rate, passed, PARSE_SUCCESS_THRESHOLD),
    )
    return AssetCheckResult(
        passed=passed,
        metadata={"rate": rate, "total_documents": total, "zero_block_documents": zero_block},
        severity=AssetCheckSeverity.ERROR,
    )


@asset_check(asset=doc_blocks_loaded, blocking=True)
def table_closure_rate_check(
    context: AssetCheckExecutionContext, conn: ResourceParam[psycopg.Connection]
) -> AssetCheckResult:
    since, until = partition_window(context)
    rows = conn.execute(
        "SELECT b.content FROM core.doc_block b JOIN core.document d USING (doc_id)"
        " WHERE b.block_type = 'table' AND d.publish_at > %s AND d.publish_at <= %s",
        (since, until),
    ).fetchall()
    tables = [str(r[0]) for r in rows]
    # 没有表格时这一项不适用，视为满分——与 parse/confidence.py 的
    # _closure_score 是同一个约定，不能因为文档里没有表格就判定失败。
    rate = 1.0 if not tables else sum(1 for t in tables if table_looks_closed(t)) / len(tables)
    passed = rate >= TABLE_CLOSURE_THRESHOLD
    record_metric(
        conn,
        _partition_date(context),
        MetricResult("table_closure_rate", rate, passed, TABLE_CLOSURE_THRESHOLD),
    )
    return AssetCheckResult(
        passed=passed,
        metadata={"rate": rate, "table_count": len(tables)},
        severity=AssetCheckSeverity.ERROR,
    )


@asset_check(asset=doc_blocks_loaded, blocking=False)
def parse_confidence_p50_check(
    context: AssetCheckExecutionContext, conn: ResourceParam[psycopg.Connection]
) -> AssetCheckResult:
    """告警级别，不阻断：置信度低不代表这批块不能用，只代表值得关注
    （低分块会被阶段 G 的 C 档路由捡走重解析）。"""
    since, until = partition_window(context)
    row = conn.execute(
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY b.parse_confidence)"
        "  FROM core.doc_block b JOIN core.document d USING (doc_id)"
        " WHERE d.publish_at > %s AND d.publish_at <= %s AND b.parse_confidence IS NOT NULL",
        (since, until),
    ).fetchone()
    p50 = row[0] if row and row[0] is not None else None
    # 这批块一个都没打过分（比如全部走了不经过 DocumentWriter 的测试路径），
    # 不该编造一个数字硬判定失败——记 passed=True 但 value=-1 作为"无样本"的
    # 显式标记，而不是让 None 混进只接受数值的 quality_metric.value 列。
    value = float(p50) if p50 is not None else -1.0
    passed = p50 is None or float(p50) >= PARSE_CONFIDENCE_P50_THRESHOLD
    record_metric(
        conn,
        _partition_date(context),
        MetricResult("parse_confidence_p50", value, passed, PARSE_CONFIDENCE_P50_THRESHOLD),
    )
    return AssetCheckResult(
        passed=passed, metadata={"p50": value}, severity=AssetCheckSeverity.WARN
    )


@asset_check(asset=doc_blocks_loaded, blocking=True)
def orphan_block_check(
    context: AssetCheckExecutionContext, conn: ResourceParam[psycopg.Connection]
) -> AssetCheckResult:
    """防御性复核：`parse/validate.py` 的 `validate_chunks` 已经在写入前
    拒绝过这类违规（整份文档回滚），这里再查一遍是为了在"validate_chunks
    被绕过"（比如未来某个新写入路径忘了调它）时仍然能拦住，而不是假设
    它永远被正确调用。"""
    since, until = partition_window(context)
    (orphans,) = conn.execute(
        "SELECT count(*) FROM core.doc_block b JOIN core.document d USING (doc_id)"
        " WHERE d.publish_at > %s AND d.publish_at <= %s"
        "   AND ("
        "     (b.is_leaf = false AND NOT EXISTS ("
        "        SELECT 1 FROM core.doc_block c WHERE c.parent_block_id = b.block_id))"
        "     OR (b.is_leaf = true AND b.block_type != 'table' AND b.parent_block_id IS NULL)"
        "   )",
        (since, until),
    ).fetchone()  # type: ignore[misc]
    passed = orphans == 0
    record_metric(
        conn,
        _partition_date(context),
        MetricResult("orphan_block_count", float(orphans), passed, 0.0),
    )
    return AssetCheckResult(
        passed=passed, metadata={"orphan_count": orphans}, severity=AssetCheckSeverity.ERROR
    )


@asset_check(asset=doc_blocks_loaded, blocking=False)
def leaf_length_compliance_check(
    context: AssetCheckExecutionContext, conn: ResourceParam[psycopg.Connection]
) -> AssetCheckResult:
    """告警级别：块长偏离区间不影响正确性，只影响检索信号质量
    （方案 §2.3：短于下限切断因果链，长于上限稀释向量信号）。"""
    since, until = partition_window(context)
    total, in_range = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE b.char_len BETWEEN %s AND %s)"
        "  FROM core.doc_block b JOIN core.document d USING (doc_id)"
        " WHERE d.publish_at > %s AND d.publish_at <= %s"
        "   AND b.is_leaf AND b.block_type = 'paragraph'",
        (
            _DEFAULT_CHUNK_CFG.leaf_min_chars,
            _DEFAULT_CHUNK_CFG.leaf_max_chars,
            since,
            until,
        ),
    ).fetchone()  # type: ignore[misc]
    rate = 1.0 if total == 0 else in_range / total
    passed = rate >= LEAF_LENGTH_COMPLIANCE_THRESHOLD
    record_metric(
        conn,
        _partition_date(context),
        MetricResult("leaf_length_compliance_rate", rate, passed, LEAF_LENGTH_COMPLIANCE_THRESHOLD),
    )
    return AssetCheckResult(
        passed=passed,
        metadata={"rate": rate, "paragraph_leaf_count": total},
        severity=AssetCheckSeverity.WARN,
    )


@asset_check(asset=block_embeddings, blocking=True)
def vector_coverage_check(
    context: AssetCheckExecutionContext, conn: ResourceParam[psycopg.Connection]
) -> AssetCheckResult:
    since, until = partition_window(context)
    total, embedded = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE b.embedding IS NOT NULL)"
        "  FROM core.doc_block b JOIN core.document d USING (doc_id)"
        " WHERE d.publish_at > %s AND d.publish_at <= %s AND b.is_leaf",
        (since, until),
    ).fetchone()  # type: ignore[misc]
    rate = 1.0 if total == 0 else embedded / total
    passed = rate >= VECTOR_COVERAGE_THRESHOLD
    record_metric(
        conn,
        _partition_date(context),
        MetricResult("vector_coverage_rate", rate, passed, VECTOR_COVERAGE_THRESHOLD),
    )
    return AssetCheckResult(
        passed=passed,
        metadata={"rate": rate, "leaf_block_count": total, "embedded_count": embedded},
        severity=AssetCheckSeverity.ERROR,
    )


ALL_CHECKS = (
    parse_success_rate_check,
    table_closure_rate_check,
    parse_confidence_p50_check,
    orphan_block_check,
    leaf_length_compliance_check,
    vector_coverage_check,
)
