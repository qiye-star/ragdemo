"""文档管线质量门禁：八项检查各自的通过/失败场景 + quality.dashboard 视图。

阻断（blocking=True）语义本身——检查失败时 block_embeddings 在同一次 run 里
不被物化——是 Dagster 的运行时行为，单元测试直接调用检查函数验证不到；
那一层由手工跑通真实 Dagster 管线验证过（见阶段 D 的验收记录），这里只测
每个检查函数自己的判定逻辑与 quality_metric 写入是否正确。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from dagster import AssetCheckSeverity, build_op_context
from dagster._core.execution.context.invocation import DirectAssetCheckExecutionContext

from ragdemo.quality.checks import (
    ingest_latency_check,
    ingest_reconciliation_check,
    leaf_length_compliance_check,
    orphan_block_check,
    parse_confidence_p50_check,
    parse_success_rate_check,
    table_closure_rate_check,
    vector_coverage_check,
)
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
PARTITION_KEY = "2024-10-28"
PUBLISH_AT = "2024-10-28 10:00:00+00"  # 落在该分区 (since, until] 窗口内


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    c.execute(
        "INSERT INTO core.source_registry (source_id, vendor, layer, can_cache,"
        " can_show_raw, can_vectorize, time_precision) "
        "VALUES ('mock','mock','filing',true,true,true,'second')"
    )
    c.commit()
    return c


def _check_ctx() -> DirectAssetCheckExecutionContext:
    """不在这里绑定 `conn` 资源——直接调用检查函数时，`conn` 作为额外的
    位置参数传入（与 `tests/ingest/test_assets_docs.py` 里 `@asset` 函数的
    直调方式一致）；context 自己再绑一份会撞上 Dagster 的
    "Cannot provide resources in both context and kwargs"。"""
    op_ctx = build_op_context(partition_key=PARTITION_KEY)
    return DirectAssetCheckExecutionContext(op_execution_context=op_ctx)


def _seed_document(conn: psycopg.Connection, *, doc_id: int, content_hash: str) -> None:
    conn.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at,"
        " source, content_hash, version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (%s,'CN.688256','quarterly','t',%s,"
        " 'mock',%s,%s,'2024-07-01',%s,'r1')",
        (doc_id, PUBLISH_AT, content_hash, doc_id, PUBLISH_AT),
    )


def _seed_block(
    conn: psycopg.Connection,
    *,
    doc_id: int,
    ordinal: int,
    block_type: str = "paragraph",
    content: str = "内容",
    is_leaf: bool = True,
    parent_block_id: int | None = None,
    char_len: int | None = None,
    parse_confidence: float | None = None,
    embedding: str | None = None,
) -> int:
    row = conn.execute(
        "INSERT INTO core.doc_block (doc_id, parent_block_id, block_type, section_path,"
        " ordinal, content, char_len, is_leaf, entity_id, doc_type, publish_at, valid_from,"
        " known_at, source, ingest_run_id, parse_confidence, embedding) "
        "VALUES (%s,%s,%s,'',%s,%s,%s,%s,'CN.688256','quarterly',%s,'2024-07-01',%s,"
        "'mock','r1',%s,%s) RETURNING block_id",
        (
            doc_id,
            parent_block_id,
            block_type,
            ordinal,
            content,
            char_len if char_len is not None else len(content),
            is_leaf,
            PUBLISH_AT,
            PUBLISH_AT,
            parse_confidence,
            embedding,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


# --- parse_success_rate_check -------------------------------------------------


@pytest.mark.db
def test_parse_success_rate_passes_when_every_document_has_blocks(
    conn: psycopg.Connection,
) -> None:
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0)
    conn.commit()

    result = parse_success_rate_check(_check_ctx(), conn)
    assert result.passed is True

    (value, passed) = conn.execute(
        "SELECT value, passed FROM quality.quality_metric WHERE metric = 'parse_success_rate'"
    ).fetchone()  # type: ignore[misc]
    assert float(value) == 1.0
    assert passed is True


@pytest.mark.db
def test_parse_success_rate_fails_when_a_document_has_zero_blocks(
    conn: psycopg.Connection,
) -> None:
    """解析失败的文档仍然会写一行 document（留个记号），但没有任何 doc_block
    ——零块就是失败信号，不依赖对 parse_engine 字符串做匹配。"""
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0)
    _seed_document(conn, doc_id=2, content_hash="h2")  # 解析失败：没有块
    conn.commit()

    result = parse_success_rate_check(_check_ctx(), conn)
    assert result.passed is False  # 1/2 = 0.5 < 0.98


@pytest.mark.db
def test_parse_success_rate_vacuously_passes_with_no_documents(conn: psycopg.Connection) -> None:
    result = parse_success_rate_check(_check_ctx(), conn)
    assert result.passed is True


# --- table_closure_rate_check --------------------------------------------------


@pytest.mark.db
def test_table_closure_rate_fails_on_an_unclosed_table(conn: psycopg.Connection) -> None:
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(
        conn,
        doc_id=1,
        ordinal=0,
        block_type="table",
        content="| 甲 | 乙 |\n|---|---|\n| 1 |  |",  # 一个空单元格：未闭合
    )
    conn.commit()

    result = table_closure_rate_check(_check_ctx(), conn)
    assert result.passed is False


@pytest.mark.db
def test_table_closure_rate_passes_with_no_tables(conn: psycopg.Connection) -> None:
    """没有表格时这一项不适用，视为满分——不能因为文档里没有表格就判定失败。"""
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0, block_type="paragraph")
    conn.commit()

    result = table_closure_rate_check(_check_ctx(), conn)
    assert result.passed is True


# --- parse_confidence_p50_check（告警，不阻断） --------------------------------


@pytest.mark.db
def test_parse_confidence_p50_fails_when_median_is_low(conn: psycopg.Connection) -> None:
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0, parse_confidence=0.3)
    _seed_block(conn, doc_id=1, ordinal=1, parse_confidence=0.4)
    conn.commit()

    result = parse_confidence_p50_check(_check_ctx(), conn)
    assert result.passed is False


@pytest.mark.db
def test_parse_confidence_p50_passes_with_no_scored_blocks(conn: psycopg.Connection) -> None:
    """一个块都没打过分（比如全部走了不经过 DocumentWriter 的写入路径）不该
    被硬判定失败——value=-1 是"无样本"的显式标记。"""
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0, parse_confidence=None)
    conn.commit()

    result = parse_confidence_p50_check(_check_ctx(), conn)
    assert result.passed is True
    (value,) = conn.execute(
        "SELECT value FROM quality.quality_metric WHERE metric = 'parse_confidence_p50'"
    ).fetchone()  # type: ignore[misc]
    assert float(value) == -1.0


# --- orphan_block_check --------------------------------------------------------


@pytest.mark.db
def test_orphan_block_check_fails_on_a_leaf_paragraph_without_a_parent(
    conn: psycopg.Connection,
) -> None:
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0, is_leaf=True, parent_block_id=None)
    conn.commit()

    result = orphan_block_check(_check_ctx(), conn)
    assert result.passed is False


@pytest.mark.db
def test_orphan_block_check_allows_a_leaf_table_without_a_parent(
    conn: psycopg.Connection,
) -> None:
    """表格块既是叶子也没有父块——这是合法状态，不是孤儿（05 §4.2 规则 3）。"""
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0, block_type="table", is_leaf=True, parent_block_id=None)
    conn.commit()

    result = orphan_block_check(_check_ctx(), conn)
    assert result.passed is True


@pytest.mark.db
def test_orphan_block_check_fails_on_a_parent_block_with_no_children(
    conn: psycopg.Connection,
) -> None:
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0, is_leaf=False)
    conn.commit()

    result = orphan_block_check(_check_ctx(), conn)
    assert result.passed is False


@pytest.mark.db
def test_orphan_block_check_passes_for_a_well_formed_parent_child_pair(
    conn: psycopg.Connection,
) -> None:
    _seed_document(conn, doc_id=1, content_hash="h1")
    parent_id = _seed_block(conn, doc_id=1, ordinal=0, is_leaf=False)
    _seed_block(conn, doc_id=1, ordinal=1, is_leaf=True, parent_block_id=parent_id)
    conn.commit()

    result = orphan_block_check(_check_ctx(), conn)
    assert result.passed is True


# --- leaf_length_compliance_check（告警，不阻断） ------------------------------


@pytest.mark.db
def test_leaf_length_compliance_fails_when_most_leaves_are_too_short(
    conn: psycopg.Connection,
) -> None:
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0, char_len=300)  # 合规
    _seed_block(conn, doc_id=1, ordinal=1, char_len=10)  # 太短
    _seed_block(conn, doc_id=1, ordinal=2, char_len=5)  # 太短
    conn.commit()

    result = leaf_length_compliance_check(_check_ctx(), conn)
    assert result.passed is False  # 1/3 ≈ 0.33 < 0.9


@pytest.mark.db
def test_leaf_length_compliance_ignores_tables_and_parents(conn: psycopg.Connection) -> None:
    """只统计叶子段落块——表格块长度不受 200-400 约束，父块不供检索。"""
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0, char_len=300)
    _seed_block(conn, doc_id=1, ordinal=1, block_type="table", char_len=5)
    _seed_block(conn, doc_id=1, ordinal=2, is_leaf=False, char_len=2000)
    conn.commit()

    result = leaf_length_compliance_check(_check_ctx(), conn)
    assert result.passed is True


# --- vector_coverage_check ------------------------------------------------------


@pytest.mark.db
def test_vector_coverage_fails_when_a_leaf_block_has_no_embedding(
    conn: psycopg.Connection,
) -> None:
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0, embedding="[" + ",".join(["0.1"] * 1024) + "]")
    _seed_block(conn, doc_id=1, ordinal=1, embedding=None)
    conn.commit()

    result = vector_coverage_check(_check_ctx(), conn)
    assert result.passed is False


@pytest.mark.db
def test_vector_coverage_passes_when_all_leaves_are_embedded(conn: psycopg.Connection) -> None:
    _seed_document(conn, doc_id=1, content_hash="h1")
    vec = "[" + ",".join(["0.1"] * 1024) + "]"
    _seed_block(conn, doc_id=1, ordinal=0, embedding=vec)
    _seed_block(conn, doc_id=1, ordinal=1, embedding=vec)
    conn.commit()

    result = vector_coverage_check(_check_ctx(), conn)
    assert result.passed is True


@pytest.mark.db
def test_vector_coverage_ignores_parent_blocks(conn: psycopg.Connection) -> None:
    """父块永远不被嵌入——不该因为父块没有向量就判定覆盖率不足。"""
    _seed_document(conn, doc_id=1, content_hash="h1")
    vec = "[" + ",".join(["0.1"] * 1024) + "]"
    _seed_block(conn, doc_id=1, ordinal=0, is_leaf=True, embedding=vec)
    _seed_block(conn, doc_id=1, ordinal=1, is_leaf=False, embedding=None)
    conn.commit()

    result = vector_coverage_check(_check_ctx(), conn)
    assert result.passed is True


# --- quality.dashboard 视图 -----------------------------------------------------


@pytest.mark.db
def test_dashboard_shows_the_latest_value_when_a_metric_is_recomputed(
    conn: psycopg.Connection,
) -> None:
    """同一个 (metric, partition_date) 因为 run 重试被写入多次时，看板只
    展示最新的一次，不是历史全量——历史值仍然留在 quality_metric 里。"""
    _seed_document(conn, doc_id=1, content_hash="h1")
    _seed_block(conn, doc_id=1, ordinal=0)
    conn.commit()

    parse_success_rate_check(_check_ctx(), conn)  # 第一次：1/1 通过
    _seed_document(conn, doc_id=2, content_hash="h2")  # 追加一份解析失败的文档
    conn.commit()
    parse_success_rate_check(_check_ctx(), conn)  # 第二次：1/2 不通过

    rows = conn.execute(
        "SELECT value, passed FROM quality.dashboard WHERE metric = 'parse_success_rate'"
    ).fetchall()
    assert len(rows) == 1, "看板每个 metric 只应该有一行——最新的那次"
    assert float(rows[0][0]) == 0.5
    assert rows[0][1] is False

    (history_count,) = conn.execute(
        "SELECT count(*) FROM quality.quality_metric WHERE metric = 'parse_success_rate'"
    ).fetchone()  # type: ignore[misc]
    assert history_count == 2, "历史两次都应该保留在 quality_metric 里"


# --- ingest_reconciliation_check（阶段 F，第七项）---------------------------


def _seed_fetched_count(conn: psycopg.Connection, count: int) -> None:
    """模拟 doc_normalized 已经为这个分区记过"应到"——见 assets_docs.py 里
    `doc_normalized` 写 `fetched_count` 那一步。"""
    conn.execute(
        "INSERT INTO quality.quality_metric (metric, source_id, partition_date, value, passed) "
        "VALUES ('fetched_count', 'mock-announcements', %s, %s, true)",
        (date.fromisoformat(PARTITION_KEY), float(count)),
    )
    conn.commit()


def _seed_reconcile_document(conn: psycopg.Connection, *, doc_id: int) -> None:
    """"实到"：一行落进 `core.document`，`source='mock-announcements'`、
    `publish_at` 落在这个分区的 (since, until] 窗口内——与 `_seed_fetched_
    count` 记的"应到"比对的就是这张表这个窗口内的行数，不是任何上游资产的
    Python 返回值。"""
    conn.execute(
        "INSERT INTO core.document (doc_id, doc_type, title, publish_at, source,"
        " content_hash, version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (%s,'announcement','t',%s,'mock-announcements',"
        "%s,%s,'2024-10-28',%s,'r1')",
        (doc_id, PUBLISH_AT, f"rc-hash-{doc_id}", doc_id, PUBLISH_AT),
    )
    conn.commit()


@pytest.mark.db
def test_reconciliation_skips_when_fetched_count_was_never_recorded(
    conn: psycopg.Connection,
) -> None:
    """doc_normalized 还没为这个分区跑过——没有"应到"可比对，不该编造一个
    数字，直接跳过，不是判定失败。"""
    result = ingest_reconciliation_check(_check_ctx(), conn)

    assert result.passed is True


@pytest.mark.db
def test_reconciliation_passes_with_no_note_when_counts_match(conn: psycopg.Connection) -> None:
    _seed_fetched_count(conn, 2)
    _seed_reconcile_document(conn, doc_id=1)
    _seed_reconcile_document(conn, doc_id=2)

    result = ingest_reconciliation_check(_check_ctx(), conn)

    assert result.passed is True
    (note,) = conn.execute(
        "SELECT note FROM quality.quality_metric WHERE metric = 'reconcile_diff'"
    ).fetchone()  # type: ignore[misc]
    assert note is None


@pytest.mark.db
def test_reconciliation_passes_with_an_attributed_note_when_a_document_did_not_land(
    conn: psycopg.Connection,
) -> None:
    """budget 耗尽或 ParseRetryable 都会让落进 core.document 的行数少于
    doc_normalized 报的"应到"——这是已知、会在下次重跑时自然补齐的情况，
    check 仍然通过，但必须留下归因。"""
    _seed_fetched_count(conn, 2)
    _seed_reconcile_document(conn, doc_id=1)  # 只有一份真正落库

    result = ingest_reconciliation_check(_check_ctx(), conn)

    assert result.passed is True
    assert result.metadata is not None
    assert result.metadata["diff"].value == -1  # type: ignore[union-attr]
    (note,) = conn.execute(
        "SELECT note FROM quality.quality_metric WHERE metric = 'reconcile_diff'"
    ).fetchone()  # type: ignore[misc]
    assert note is not None


# --- ingest_latency_check（F6）------------------------------------------------


def _seed_document_with_latency(
    conn: psycopg.Connection, *, doc_id: int, source: str, delay_minutes: float
) -> None:
    publish_at = datetime(2024, 10, 28, 10, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO core.document (doc_id, doc_type, title, publish_at, source,"
        " content_hash, version_group_id, valid_from, known_at, ingest_run_id, ingested_at) "
        "OVERRIDING SYSTEM VALUE VALUES (%s,'announcement','t',%s,%s,%s,%s,%s,%s,'r1',%s)",
        (
            doc_id,
            publish_at,
            source,
            f"h{doc_id}",
            doc_id,
            publish_at.date(),
            publish_at,
            publish_at + timedelta(minutes=delay_minutes),
        ),
    )
    conn.commit()


@pytest.mark.db
def test_ingest_latency_check_warns_but_does_not_block_when_slow(
    conn: psycopg.Connection,
) -> None:
    _seed_document_with_latency(conn, doc_id=1, source="mock-announcements", delay_minutes=30)

    result = ingest_latency_check(_check_ctx(), conn)

    assert result.passed is False
    assert result.severity == AssetCheckSeverity.WARN


@pytest.mark.db
def test_ingest_latency_check_passes_within_threshold(conn: psycopg.Connection) -> None:
    _seed_document_with_latency(conn, doc_id=1, source="mock-announcements", delay_minutes=5)

    result = ingest_latency_check(_check_ctx(), conn)

    assert result.passed is True
