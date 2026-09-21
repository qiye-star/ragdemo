"""P1 验收：docs/10-roadmap.md P1 表格的九项，逐条落成可执行断言。

跑绿 = P1 可以收。跑红 = 不能进 P2。

**三项做不到「真跑」，只能做结构性验证**（与该表格逐项核对，标注见各测试的
docstring）：

- 第 1 项（Dagster 连续 5 天无人工干预）：本会话没有 5 天时间跨度，也不适合
  在这里驱动真实的 Dagster 资产物化（会与当前正在并发重写的 ingest 代码耦合）。
  只验证管线重跑幂等这一必要条件。
- 第 6 项（双跑一致性）：roadmap 原文明确「需要间隔一周，不能压缩」。首次运行
  写快照并 skip，满 7 天后重跑本文件才真正完成这一项。
- 第 8 项（备份恢复演练）：真实演练会覆盖 `ragdemo-db`，而这个库当前被其他
  并发会话共用，跑一次真实 `pg_dump`/`pg_restore` 会造成不可控的数据丢失。
  只做结构性核验（脚本具备演练所需的能力），不驱动真实演练。

其余六项（2/3/4/5/7/9）在 `corpus` 夹具的小语料上真实执行。数字在夹具规模下
意义有限（不代表生产规模的召回/延迟），但断言本身、SQL、阈值都是真实跑通的，
不是占位符。评测用例（`_seed_eval_cases`）只是 5 条示例，不是
docs/06-retrieval.md §9.2 要求的 100 条生产评测集——那是创始人工作流 W9 的职责。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import chromadb
import psycopg
import pytest

from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.mock import MockEmbedder
from ragdemo.embed.reconcile import IndexDrift, reconcile_index
from ragdemo.evals.index_recall import index_recall
from ragdemo.evals.runner import run_retrieval_eval
from ragdemo.retrieval.lexical import explain_bm25
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest
from ragdemo.retrieval.vector_index import ChromaVectorIndex
from ragdemo_core.db.invariants import check_point_in_time_leaks

SNAPSHOT = Path(".superpowers/p1-consistency-snapshot.json")
_FUTURE_AS_OF = datetime(2025, 6, 1, tzinfo=UTC)

# (问题, 用来定位 gold 块的内容片段（可多个）, entity_filter, difficulty)
#
# 案例 1、2 的 gold 片段刻意各带两个：doc1 的「云端训练芯片出货量提升」
# （ordinal 1）与「研发费用 1,890 万元」（ordinal 2）是同一个父块下的兄弟叶子块，
# `expand_to_evidence`（Task 6）按父块分组去重，一组只留一个代表性 block_id——
# 但那个代表块的 `content` 是聚合整个父块小节后的结果，两个兄弟叶子块的文本
# 都在里面（`docs/05-document-pipeline.md §7`「父块是整个小节」），用户视角看到
# 的信息是完整的。`gold_block_ids` 允许多个正是为了表达这种「同一事实出现在
# 多处都算对」（docs/06-retrieval.md §9.2），这里让两条用例互相接受对方的
# block_id，避免把「父子块去重选中了兄弟块」误判成召回失败。
_CASES: tuple[tuple[str, list[str], list[str], str], ...] = (
    (
        "寒武纪三季度云端训练芯片业务收入表现如何？",
        ["云端训练芯片出货量提升", "研发费用 1,890 万元"],
        ["CN.688256"],
        "easy",
    ),
    (
        "寒武纪研发费用同比增长多少？",
        ["研发费用 1,890 万元", "云端训练芯片出货量提升"],
        ["CN.688256"],
        "easy",
    ),
    ("寒武纪智能计算业务分部收入是多少？", ["智能计算 | 12,340"], ["CN.688256"], "medium"),
    ("紫光国微特种集成电路业务收入情况如何？", ["特种集成电路业务收入"], ["CN.002049"], "easy"),
    (
        "寒武纪与云计算厂商签订的采购合同金额是多少？",
        ["采购合同，合同金额"],
        ["CN.688256"],
        "hard",
    ),
)


def _block_id(conn: psycopg.Connection, content_fragment: str) -> int:
    row = conn.execute(
        "SELECT block_id FROM core.doc_block WHERE content LIKE %s AND is_leaf LIMIT 1",
        (f"%{content_fragment}%",),
    ).fetchone()
    assert row is not None, f"语料里找不到含有 {content_fragment!r} 的叶子块"
    return int(row[0])


def _seed_eval_cases(conn: psycopg.Connection, as_of_2024: datetime) -> None:
    """从 tests/corpus.py 的固定语料里挑 5 条用例，覆盖两家实体、跨 as_of
    （最后一条用 `_FUTURE_AS_OF`，专门覆盖「过滤后召回是否塌陷」这一评测意图，
    docs/06-retrieval.md §9.2 对 100 条生产用例的同一条要求）。
    """
    for question, fragments, entities, difficulty in _CASES:
        as_of = _FUTURE_AS_OF if any("采购合同" in f for f in fragments) else as_of_2024
        gold = [_block_id(conn, f) for f in fragments]
        conn.execute(
            "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids,"
            " entity_filter, difficulty, author) VALUES (%s,%s,%s,%s,%s,'p1-acceptance')",
            (question, as_of, gold, entities, difficulty),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# 第 1 项：Dagster 管线连续 5 天无人工干预跑通，5/5 分区成功
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_document_pipeline_reruns_are_idempotent(corpus: psycopg.Connection) -> None:
    """结构性替代：只验证管线重跑幂等——这是「连续多天无人工干预」能够成立的
    必要条件，不等价于它。真正的 5/5 分区验收需要真实的 5 天时间跨度，
    留给正式上线后的运维观察窗口。
    """
    before = corpus.execute(
        "SELECT count(*) FROM core.doc_block WHERE is_leaf AND embedding IS NOT NULL"
    ).fetchone()
    embed_pending_blocks(corpus, MockEmbedder())  # 待办队列应已空（corpus 夹具已跑过一次）
    after = corpus.execute(
        "SELECT count(*) FROM core.doc_block WHERE is_leaf AND embedding IS NOT NULL"
    ).fetchone()
    assert before == after, "待办队列为空时重跑不应该改变任何已嵌入的块"


# ---------------------------------------------------------------------------
# 第 2 / 3 项：eval_retrieval recall@10 > 0.80，recall@50 > 0.92
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_recall_at_10_above_threshold(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    _seed_eval_cases(corpus, as_of_2024)
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    result = run_retrieval_eval(corpus, service, git_sha="p1-accept", config=RetrievalConfig())
    assert result.recall_at_10 > 0.80


@pytest.mark.db
def test_recall_at_50_above_threshold(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    _seed_eval_cases(corpus, as_of_2024)
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    result = run_retrieval_eval(corpus, service, git_sha="p1-accept", config=RetrievalConfig())
    assert result.recall_at_50 > 0.92


# ---------------------------------------------------------------------------
# 第 4 项：索引召回率（docs/06-retrieval.md §3.6）≥ 0.95
# + task-10-addendum.md：PG ↔ Chroma 偏移必须能收敛到 0（ADR-0009 后果 2）
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_index_recall_above_threshold(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    cases = [
        RetrievalRequest(query=q, as_of=as_of_2024)
        for q in ("云端训练芯片", "研发费用", "收入", "主营业务")
    ]
    assert index_recall(corpus, service, MockEmbedder(), cases) >= 0.95


@pytest.mark.db
def test_index_drift_is_empty_after_reconciliation(corpus: psycopg.Connection) -> None:
    """ADR-0009 的推翻条件之一：偏移无法收敛到 0 就要退回纯 pgvector。

    `corpus` 夹具调用 `embed_pending_blocks(conn, MockEmbedder())` 时没有传
    `index`，只写了 PG，Chroma 侧仍是空的——这正是 `reconcile_index` 要兜底的
    那类偏移（`embed/reconcile.py` 顶部注释）。先确认偏移确实存在（否则这条
    测试什么也没测），再验证 `reconcile_index` 修复后归零。
    """
    index = ChromaVectorIndex(chromadb.EphemeralClient(), owner_user="p1-accept-drift")

    before = reconcile_index(corpus, index)
    assert before.missing_in_index, "语料应已在 PG 写入向量但从未同步到 Chroma"

    after = reconcile_index(corpus, index)
    assert after == IndexDrift(missing_in_index=[], orphan_in_index=[], checked=before.checked)


# ---------------------------------------------------------------------------
# 第 5 项：p95 检索延迟 < 3s
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_p95_latency_under_3s(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    """本机小语料的数字不代表生产规模，但门禁本身、阈值都要挂到真实数字上。"""
    _seed_eval_cases(corpus, as_of_2024)
    cases = corpus.execute(
        "SELECT question, as_of FROM evals.eval_retrieval ORDER BY q_id"
    ).fetchall()
    assert cases, "评测集为空，无法测延迟"
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    latencies = sorted(
        service.search(RetrievalRequest(query=str(q), as_of=a)).stats.ms_total for q, a in cases
    )
    p95 = latencies[int(len(latencies) * 0.95) - 1] if len(latencies) > 1 else latencies[0]
    assert p95 < 3000


# ---------------------------------------------------------------------------
# 第 6 项：双跑一致性（docs/03-point-in-time.md §5.1），间隔一周两次结果完全一致
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_double_run_consistency(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    """首次运行写快照并 skip；间隔至少 7 天后重跑本测试才真正完成验收。

    这是结构性验证，不是真实经过 7 天的双跑：`corpus` 夹具每次都用一个全新的
    临时库（`tests/corpus.py` 的 `temp_db`），两次运行的 block_id 只有在
    `tests/corpus.py` 的插入顺序不变时才具备可比性——这是本方法本身的前提，
    不是缺陷。未满 7 天时 skip 而不是断言失败：`make accept-p1` 不应该在
    等待期内被这一项拖成永久红灯。
    """
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    req = RetrievalRequest(query="云端训练芯片业务表现", as_of=as_of_2024)
    current = [b.block_id for b in service.search(req).blocks]

    if not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps({"taken_at": datetime.now(UTC).isoformat(), "block_ids": current}),
            encoding="utf-8",
        )
        pytest.skip(f"已写入基线快照 {SNAPSHOT}；间隔至少 7 天后重跑本测试完成验收")

    baseline = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    taken_at = datetime.fromisoformat(baseline["taken_at"])
    elapsed_days = (datetime.now(UTC) - taken_at).days
    if elapsed_days < 7:
        pytest.skip(f"快照仅 {elapsed_days} 天，需间隔至少 7 天才能完成验收")
    assert current == baseline["block_ids"], "同一 as_of 两次结果不一致——存在时点泄漏"


# ---------------------------------------------------------------------------
# 第 7 项：时点泄漏自检 5 条查询，全部返回空
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_no_point_in_time_leaks(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    assert check_point_in_time_leaks(corpus, as_of_2024) == {}


# ---------------------------------------------------------------------------
# 第 8 项：备份恢复演练，从 pg_dump 完整恢复一次，数据校验通过
# ---------------------------------------------------------------------------


def test_backup_restore_drill_structural_preconditions() -> None:
    """只做结构性核验，不驱动真实演练。

    真实演练需要 `pg_dump` 整库 + `pg_restore` 到一个隔离环境再做数据校验；
    在这个会话里对 `ragdemo-db` 跑一次会覆盖其他并发会话正在使用的库
    （见 `.superpowers/sdd/2026-09-21-p1c-retrieval-and-acceptance/progress.md`
    「环境风险」一节），代价不可控。这里核验的是 Task 11 已交付的脚本与
    runbook 具备演练所需的能力（`tests/test_infra.py` 覆盖了更细的不变量，
    这里只挑与「完整恢复 + 数据校验」直接相关的几条，不重复整份清单）。
    真实演练要在专门预留的隔离窗口里由人工或独立 staging 环境执行。
    """
    backup = Path("scripts/backup.sh").read_text(encoding="utf-8")
    restore = Path("scripts/restore.sh").read_text(encoding="utf-8")
    runbook = Path("infra/runbook-backup.md").read_text(encoding="utf-8")
    assert "pg_dump" in backup and "chroma" in backup.lower(), "备份必须同时覆盖 PG 与 Chroma"
    assert "pg_restore" in restore and "chroma" in restore.lower(), "恢复必须同时覆盖 PG 与 Chroma"
    assert "CONFIRM" in restore, "恢复会覆盖现有库，必须要求显式确认"
    assert "同一时点" in runbook, "PG 与 Chroma 快照必须对齐到同一时点（ADR-0009 后果 1）"
    assert "恢复演练" in runbook, "runbook 必须记录恢复演练步骤"


# ---------------------------------------------------------------------------
# 第 9 项：EXPLAIN 验证 BM25 过滤下推，过滤条件在索引扫描节点内
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_bm25_filters_are_pushed_into_the_index(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    plan = explain_bm25(
        corpus,
        RetrievalRequest(query="云端训练芯片", as_of=as_of_2024, entity_ids=["CN.688256"]),
    )
    assert "Seq Scan on doc_block" not in plan, f"退化成顺序扫描:\n{plan}"
