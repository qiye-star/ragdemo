"""文档管线的质量门禁（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 D）。

方案 §2.5 说得最直接的一句是「没有监控的管线等于没有数据」，更关键的是
「失败即阻断下游物化，而非仅告警」——告警会被忽略，阻断不会。这里每个
`阻断=True` 的检查失败时，`block_embeddings`（依赖 `doc_blocks_loaded`）
在同一次 run 里不会被物化：Dagster 的 blocking asset check 语义就是
"这一步失败，下游在本次 run 里不跑"。

八项里六项在这里实现；第七、八项由阶段 F 补上——"每源应到/实到对账"
（`ingest_reconciliation_check`）与"接入延迟"（`ingest_latency_check`），
都依赖 `core.ingest_watermark`/`quality_metric.fetched_count` 这类阶段 F
才会引入的数据，在阶段 D 就做这两项只能编造假数字，不如明确留白到 F
（计划里 D 和 F 的先后顺序标注为"D 在 E/F 之前"，但这两条具体检查的数据
依赖决定它们必须等 F）。

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

from ragdemo.ingest.assets_docs import block_embeddings, doc_blocks_loaded, doc_normalized
from ragdemo.ingest.partitions import partition_window
from ragdemo.ingest.reconcile import compute_ingest_latency, reconcile_counts
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


_RECONCILE_SOURCE_ID = "mock-announcements"  # 与 document_writer_resource 同一个来源


@asset_check(asset=doc_blocks_loaded, blocking=True)
def ingest_reconciliation_check(
    context: AssetCheckExecutionContext, conn: ResourceParam[psycopg.Connection]
) -> AssetCheckResult:
    """第七项质量门禁：应到（`doc_normalized` 拉到的文档数）vs 实到
    （这个分区窗口内真正落进 `core.document` 的行数）——阶段 D 留白到这里
    的那一项（见模块 docstring）。

    两个数字都只读 Postgres，不消费任何上游资产的 Python 返回值——与其余
    七项检查完全同构。最初的版本让这个检查挂在 `doc_prepared` 上，直接把
    `doc_prepared: list[PreparedDocument]` 当函数参数（读上游资产的物化
    结果），在真实 Dagster 物化时稳定复现同一个错误：不管是自动按参数名
    注入被检查资产本身，还是用 `additional_ins=AssetIn(partition_mapping=
    IdentityPartitionMapping())` 显式声明额外的上游依赖，Dagster 都会去
    加载 partitions_def 里"哪个分区排第一"（这里是 2022-01-01）而不是这次
    实际物化的分区，那个分区从未跑过，直接 FileNotFoundError 崩溃——本
    仓库其余七项检查没有一个消费过被检查资产或其上游的 Python 返回值，
    全部只读 Postgres，这不是偶然，是同一个坑的前车之鉴。改成落在
    `doc_blocks_loaded` 上、"实到"也现查 `core.document`，问题消失。

    `prepare_documents`（ingest/assets_docs.py）有两条会让"实到"少于"应到"
    的分支，都是刻意设计成"这次先跳过、留给下次分区重跑"，不是数据丢失：
    `PageBudget` 耗尽时提前 break；单份文档遇到 `ParseRetryable`（供应商侧
    临时故障）时 continue、不留任何记号。两者都会在下一次同分区重跑时自然
    补齐（前者见 `MAX_PAGES_PER_PREPARE_RUN` 的注释，后者见
    `prepare_documents` 对应分支的注释），因此这里给出的 note 不是猜测，
    是这两条分支的既有约定——差异不代表数据永久丢失，只代表"还没轮到"。
    """
    row = conn.execute(
        "SELECT value FROM quality.quality_metric"
        " WHERE metric = 'fetched_count' AND source_id = %s AND partition_date = %s"
        " ORDER BY computed_at DESC LIMIT 1",
        (_RECONCILE_SOURCE_ID, _partition_date(context)),
    ).fetchone()
    # doc_normalized 还没为这个分区跑过（比如这个检查被单独物化）——没有
    # "应到"可比对，不该编造一个数字，直接跳过对账，不是判定失败。
    if row is None:
        return AssetCheckResult(passed=True, metadata={"skipped": "no fetched_count yet"})
    expected = int(row[0])
    since, until = partition_window(context)
    (actual,) = conn.execute(
        "SELECT count(*) FROM core.document"
        " WHERE source = %s AND publish_at > %s AND publish_at <= %s",
        (_RECONCILE_SOURCE_ID, since, until),
    ).fetchone()  # type: ignore[misc]
    note = (
        None
        if actual == expected
        else "prepare_documents 未覆盖全部输入（PageBudget 耗尽或遇到可重试解析"
        "失败），差额会在下次同分区重跑时自然补齐，不是数据丢失"
    )
    result = reconcile_counts(
        conn,
        _partition_date(context),
        _RECONCILE_SOURCE_ID,
        expected=expected,
        actual=actual,
        note=note,
    )
    return AssetCheckResult(
        passed=result.passed,
        metadata={"expected": expected, "actual": actual, "diff": actual - expected},
        severity=AssetCheckSeverity.ERROR,
    )


ANNOUNCEMENT_LATENCY_P95_THRESHOLD_MINUTES = 15.0  # 方案 §2.2：公告源合理，其余源不套用这个数字


@asset_check(asset=doc_normalized, blocking=False)
def ingest_latency_check(
    context: AssetCheckExecutionContext, conn: ResourceParam[psycopg.Connection]
) -> AssetCheckResult:
    """接入延迟（F6）：只给已知会接近实时披露的公告源（`mock-announcements`）
    设 15 分钟的 P95 阻断阈值——告警级别，不阻断下游物化，因为延迟高不代表
    这批数据不能用，只代表值得关注（与 `parse_confidence_p50_check` 同一个
    严重级别选择理由）。EDGAR 这类日频源不经这个检查：它的"新鲜度"概念
    与公告完全不同，套用同一个阈值只会让指标永远红着然后被忽略（阶段 F
    引言原话）——真正需要按源分别设阈值时，直接调 `compute_ingest_latency`
    并传各自的 `p95_threshold_minutes`，不在这一个检查函数里堆条件分支。
    """
    since, until = partition_window(context)
    result = compute_ingest_latency(
        conn,
        _partition_date(context),
        _RECONCILE_SOURCE_ID,
        since,
        until,
        p95_threshold_minutes=ANNOUNCEMENT_LATENCY_P95_THRESHOLD_MINUTES,
    )
    return AssetCheckResult(
        passed=result.passed,
        metadata={
            "p95_minutes": result.value,
            "threshold_minutes": ANNOUNCEMENT_LATENCY_P95_THRESHOLD_MINUTES,
        },
        severity=AssetCheckSeverity.WARN,
    )


ALL_CHECKS = (
    parse_success_rate_check,
    table_closure_rate_check,
    parse_confidence_p50_check,
    orphan_block_check,
    leaf_length_compliance_check,
    vector_coverage_check,
    ingest_reconciliation_check,
    ingest_latency_check,
)
