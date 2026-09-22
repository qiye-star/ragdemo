"""H7：改坏 ChunkConfig 后，评测门禁真的能拦住——不是靠猜测/文档描述认定。

具体机制：`leaf_max_chars` 改小会让 `doc_prepared`/`docs rechunk` 之后的
重新切块产出全新的 block_id（`DocumentWriter.reparse_document` 把旧块标
`superseded_at`，插入新块），而评测集里已经标注好的 `gold_block_ids`
绑的是旧 block_id——`asof.doc_block` 只能看见"活着"的块，重新切块之后旧
block_id 全部从可见集合里消失，`recall@10` 因此从满分直接归零，而不是
"稍微下降一点"。这正是方案 §2.2 强调"切块策略是需要跑评测的改动"的根本
原因：不是"新参数切得更差"这种渐进式质量问题，是"旧标注集直接失效"这种
结构性断裂，理应是最刺眼的红灯。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.mock import MockEmbedder
from ragdemo.evals.cli import fetch_baseline, passes_gate
from ragdemo.evals.runner import run_retrieval_eval
from ragdemo.ingest.documents import DocumentWriter
from ragdemo.parse.chunker import chunk_document
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.tree import build_tree
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

_TITLE = "寒武纪 2024 年三季报"
_PARAGRAPH = (
    "报告期内公司智能计算集群系统业务实现营业收入 12,340 万元，同比增长 58.2%，"
    "主要系云端训练芯片出货量提升所致，公司持续加大下一代训练芯片的研发投入。"
)


def _doc(entity_ref: str = "688256.SH") -> object:
    from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument

    return NormalizedDocument(
        provider_doc_id="Q3-2024",
        entity_ref=entity_ref,
        doc_type="quarterly",
        title=_TITLE,
        period="2024Q3",
        publish_at=datetime(2024, 10, 28, 18, 32, tzinfo=UTC),
        language="zh",
        source_url=None,
        raw_bytes_ref=None,
        content_hash="chunk-gate-h7",
        is_correction=False,
        supersedes_provider_doc_id=None,
        page_count=1,
        blocks=[NormalizedBlock(0, "paragraph", "", _PARAGRAPH, page=1)],
    )


def _write_with_config(
    writer: DocumentWriter, cfg: ChunkConfig, *, supersedes_doc_id: int | None = None
) -> int:
    doc = _doc()
    chunks = build_tree(chunk_document(doc, cfg))  # type: ignore[arg-type]
    describer = MockTableDescriber()
    descriptions = {
        c.ordinal: describer.describe(c.content, title=_TITLE, section_path=c.section_path)
        for c in chunks
        if c.block_type == "table"
    }
    if supersedes_doc_id is None:
        result = writer.write_document(doc, chunks, descriptions, chunking_version=cfg.version)  # type: ignore[arg-type]
    else:
        result = writer.reparse_document(
            doc,  # type: ignore[arg-type]
            chunks,
            descriptions,
            supersedes_doc_id=supersedes_doc_id,
            chunking_version=cfg.version,
        )
    return result.doc_id


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
    # mock-announcements 已经在 009_source_registry.sql 里登记，不需要重复插入。
    c.commit()
    return c


@pytest.mark.db
def test_shrinking_leaf_max_chars_breaks_the_existing_gold_block_ids(
    conn: psycopg.Connection,
) -> None:
    # 必须晚于这个测试里全部写入操作的 superseded_at（= 数据库 now()），
    # 这样重新切块之后旧块才会在这个 as_of 下变得不可见——固定的日历日期
    # 做不到这一点：如果它早于重新切块发生的真实时刻，旧块的 superseded_at
    # 会晚于这个 as_of，按 bitemporal 语义（该时点看到的内容不因未来的
    # 更正而改变）反而仍然可见，这不是 bug，是本来就该有的行为。
    eval_as_of = datetime.now(UTC) + timedelta(hours=1)
    writer = DocumentWriter(conn, ingest_run_id="r1", source="mock-announcements")

    default_cfg = ChunkConfig()
    doc_id = _write_with_config(writer, default_cfg)
    embed_pending_blocks(conn, MockEmbedder())
    conn.commit()

    (gold_block_id,) = conn.execute(
        "SELECT block_id FROM core.doc_block WHERE doc_id = %s AND is_leaf", (doc_id,)
    ).fetchone()  # type: ignore[misc]
    conn.execute(
        "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids, entity_filter,"
        " difficulty, author) VALUES (%s,%s,%s,%s,'easy','test')",
        ("寒武纪三季度营业收入是多少？", eval_as_of, [int(gold_block_id)], ["CN.688256"]),
    )
    conn.commit()

    service = RetrievalService(conn, MockEmbedder(), MockReranker())
    baseline = run_retrieval_eval(
        conn, service, git_sha="base", config=RetrievalConfig(rerank_enabled=False)
    )
    assert baseline.recall_at_10 == pytest.approx(1.0)

    # 模拟"改坏 ChunkConfig"：leaf_max_chars 从默认的 200-400 砍到 50。
    bad_cfg = ChunkConfig(leaf_min_chars=20, leaf_max_chars=50, overlap_ratio=0.1)
    _write_with_config(writer, bad_cfg, supersedes_doc_id=doc_id)
    embed_pending_blocks(conn, MockEmbedder())
    conn.commit()

    with conn.transaction():
        conn.execute("SELECT set_config('app.as_of', %s, true)", (eval_as_of.isoformat(),))
        (still_visible,) = conn.execute(
            "SELECT count(*) FROM asof.doc_block WHERE block_id = %s", (gold_block_id,)
        ).fetchone()  # type: ignore[misc]
    assert still_visible == 0, "重新切块后旧块应该已经被 superseded，不再可见"

    # 必须在 run_retrieval_eval 写入这次结果之前读基线——见 fetch_baseline
    # 的 docstring；读晚了会把这次自己的结果当成基线，门禁形同虚设
    # （这正是本测试在编写过程中抓到、并在 evals/cli.py 里修复的真实回归：
    # eval run --gate 此前的调用顺序永远和自己比，从未真正拦截过回退）。
    prior_baseline = fetch_baseline(conn, "retrieval", "recall_at_10")
    assert prior_baseline == pytest.approx(baseline.recall_at_10)

    regressed = run_retrieval_eval(
        conn, service, git_sha="bad-chunk-config", config=RetrievalConfig(rerank_enabled=False)
    )

    assert regressed.recall_at_10 < baseline.recall_at_10
    assert regressed.recall_at_10 == pytest.approx(0.0)

    ok = passes_gate(regressed.recall_at_10, prior_baseline, tolerance=0.02)
    assert ok is False, "门禁必须拦住这次回退——这就是 CI 红灯"
