# P1c 检索与 P1 验收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 `RetrievalService`（过滤 → 双路召回 → 加权 RRF → 重排 → 父子块展开），并把 [`10-roadmap.md`](../../10-roadmap.md) P1 的九项验收指标全部做成可执行的测试。

**Architecture:** 检索走 `asof` 安全视图保证正确性，同时在 SQL 里写一遍显式时点谓词保证可下推——正确性靠声明式机制，性能靠显式条件。两路召回各取 50，加权 RRF 融合后取 50，重排取 10，最后按父块展开去重。评测框架把每次运行写进 `evals.eval_run`，指标可追溯回参数。

**Tech Stack:** Python 3.11+ / psycopg 3 / ParadeDB `pg_search` / pgvector / pytest / Click

**Spec:** [`docs/06-retrieval.md`](../../06-retrieval.md)、[`docs/08-evaluation.md`](../../08-evaluation.md)、[`docs/03-point-in-time.md`](../../03-point-in-time.md) §5
**工作流：** [`docs/11-sdlc.md`](../../11-sdlc.md) §3 的 W3.1–W3.7、W6.1–W6.2、W7.4、W7.6
**前置：** [P0](2026-09-21-p0-foundation.md)、[P1a](2026-09-21-p1a-ingestion.md)、[P1b](2026-09-21-p1b-document-pipeline.md) 全部验收

## 对上游的接口假设

| 接口 | 来自 |
|---|---|
| `as_of_session(conn, as_of, *, tenant, user)` | P0 Task 10 |
| `check_point_in_time_leaks(conn, probe_as_of)` | P0 Task 11 |
| `asof.doc_block` 视图、`doc_block_bm25`、`doc_block_embedding_hnsw` | P0 Task 7/9 |
| `Embedder` 协议、`MockEmbedder`、`l2_normalize` | P1b Task 7 |
| `DocumentWriter`、切块器、`build_tree` | P1b Task 2/3/6 |
| `MockAnnouncementProvider` | P1a Task 8 |
| `HttpClient` | P1a Task 5 |

## Global Constraints

- Python **3.11+**；完整类型注解；`ruff` 与 `mypy --strict` 通过。
- **TDD**：先写失败的测试，跑一遍确认失败，再写最小实现。
- **`as_of` 必填、无默认值**，且必须带时区。
- 检索**只查 `asof` 视图**，不查 `core` 基表；同时在 SQL 里写一遍显式时点谓词（[06](../../06-retrieval.md) §3.4 的冗余谓词）。
- 融合用**加权 RRF**：`score = w_bm25/(k+rank_bm25) + w_vec/(k+rank_vec)`，默认 `0.6 / 0.4 / k=60`，某一路未召回则该项贡献 0。
- **引用标注用叶子块 `block_id`**，生成用父块内容。
- 重排失败**降级为 RRF 顺序**并标 `reranked=false`，不得让查询失败。
- 每次查询都记录 `RetrievalStats` 到结构化日志，含 `run_id` / `as_of` / `entity_id`。
- **评测参数改动必须跑评测并把数字贴进 PR**；每次运行写一行 `evals.eval_run`。
- 提交信息用 Conventional Commits。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `src/ragdemo/retrieval/__init__.py` | 包声明 |
| `src/ragdemo/retrieval/types.py` | `RetrievalConfig` / `RetrievalRequest` / `EvidenceBlock` / `RetrievalStats` / `RetrievalResult` |
| `src/ragdemo/retrieval/filters.py` | 过滤条件 → SQL 片段与参数 |
| `src/ragdemo/retrieval/lexical.py` | BM25 一路 |
| `src/ragdemo/retrieval/vector.py` | 向量一路 + 迭代扫描参数 |
| `src/ragdemo/retrieval/fusion.py` | 加权 RRF |
| `src/ragdemo/retrieval/rerank.py` | `Reranker` 协议 + Mock + 降级 |
| `src/ragdemo/retrieval/expand.py` | 父子块展开与去重 |
| `src/ragdemo/retrieval/rewrite.py` | 查询改写（默认关闭） |
| `src/ragdemo/retrieval/service.py` | `RetrievalService` 编排 |
| `src/ragdemo/evals/__init__.py` | 包声明 |
| `src/ragdemo/evals/runner.py` | 评测框架 + `eval_run` 记录 |
| `src/ragdemo/evals/retrieval_metrics.py` | `recall@k` / `MRR` / 索引召回率 |
| `src/ragdemo/evals/cli.py` | `ragdemo eval add-retrieval` / `run` |
| `infra/egress-proxy/policy.yaml` | 出网域名白名单与凭据注入 |
| `infra/runbook-backup.md` | 备份与恢复手册 |
| `scripts/backup.sh` / `scripts/restore.sh` | 备份与恢复脚本 |

---

## Task 1: 检索数据结构与过滤

**Files:**
- Create: `src/ragdemo/retrieval/__init__.py`, `src/ragdemo/retrieval/types.py`, `src/ragdemo/retrieval/filters.py`, `tests/retrieval/__init__.py`, `tests/retrieval/test_types.py`, `tests/retrieval/test_filters.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `RetrievalConfig(candidate_k=50, fusion_k=50, top_k=10, w_bm25=0.6, w_vec=0.4, rrf_k=60, ef_search=200, rewrite_enabled=False, rerank_enabled=True)`
  - `RetrievalRequest(query, as_of, entity_ids=None, doc_types=None, published_after=None, tenant=None, user=None, config=RetrievalConfig())`
  - `EvidenceBlock(block_id, parent_block_id, content, matched_child_ids, doc_id, doc_title, section_path, page, known_at, score, reranked)`
  - `RetrievalStats(bm25_hits, vec_hits, after_fusion, after_rerank, ms_bm25, ms_vec, ms_rerank, ms_total, degraded)`
  - `RetrievalResult(blocks, stats)`
  - `build_filters(req) -> tuple[str, dict[str, Any]]`

- [ ] **Step 1: 写失败的测试**

`tests/retrieval/__init__.py`：空文件。

`tests/retrieval/test_types.py`：

```python
"""检索请求与配置的不变量。"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest

AS_OF = datetime(2024, 12, 31, tzinfo=UTC)


def test_defaults_match_the_spec() -> None:
    cfg = RetrievalConfig()
    assert (cfg.candidate_k, cfg.fusion_k, cfg.top_k) == (50, 50, 10)
    assert (cfg.w_bm25, cfg.w_vec, cfg.rrf_k) == (0.6, 0.4, 60)
    assert cfg.rewrite_enabled is False, "查询改写默认关闭，开启需评测证明"
    assert cfg.rerank_enabled is True


def test_as_of_is_required_and_has_no_default() -> None:
    with pytest.raises(TypeError):
        RetrievalRequest(query="算力")  # type: ignore[call-arg]


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="时区"):
        RetrievalRequest(query="算力", as_of=datetime(2024, 12, 31))


def test_empty_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="query"):
        RetrievalRequest(query="   ", as_of=AS_OF)


def test_top_k_larger_than_fusion_k_is_rejected() -> None:
    with pytest.raises(ValueError, match="top_k"):
        RetrievalConfig(fusion_k=10, top_k=50).validate()


def test_weights_must_be_positive() -> None:
    with pytest.raises(ValueError, match="权重"):
        RetrievalConfig(w_bm25=0.0, w_vec=0.0).validate()
```

`tests/retrieval/test_filters.py`：

```python
"""过滤条件：时点谓词必须出现在 SQL 里（冗余谓词，06 §3.4）。"""
from __future__ import annotations

from datetime import UTC, datetime

from ragdemo.retrieval.filters import build_filters
from ragdemo.retrieval.types import RetrievalRequest

AS_OF = datetime(2024, 12, 31, tzinfo=UTC)


def test_time_point_predicates_are_always_present() -> None:
    """走 asof 视图还写一遍，是为了让谓词能下推进索引（06 §3.4）。"""
    sql, params = build_filters(RetrievalRequest(query="算力", as_of=AS_OF))
    assert "known_at <= %(as_of)s" in sql
    assert "superseded_at IS NULL OR superseded_at > %(as_of)s" in sql
    assert params["as_of"] == AS_OF


def test_is_leaf_is_always_filtered() -> None:
    sql, _ = build_filters(RetrievalRequest(query="算力", as_of=AS_OF))
    assert "is_leaf" in sql


def test_owner_uses_is_not_distinct_from_not_equals() -> None:
    """公共文档的 owner 是 NULL，用 = 会把它们全部过滤掉。"""
    sql, params = build_filters(RetrievalRequest(query="算力", as_of=AS_OF))
    assert "owner_tenant IS NOT DISTINCT FROM %(tenant)s" in sql
    assert params["tenant"] is None


def test_entity_filter_is_optional_and_parameterised() -> None:
    sql, params = build_filters(
        RetrievalRequest(query="算力", as_of=AS_OF, entity_ids=["CN.688256"])
    )
    assert "entity_id = ANY(%(entity_ids)s)" in sql
    assert params["entity_ids"] == ["CN.688256"]


def test_no_entity_filter_means_no_entity_clause() -> None:
    sql, params = build_filters(RetrievalRequest(query="算力", as_of=AS_OF))
    assert "entity_id = ANY" not in sql
    assert "entity_ids" not in params


def test_doc_type_and_published_after_are_supported() -> None:
    sql, params = build_filters(
        RetrievalRequest(
            query="算力", as_of=AS_OF, doc_types=["quarterly"],
            published_after=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    assert "doc_type = ANY(%(doc_types)s)" in sql
    assert "publish_at >= %(published_after)s" in sql
    assert params["doc_types"] == ["quarterly"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/retrieval -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.retrieval'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/retrieval/__init__.py`：

```python
"""检索管线：过滤 → 双路召回 → 加权 RRF → 重排 → 父子块展开。"""
```

`src/ragdemo/retrieval/types.py`：

```python
"""检索的请求、配置与结果。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class RetrievalConfig:
    candidate_k: int = 50
    fusion_k: int = 50
    top_k: int = 10
    w_bm25: float = 0.6
    w_vec: float = 0.4
    rrf_k: int = 60
    ef_search: int = 200
    max_scan_tuples: int = 200_000
    rewrite_enabled: bool = False
    rerank_enabled: bool = True

    def validate(self) -> None:
        if self.top_k > self.fusion_k:
            raise ValueError(f"top_k {self.top_k} 不能大于 fusion_k {self.fusion_k}")
        if self.fusion_k > self.candidate_k * 2:
            raise ValueError("fusion_k 超出两路候选之和的上限")
        if self.w_bm25 <= 0 and self.w_vec <= 0:
            raise ValueError("两路权重不能同时为 0")
        if self.rrf_k <= 0:
            raise ValueError("rrf_k 必须为正")


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    as_of: datetime
    entity_ids: list[str] | None = None
    doc_types: list[str] | None = None
    published_after: datetime | None = None
    tenant: str | None = None
    user: str | None = None
    config: RetrievalConfig = field(default_factory=RetrievalConfig)

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query 不能为空")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError(f"as_of 必须带时区，收到 {self.as_of!r}")
        self.config.validate()


@dataclass(frozen=True)
class EvidenceBlock:
    block_id: int
    parent_block_id: int | None
    content: str
    matched_child_ids: list[int]
    doc_id: int
    doc_title: str
    section_path: str
    page: int | None
    known_at: datetime
    score: float
    reranked: bool


@dataclass(frozen=True)
class RetrievalStats:
    bm25_hits: int = 0
    vec_hits: int = 0
    after_fusion: int = 0
    after_rerank: int = 0
    ms_bm25: float = 0.0
    ms_vec: float = 0.0
    ms_rerank: float = 0.0
    ms_total: float = 0.0
    degraded: bool = False


@dataclass(frozen=True)
class RetrievalResult:
    blocks: list[EvidenceBlock]
    stats: RetrievalStats
```

`src/ragdemo/retrieval/filters.py`：

```python
"""过滤条件 → SQL 片段。

时点谓词即使走 asof 视图也要再写一遍：视图里的 asof.current_as_of() 是
PL/pgSQL 函数调用，很可能阻止优化器把谓词下推进 tantivy 或 HNSW 的过滤
（docs/06-retrieval.md §3.4）。冗余不改变结果集，只影响执行计划。
"""
from __future__ import annotations

from typing import Any

from ragdemo.retrieval.types import RetrievalRequest


def build_filters(req: RetrievalRequest) -> tuple[str, dict[str, Any]]:
    """返回 (WHERE 片段, 参数字典)。片段以 AND 开头，可直接拼在已有条件之后。"""
    clauses = [
        "known_at <= %(as_of)s",
        "(superseded_at IS NULL OR superseded_at > %(as_of)s)",
        "is_leaf",
        "owner_tenant IS NOT DISTINCT FROM %(tenant)s",
        "owner_user IS NOT DISTINCT FROM %(user)s",
    ]
    params: dict[str, Any] = {
        "as_of": req.as_of,
        "tenant": req.tenant,
        "user": req.user,
    }

    if req.entity_ids:
        clauses.append("entity_id = ANY(%(entity_ids)s)")
        params["entity_ids"] = list(req.entity_ids)
    if req.doc_types:
        clauses.append("doc_type = ANY(%(doc_types)s)")
        params["doc_types"] = list(req.doc_types)
    if req.published_after is not None:
        clauses.append("publish_at >= %(published_after)s")
        params["published_after"] = req.published_after

    return " AND " + " AND ".join(clauses), params
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/retrieval -v`
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/retrieval tests/retrieval
git commit -m "feat(retrieval): 检索数据结构与冗余时点谓词的过滤构造"
```

---

## Task 2: BM25 一路与下推验证

**Files:**
- Create: `src/ragdemo/retrieval/lexical.py`, `tests/retrieval/conftest.py`, `tests/retrieval/test_lexical.py`

**Interfaces:**
- Consumes: Task 1；P1b 的入库管线
- Produces:
  - `RankedHit(block_id: int, rank: int, raw_score: float)`
  - `bm25_search(conn, req) -> list[RankedHit]`
  - `explain_bm25(conn, req) -> str`

- [ ] **Step 1: 写失败的测试**

`tests/retrieval/conftest.py`：

```python
"""检索测试的共享语料：两家公司、三份文档、含表格与历史时点。"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate
from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.mock import MockEmbedder

MIGRATIONS = Path("db/migrations")

_ENTITIES = (
    ("CN.688256", "寒武纪-U", "688256.SH"),
    ("CN.002049", "紫光国微", "002049.SZ"),
)

# (doc_id, entity_id, doc_type, title, publish_at, content_hash)
_DOCS = (
    (1, "CN.688256", "quarterly", "寒武纪 2024 年三季报", "2024-10-28 18:32+08", "h1"),
    (2, "CN.688256", "announcement", "关于签订重大销售合同的公告", "2025-03-05 19:00+08", "h2"),
    (3, "CN.002049", "quarterly", "紫光国微 2024 年三季报", "2024-10-25 17:10+08", "h3"),
)

# (doc_id, ordinal, block_type, section_path, content, is_leaf, parent_ordinal)
_BLOCKS = (
    (1, 0, "paragraph", "第三节 主营业务", "第三节 主营业务\n（父块）", False, None),
    (1, 1, "paragraph", "第三节 主营业务",
     "第三节 主营业务\n报告期内云端训练芯片出货量提升，智能计算集群系统业务收入 12,340 万元，"
     "同比增长 58.2%。", True, 0),
    (1, 2, "paragraph", "第三节 主营业务",
     "第三节 主营业务\n研发费用 1,890 万元，同比增长 22.4%，主要用于下一代训练芯片流片。",
     True, 0),
    (1, 3, "table", "第三节 主营业务",
     "| 业务分部 | 收入(万元) | 同比 |\n| 智能计算 | 12,340 | +58.2% |", True, None),
    (2, 0, "paragraph", "正文",
     "正文\n公司与某云计算厂商签订云端训练芯片采购合同，合同金额 8.5 亿元。", True, None),
    (3, 0, "paragraph", "第三节 主营业务",
     "第三节 主营业务\n特种集成电路业务收入 4,021 万元，智能安全芯片需求平稳。", True, None),
)


@pytest.fixture()
def corpus(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)

    for entity_id, name, code in _ENTITIES:
        conn.execute(
            "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
            " l2_segment, l3_node, primary_node, tushare_code) "
            "VALUES (%s,%s,'listed','算力','AI芯片',ARRAY['云端训练芯片'],"
            " '云端训练芯片',%s)",
            (entity_id, name, code),
        )

    for doc_id, entity_id, doc_type, title, publish_at, chash in _DOCS:
        conn.execute(
            "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at,"
            " source, content_hash, version_group_id, valid_from, known_at, ingest_run_id)"
            " OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,'mock',%s,%s,"
            " '2024-07-01',%s,'r1')",
            (doc_id, entity_id, doc_type, title, publish_at, chash, doc_id, publish_at),
        )

    parent_ids: dict[tuple[int, int], int] = {}
    for doc_id, ordinal, btype, section, content, is_leaf, parent_ordinal in _BLOCKS:
        parent_block_id = parent_ids.get((doc_id, parent_ordinal)) if parent_ordinal is not None else None
        row = conn.execute(
            "INSERT INTO core.doc_block (doc_id, parent_block_id, block_type, section_path,"
            " ordinal, page, content, is_leaf, entity_id, doc_type, publish_at,"
            " valid_from, known_at, source, ingest_run_id) "
            "SELECT %s,%s,%s,%s,%s,1,%s,%s,d.entity_id,d.doc_type,d.publish_at,"
            " d.valid_from,d.known_at,'mock','r1' FROM core.document d WHERE d.doc_id = %s "
            "RETURNING block_id",
            (doc_id, parent_block_id, btype, section, ordinal, content, is_leaf, doc_id),
        ).fetchone()
        assert row is not None
        parent_ids[(doc_id, ordinal)] = int(row[0])

    conn.commit()
    embed_pending_blocks(conn, MockEmbedder())
    conn.commit()
    return conn


@pytest.fixture()
def as_of_2024() -> datetime:
    """2024-12-31：能看到两份三季报，看不到 2025-03 的公告。"""
    return datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)
```

`tests/retrieval/test_lexical.py`：

```python
"""BM25 一路：中文命中、时点过滤、实体过滤、下推验证。"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.retrieval.lexical import bm25_search, explain_bm25
from ragdemo.retrieval.types import RetrievalRequest


@pytest.mark.db
def test_chinese_query_hits(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    hits = bm25_search(corpus, RetrievalRequest(query="云端训练芯片", as_of=as_of_2024))
    assert hits


@pytest.mark.db
def test_ranks_start_at_one_and_are_contiguous(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    hits = bm25_search(corpus, RetrievalRequest(query="收入", as_of=as_of_2024))
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))


@pytest.mark.db
def test_future_documents_are_invisible(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """2025-03 的合同公告在 2024-12-31 的时点上必须不可见。"""
    hits = bm25_search(corpus, RetrievalRequest(query="采购合同", as_of=as_of_2024))
    assert hits == []

    later = bm25_search(
        corpus,
        RetrievalRequest(query="采购合同", as_of=datetime(2025, 6, 1, tzinfo=UTC)),
    )
    assert later


@pytest.mark.db
def test_entity_filter_excludes_other_companies(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    hits = bm25_search(
        corpus,
        RetrievalRequest(query="收入", as_of=as_of_2024, entity_ids=["CN.002049"]),
    )
    block_ids = [h.block_id for h in hits]
    rows = corpus.execute(
        "SELECT DISTINCT entity_id FROM core.doc_block WHERE block_id = ANY(%s)",
        (block_ids,),
    ).fetchall()
    assert {r[0] for r in rows} == {"CN.002049"}


@pytest.mark.db
def test_parent_blocks_are_never_returned(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    hits = bm25_search(corpus, RetrievalRequest(query="主营业务", as_of=as_of_2024))
    if hits:
        rows = corpus.execute(
            "SELECT bool_and(is_leaf) FROM core.doc_block WHERE block_id = ANY(%s)",
            ([h.block_id for h in hits],),
        ).fetchone()
        assert rows is not None and rows[0] is True


@pytest.mark.db
def test_candidate_k_caps_the_result(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    from ragdemo.retrieval.types import RetrievalConfig

    req = RetrievalRequest(
        query="收入", as_of=as_of_2024, config=RetrievalConfig(candidate_k=1)
    )
    assert len(bm25_search(corpus, req)) <= 1


@pytest.mark.db
def test_explain_shows_bm25_index_usage(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """P1 验收项：EXPLAIN 验证 BM25 过滤下推（10-roadmap P1）。

    小语料下优化器可能选顺序扫描，因此这里只断言计划可取到且包含表名；
    真实数据量下的下推验证在 Task 10 的 nightly 任务里。
    """
    plan = explain_bm25(corpus, RetrievalRequest(query="云端训练芯片", as_of=as_of_2024))
    assert "doc_block" in plan
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/retrieval/test_lexical.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.retrieval.lexical'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/retrieval/lexical.py`：

```python
"""BM25 一路（ParadeDB pg_search）。

entity_id / doc_type / is_leaf / known_at 都在 BM25 索引里且标了 fast，
ParadeDB 能把这些谓词下推进 tantivy，过滤在打分之前完成——这是时点过滤后
仍能保证召回的关键（docs/06-retrieval.md §3.2）。
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ragdemo.retrieval.filters import build_filters
from ragdemo.retrieval.types import RetrievalRequest

_SQL = """
SELECT block_id,
       ROW_NUMBER() OVER (ORDER BY paradedb.score(block_id) DESC) AS rnk,
       paradedb.score(block_id) AS raw_score
  FROM asof.doc_block
 WHERE content @@@ %(query)s
 {filters}
 ORDER BY paradedb.score(block_id) DESC
 LIMIT %(candidate_k)s
"""


@dataclass(frozen=True)
class RankedHit:
    block_id: int
    rank: int
    raw_score: float


def _statement(req: RetrievalRequest) -> tuple[str, dict[str, object]]:
    filters, params = build_filters(req)
    params["query"] = req.query
    params["candidate_k"] = req.config.candidate_k
    return _SQL.format(filters=filters), params


def bm25_search(conn: psycopg.Connection, req: RetrievalRequest) -> list[RankedHit]:
    sql, params = _statement(req)
    rows = conn.execute(sql, params).fetchall()
    return [RankedHit(int(r[0]), int(r[1]), float(r[2])) for r in rows]


def explain_bm25(conn: psycopg.Connection, req: RetrievalRequest) -> str:
    """返回执行计划文本。用于验证过滤是否下推进索引扫描节点。"""
    sql, params = _statement(req)
    rows = conn.execute(f"EXPLAIN (ANALYZE, VERBOSE) {sql}", params).fetchall()
    return "\n".join(str(r[0]) for r in rows)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/retrieval/test_lexical.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/retrieval/lexical.py tests/retrieval/conftest.py tests/retrieval/test_lexical.py
git commit -m "feat(retrieval): BM25 一路与 EXPLAIN 下推检查"
```

---

## Task 3: 向量一路与迭代扫描

**Files:**
- Create: `src/ragdemo/retrieval/vector.py`, `tests/retrieval/test_vector.py`

**Interfaces:**
- Consumes: Task 1/2；P1b 的 `Embedder`
- Produces:
  - `vector_search(conn, req, query_vector: Sequence[float]) -> list[RankedHit]`
  - `apply_scan_settings(conn, cfg: RetrievalConfig) -> None`

- [ ] **Step 1: 写失败的测试**

`tests/retrieval/test_vector.py`：

```python
"""向量一路：迭代扫描参数、时点过滤、归一化要求。"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest
from ragdemo.retrieval.vector import apply_scan_settings, vector_search


def _qvec(text: str) -> list[float]:
    return MockEmbedder().embed([text])[0]


@pytest.mark.db
def test_returns_ranked_hits(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    hits = vector_search(
        corpus, RetrievalRequest(query="云端训练芯片", as_of=as_of_2024), _qvec("云端训练芯片")
    )
    assert hits
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))


@pytest.mark.db
def test_future_documents_are_invisible(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    early = vector_search(
        corpus, RetrievalRequest(query="采购合同", as_of=as_of_2024), _qvec("采购合同")
    )
    later = vector_search(
        corpus,
        RetrievalRequest(query="采购合同", as_of=datetime(2025, 6, 1, tzinfo=UTC)),
        _qvec("采购合同"),
    )
    assert len(later) > len(early)


@pytest.mark.db
def test_scan_settings_are_applied_to_the_session(corpus: psycopg.Connection) -> None:
    """pgvector 0.8 的迭代扫描：过滤后候选不足时继续扫索引（06 §3.3）。"""
    with corpus.transaction():
        apply_scan_settings(corpus, RetrievalConfig(ef_search=321))
        ef = corpus.execute("SHOW hnsw.ef_search").fetchone()
        mode = corpus.execute("SHOW hnsw.iterative_scan").fetchone()
    assert ef is not None and ef[0] == "321"
    assert mode is not None and mode[0] == "relaxed_order"


@pytest.mark.db
def test_blocks_without_embedding_are_skipped(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """父块没有 embedding，不能出现在向量召回里。"""
    hits = vector_search(
        corpus, RetrievalRequest(query="主营业务", as_of=as_of_2024), _qvec("主营业务")
    )
    if hits:
        (all_leaf,) = corpus.execute(
            "SELECT bool_and(is_leaf AND embedding IS NOT NULL) FROM core.doc_block"
            " WHERE block_id = ANY(%s)",
            ([h.block_id for h in hits],),
        ).fetchone()  # type: ignore[misc]
        assert all_leaf is True


@pytest.mark.db
def test_wrong_dimension_query_vector_raises(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    with pytest.raises(ValueError, match="维度"):
        vector_search(corpus, RetrievalRequest(query="x", as_of=as_of_2024), [0.1, 0.2])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/retrieval/test_vector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.retrieval.vector'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/retrieval/vector.py`：

```python
"""向量一路（pgvector HNSW）。

HNSW 是近似索引：朴素写法会先取全库最相似的若干块、再过滤，
时点过滤后召回可能塌陷。pgvector 0.8 的迭代扫描解决这个问题——
候选不足时继续扫索引（docs/06-retrieval.md §3.3）。

relaxed_order 允许结果不严格按距离排序。本管线后面还有 RRF 与重排，
对严格顺序不敏感，因此这是正确的取舍。
"""
from __future__ import annotations

from collections.abc import Sequence

import psycopg

from ragdemo.embed.base import EMBEDDING_DIM
from ragdemo.retrieval.filters import build_filters
from ragdemo.retrieval.lexical import RankedHit
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest

_SQL = """
SELECT block_id,
       ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s) AS rnk,
       (embedding <=> %(qvec)s) AS distance
  FROM asof.doc_block
 WHERE embedding IS NOT NULL
 {filters}
 ORDER BY embedding <=> %(qvec)s
 LIMIT %(candidate_k)s
"""


def apply_scan_settings(conn: psycopg.Connection, cfg: RetrievalConfig) -> None:
    """必须在事务内调用——用 SET LOCAL 语义，不污染连接池里的其他请求。"""
    conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(cfg.ef_search),))
    conn.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
    conn.execute(
        "SELECT set_config('hnsw.max_scan_tuples', %s, true)", (str(cfg.max_scan_tuples),)
    )


def vector_search(
    conn: psycopg.Connection, req: RetrievalRequest, query_vector: Sequence[float]
) -> list[RankedHit]:
    if len(query_vector) != EMBEDDING_DIM:
        raise ValueError(f"查询向量维度应为 {EMBEDDING_DIM}，收到 {len(query_vector)}")

    filters, params = build_filters(req)
    params["qvec"] = "[" + ",".join(repr(float(x)) for x in query_vector) + "]"
    params["candidate_k"] = req.config.candidate_k

    rows = conn.execute(_SQL.format(filters=filters), params).fetchall()
    # distance 越小越相似，转成 raw_score 方便观察；排序已由 SQL 完成
    return [RankedHit(int(r[0]), int(r[1]), 1.0 - float(r[2])) for r in rows]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/retrieval/test_vector.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/retrieval/vector.py tests/retrieval/test_vector.py
git commit -m "feat(retrieval): 向量一路与 pgvector 迭代扫描参数"
```

---

## Task 4: 加权 RRF 融合

**Files:**
- Create: `src/ragdemo/retrieval/fusion.py`, `tests/retrieval/test_fusion.py`

**Interfaces:**
- Consumes: Task 2 的 `RankedHit`
- Produces:
  - `FusedHit(block_id: int, score: float, bm25_rank: int | None, vec_rank: int | None)`
  - `weighted_rrf(bm25, vec, *, w_bm25, w_vec, rrf_k, limit) -> list[FusedHit]`

- [ ] **Step 1: 写失败的测试**

`tests/retrieval/test_fusion.py`：

```python
"""加权 RRF（06 §4）。用排名不用分数——BM25 分数无界，不能直接加权。"""
from __future__ import annotations

from ragdemo.retrieval.fusion import weighted_rrf
from ragdemo.retrieval.lexical import RankedHit

K = 60


def _hits(*ids: int) -> list[RankedHit]:
    return [RankedHit(block_id=b, rank=i + 1, raw_score=0.0) for i, b in enumerate(ids)]


def test_block_in_both_lists_scores_higher_than_either_alone() -> None:
    fused = weighted_rrf(_hits(1, 2), _hits(1, 3), w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    by_id = {f.block_id: f.score for f in fused}
    assert by_id[1] > by_id[2]
    assert by_id[1] > by_id[3]


def test_missing_side_contributes_zero_not_a_penalty() -> None:
    """某一路未召回该块时，该项贡献 0（06 §4.1）。"""
    fused = weighted_rrf(_hits(1), [], w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    assert fused[0].score == 0.6 / (K + 1)
    assert fused[0].vec_rank is None


def test_weights_shift_the_ordering() -> None:
    bm25, vec = _hits(1, 2), _hits(2, 1)
    bm25_heavy = weighted_rrf(bm25, vec, w_bm25=0.9, w_vec=0.1, rrf_k=K, limit=10)
    vec_heavy = weighted_rrf(bm25, vec, w_bm25=0.1, w_vec=0.9, rrf_k=K, limit=10)
    assert bm25_heavy[0].block_id == 1
    assert vec_heavy[0].block_id == 2


def test_results_are_sorted_descending_by_score() -> None:
    fused = weighted_rrf(_hits(1, 2, 3), _hits(3, 2, 1), w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    assert [f.score for f in fused] == sorted((f.score for f in fused), reverse=True)


def test_limit_truncates() -> None:
    fused = weighted_rrf(_hits(1, 2, 3), [], w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=2)
    assert len(fused) == 2


def test_empty_inputs_give_empty_output() -> None:
    assert weighted_rrf([], [], w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10) == []


def test_ranks_are_preserved_for_diagnostics() -> None:
    """stats 与排查「为什么没找到」都要看两路各自的排名。"""
    fused = weighted_rrf(_hits(1), _hits(1), w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    assert (fused[0].bm25_rank, fused[0].vec_rank) == (1, 1)


def test_score_formula_matches_the_spec() -> None:
    fused = weighted_rrf(_hits(7), _hits(9, 7), w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    seven = next(f for f in fused if f.block_id == 7)
    assert seven.score == 0.6 / (K + 1) + 0.4 / (K + 2)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/retrieval/test_fusion.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.retrieval.fusion'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/retrieval/fusion.py`：

```python
"""加权 Reciprocal Rank Fusion。

原简报同时写了「BM25 权重 0.6 / 向量 0.4」与「RRF 合并」，这是两种互斥的融合：
加权线性融合用分数（BM25 分数无界，必须先归一化，而归一化对异常值敏感），
RRF 只用排名（天然无量纲）。统一为加权 RRF——权重作用在 RRF 项上，
既保留「BM25 更重要」的意图，又避免分数尺度问题（docs/06-retrieval.md §4.1）。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ragdemo.retrieval.lexical import RankedHit


@dataclass(frozen=True)
class FusedHit:
    block_id: int
    score: float
    bm25_rank: int | None
    vec_rank: int | None


def weighted_rrf(
    bm25: Sequence[RankedHit],
    vec: Sequence[RankedHit],
    *,
    w_bm25: float,
    w_vec: float,
    rrf_k: int,
    limit: int,
) -> list[FusedHit]:
    """score(d) = w_bm25/(k + rank_bm25) + w_vec/(k + rank_vec)，缺席的一路贡献 0。"""
    bm25_ranks = {h.block_id: h.rank for h in bm25}
    vec_ranks = {h.block_id: h.rank for h in vec}

    fused = [
        FusedHit(
            block_id=block_id,
            score=(
                (w_bm25 / (rrf_k + bm25_ranks[block_id]) if block_id in bm25_ranks else 0.0)
                + (w_vec / (rrf_k + vec_ranks[block_id]) if block_id in vec_ranks else 0.0)
            ),
            bm25_rank=bm25_ranks.get(block_id),
            vec_rank=vec_ranks.get(block_id),
        )
        for block_id in {*bm25_ranks, *vec_ranks}
    ]
    fused.sort(key=lambda f: (-f.score, f.block_id))
    return fused[:limit]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/retrieval/test_fusion.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/retrieval/fusion.py tests/retrieval/test_fusion.py
git commit -m "feat(retrieval): 加权 RRF 融合，统一简报中互斥的两种融合表述"
```

---

## Task 5: 重排与降级

**Files:**
- Create: `src/ragdemo/retrieval/rerank.py`, `tests/retrieval/test_rerank.py`

**Interfaces:**
- Consumes: Task 4 的 `FusedHit`
- Produces:
  - `Reranker` Protocol：`model: str`、`rerank(query, docs, top_k) -> list[tuple[int, float]]`
  - `MockReranker`（按词重叠打分，确定性）
  - `FailingReranker`（测试降级用）
  - `RerankOutcome(order: list[int], scores: dict[int, float], degraded: bool)`
  - `rerank_or_degrade(reranker, query, fused, contents, top_k) -> RerankOutcome`

- [ ] **Step 1: 写失败的测试**

`tests/retrieval/test_rerank.py`：

```python
"""重排与降级：失败时回退 RRF 顺序，不让查询失败。"""
from __future__ import annotations

import pytest

from ragdemo.retrieval.fusion import FusedHit
from ragdemo.retrieval.rerank import (
    FailingReranker,
    MockReranker,
    rerank_or_degrade,
)

FUSED = [
    FusedHit(block_id=1, score=0.03, bm25_rank=1, vec_rank=2),
    FusedHit(block_id=2, score=0.02, bm25_rank=2, vec_rank=None),
    FusedHit(block_id=3, score=0.01, bm25_rank=None, vec_rank=1),
]
CONTENTS = {
    1: "云端训练芯片出货量提升",
    2: "研发费用同比增长",
    3: "云端训练芯片采购合同",
}


def test_reranker_reorders_by_relevance() -> None:
    out = rerank_or_degrade(MockReranker(), "采购合同", FUSED, CONTENTS, top_k=3)
    assert out.order[0] == 3
    assert out.degraded is False


def test_top_k_is_respected() -> None:
    out = rerank_or_degrade(MockReranker(), "云端训练芯片", FUSED, CONTENTS, top_k=2)
    assert len(out.order) == 2


def test_failure_degrades_to_rrf_order() -> None:
    """研究场景下「慢」比「没有结果」更不可接受（06 §5）。"""
    out = rerank_or_degrade(FailingReranker(), "任意查询", FUSED, CONTENTS, top_k=3)
    assert out.order == [1, 2, 3]
    assert out.degraded is True


def test_degraded_scores_fall_back_to_rrf_scores() -> None:
    out = rerank_or_degrade(FailingReranker(), "任意", FUSED, CONTENTS, top_k=3)
    assert out.scores[1] == pytest.approx(0.03)


def test_empty_input_short_circuits_without_calling_the_model() -> None:
    out = rerank_or_degrade(FailingReranker(), "任意", [], {}, top_k=10)
    assert out.order == []
    assert out.degraded is False


def test_missing_content_is_treated_as_empty_not_a_crash() -> None:
    out = rerank_or_degrade(MockReranker(), "云端", FUSED, {1: "云端训练芯片"}, top_k=3)
    assert len(out.order) == 3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/retrieval/test_rerank.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.retrieval.rerank'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/retrieval/rerank.py`：

```python
"""重排与降级。

重排输入用**叶子块**的 content，不用父块——父块太长会超出重排模型上下文，
且稀释相关信号（docs/06-retrieval.md §5）。

降级发生率要监控：持续降级说明需要把重排模型本地化（adr/0004 的推翻条件）。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ragdemo.retrieval.fusion import FusedHit

logger = logging.getLogger(__name__)


@runtime_checkable
class Reranker(Protocol):
    model: str

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        """返回 (原索引, 相关度分)，按分数降序，长度 ≤ top_k。"""


@dataclass(frozen=True)
class RerankOutcome:
    order: list[int]
    scores: dict[int, float]
    degraded: bool


class MockReranker:
    """按字符重叠打分。确定性，供测试与离线开发。"""

    model = "mock-reranker"

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        query_chars = set(query)
        scored = [
            (i, len(query_chars & set(doc)) / max(len(query_chars), 1))
            for i, doc in enumerate(docs)
        ]
        scored.sort(key=lambda p: (-p[1], p[0]))
        return scored[:top_k]


class FailingReranker:
    """总是抛异常。用于验证降级路径。"""

    model = "failing"

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        raise RuntimeError("重排服务不可用")


def rerank_or_degrade(
    reranker: Reranker,
    query: str,
    fused: Sequence[FusedHit],
    contents: Mapping[int, str],
    top_k: int,
) -> RerankOutcome:
    """重排失败时回退到 RRF 顺序，并标记 degraded。"""
    if not fused:
        return RerankOutcome(order=[], scores={}, degraded=False)

    block_ids = [f.block_id for f in fused]
    docs = [contents.get(b, "") for b in block_ids]

    try:
        ranked = reranker.rerank(query, docs, top_k)
    except Exception:  # noqa: BLE001 — 任何失败都必须降级，不能让查询挂掉
        logger.warning("rerank_degraded", extra={"model": reranker.model, "n": len(fused)})
        head = list(fused)[:top_k]
        return RerankOutcome(
            order=[f.block_id for f in head],
            scores={f.block_id: f.score for f in head},
            degraded=True,
        )

    return RerankOutcome(
        order=[block_ids[i] for i, _ in ranked],
        scores={block_ids[i]: score for i, score in ranked},
        degraded=False,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/retrieval/test_rerank.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/retrieval/rerank.py tests/retrieval/test_rerank.py
git commit -m "feat(retrieval): 重排与失败降级"
```

---

## Task 6: 父子块展开与去重

**Files:**
- Create: `src/ragdemo/retrieval/expand.py`, `tests/retrieval/test_expand.py`

**Interfaces:**
- Consumes: Task 5 的 `RerankOutcome`
- Produces:
  - `expand_to_evidence(conn, order, scores, *, reranked, top_k) -> list[EvidenceBlock]`

- [ ] **Step 1: 写失败的测试**

`tests/retrieval/test_expand.py`：

```python
"""父子块展开：引用用叶子块 id，内容用父块；同父块只返回一次。"""
from __future__ import annotations

import psycopg
import pytest

from ragdemo.retrieval.expand import expand_to_evidence


def _leaf_ids(conn: psycopg.Connection, doc_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT block_id FROM core.doc_block WHERE doc_id = %s AND is_leaf"
        " AND parent_block_id IS NOT NULL ORDER BY ordinal",
        (doc_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


@pytest.mark.db
def test_content_comes_from_the_parent_block(corpus: psycopg.Connection) -> None:
    """生成要完整上下文，所以给父块内容。"""
    leaf = _leaf_ids(corpus, 1)[0]
    evidence = expand_to_evidence(corpus, [leaf], {leaf: 1.0}, reranked=True, top_k=10)
    assert len(evidence) == 1
    assert "研发费用" in evidence[0].content, "父块应包含同小节的其他叶子内容"


@pytest.mark.db
def test_citation_uses_the_leaf_block_id(corpus: psycopg.Connection) -> None:
    """溯源要精确到段落，指到整个小节等于没指。"""
    leaf = _leaf_ids(corpus, 1)[0]
    evidence = expand_to_evidence(corpus, [leaf], {leaf: 1.0}, reranked=True, top_k=10)
    assert evidence[0].block_id == leaf
    assert evidence[0].parent_block_id is not None
    assert evidence[0].parent_block_id != leaf


@pytest.mark.db
def test_same_parent_is_returned_once_with_all_matched_children(
    corpus: psycopg.Connection,
) -> None:
    leaves = _leaf_ids(corpus, 1)
    assert len(leaves) >= 2
    scores = {leaves[0]: 0.9, leaves[1]: 0.5}
    evidence = expand_to_evidence(corpus, leaves[:2], scores, reranked=True, top_k=10)
    assert len(evidence) == 1
    assert set(evidence[0].matched_child_ids) == set(leaves[:2])
    assert evidence[0].score == 0.9, "取命中子块中的最高分"
    assert evidence[0].block_id == leaves[0], "block_id 取最高分的那个子块"


@pytest.mark.db
def test_table_block_uses_its_own_content(corpus: psycopg.Connection) -> None:
    """表格块没有父块，用自身内容。"""
    row = corpus.execute(
        "SELECT block_id FROM core.doc_block WHERE block_type = 'table' LIMIT 1"
    ).fetchone()
    assert row is not None
    table_id = int(row[0])
    evidence = expand_to_evidence(corpus, [table_id], {table_id: 1.0}, reranked=True, top_k=10)
    assert evidence[0].parent_block_id is None
    assert "业务分部" in evidence[0].content


@pytest.mark.db
def test_dedup_does_not_backfill_to_top_k(corpus: psycopg.Connection) -> None:
    """去重后不足 top_k 时不补位——宁可少给也不引入低相关证据（06 §6 规则 4）。"""
    leaves = _leaf_ids(corpus, 1)[:2]
    evidence = expand_to_evidence(
        corpus, leaves, {b: 1.0 for b in leaves}, reranked=True, top_k=10
    )
    assert len(evidence) == 1


@pytest.mark.db
def test_reranked_flag_is_propagated(corpus: psycopg.Connection) -> None:
    leaf = _leaf_ids(corpus, 1)[0]
    evidence = expand_to_evidence(corpus, [leaf], {leaf: 1.0}, reranked=False, top_k=10)
    assert evidence[0].reranked is False


@pytest.mark.db
def test_order_is_preserved(corpus: psycopg.Connection) -> None:
    table = corpus.execute(
        "SELECT block_id FROM core.doc_block WHERE block_type = 'table' LIMIT 1"
    ).fetchone()
    leaf = _leaf_ids(corpus, 1)[0]
    assert table is not None
    order = [int(table[0]), leaf]
    evidence = expand_to_evidence(
        corpus, order, {order[0]: 0.9, order[1]: 0.8}, reranked=True, top_k=10
    )
    assert [e.block_id for e in evidence] == order
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/retrieval/test_expand.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.retrieval.expand'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/retrieval/expand.py`：

```python
"""父子块展开与去重（docs/06-retrieval.md §6）。

检索命中叶子块，生成用父块内容，但**引用标注用叶子块 block_id**——
溯源要精确到段落，指到整个小节等于没指。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import psycopg

from ragdemo.retrieval.types import EvidenceBlock

_SQL = """
SELECT b.block_id,
       b.parent_block_id,
       COALESCE(p.content, b.content) AS content,
       b.doc_id,
       d.title,
       b.section_path,
       b.page,
       b.known_at
  FROM core.doc_block b
  JOIN core.document d ON d.doc_id = b.doc_id
  LEFT JOIN core.doc_block p ON p.block_id = b.parent_block_id
 WHERE b.block_id = ANY(%(block_ids)s)
"""


def expand_to_evidence(
    conn: psycopg.Connection,
    order: Sequence[int],
    scores: Mapping[int, float],
    *,
    reranked: bool,
    top_k: int,
) -> list[EvidenceBlock]:
    """按 order 展开为证据集。同一父块只出现一次，取其中最高分的子块作为引用。

    去重后不足 top_k 时不补位——宁可少给几条也不引入低相关证据。
    """
    if not order:
        return []

    rows = conn.execute(_SQL, {"block_ids": list(order)}).fetchall()
    by_id = {int(r[0]): r for r in rows}

    grouped: dict[int, list[int]] = {}
    group_order: list[int] = []
    for block_id in order:
        row = by_id.get(block_id)
        if row is None:
            continue
        # 有父块的按父块分组；表格块（无父块）自成一组
        key = int(row[1]) if row[1] is not None else -block_id
        if key not in grouped:
            grouped[key] = []
            group_order.append(key)
        grouped[key].append(block_id)

    evidence: list[EvidenceBlock] = []
    for key in group_order[:top_k]:
        members = grouped[key]
        best = max(members, key=lambda b: scores.get(b, 0.0))
        row = by_id[best]
        evidence.append(
            EvidenceBlock(
                block_id=best,
                parent_block_id=int(row[1]) if row[1] is not None else None,
                content=str(row[2]),
                matched_child_ids=list(members),
                doc_id=int(row[3]),
                doc_title=str(row[4]),
                section_path=str(row[5] or ""),
                page=int(row[6]) if row[6] is not None else None,
                known_at=row[7],
                score=scores.get(best, 0.0),
                reranked=reranked,
            )
        )
    return evidence
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/retrieval/test_expand.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/retrieval/expand.py tests/retrieval/test_expand.py
git commit -m "feat(retrieval): 父子块展开与同父块去重"
```

---

## Task 7: `RetrievalService` 编排与查询改写

**Files:**
- Create: `src/ragdemo/retrieval/rewrite.py`, `src/ragdemo/retrieval/service.py`, `config/synonyms.yaml`, `tests/retrieval/test_service.py`

**Interfaces:**
- Consumes: Task 1–6
- Produces:
  - `expand_synonyms(query: str, table: Mapping[str, list[str]]) -> list[str]`
  - `RetrievalService(conn, embedder, reranker, *, synonyms=None)`，方法 `search(req) -> RetrievalResult`

- [ ] **Step 1: 写失败的测试**

`tests/retrieval/test_service.py`：

```python
"""端到端检索：五个阶段串起来，统计齐全，时点正确。"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.retrieval.rerank import FailingReranker, MockReranker
from ragdemo.retrieval.rewrite import expand_synonyms
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest


def _service(conn: psycopg.Connection, reranker: object = None) -> RetrievalService:
    return RetrievalService(conn, MockEmbedder(), reranker or MockReranker())


@pytest.mark.db
def test_end_to_end_returns_evidence(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    result = _service(corpus).search(
        RetrievalRequest(query="云端训练芯片 收入", as_of=as_of_2024)
    )
    assert result.blocks
    assert all(b.block_id > 0 for b in result.blocks)
    assert all(b.known_at <= as_of_2024 for b in result.blocks)


@pytest.mark.db
def test_stats_are_populated_for_every_stage(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """stats 是排查「为什么没找到」的唯一依据，每次查询都要记（06 §8）。"""
    result = _service(corpus).search(RetrievalRequest(query="收入", as_of=as_of_2024))
    s = result.stats
    assert s.bm25_hits >= 0 and s.vec_hits > 0
    assert s.after_fusion > 0
    assert s.ms_total > 0
    assert s.after_rerank == len(result.blocks) or s.after_rerank >= len(result.blocks)


@pytest.mark.db
def test_future_document_never_appears(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus).search(
        RetrievalRequest(query="采购合同 8.5 亿元", as_of=as_of_2024)
    )
    assert all(b.doc_id != 2 for b in result.blocks)


@pytest.mark.db
def test_same_query_same_as_of_is_reproducible(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """可复现是引擎的核心承诺（00-overview §6 第 1 条）。"""
    svc = _service(corpus)
    req = RetrievalRequest(query="云端训练芯片", as_of=as_of_2024)
    first = [b.block_id for b in svc.search(req).blocks]
    second = [b.block_id for b in svc.search(req).blocks]
    assert first == second


@pytest.mark.db
def test_rerank_failure_degrades_but_still_returns(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    result = _service(corpus, FailingReranker()).search(
        RetrievalRequest(query="收入", as_of=as_of_2024)
    )
    assert result.blocks
    assert result.stats.degraded is True
    assert all(b.reranked is False for b in result.blocks)


@pytest.mark.db
def test_rerank_can_be_disabled(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus, FailingReranker()).search(
        RetrievalRequest(
            query="收入", as_of=as_of_2024, config=RetrievalConfig(rerank_enabled=False)
        )
    )
    assert result.blocks
    assert result.stats.ms_rerank == 0.0


@pytest.mark.db
def test_top_k_is_respected(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus).search(
        RetrievalRequest(query="收入", as_of=as_of_2024, config=RetrievalConfig(top_k=1))
    )
    assert len(result.blocks) <= 1


def test_synonym_expansion_adds_variants() -> None:
    table = {"算力": ["计算力", "compute"]}
    assert set(expand_synonyms("算力需求", table)) == {"算力需求", "计算力需求", "compute需求"}


def test_synonym_expansion_is_noop_without_matches() -> None:
    assert expand_synonyms("寒武纪业绩", {"算力": ["计算力"]}) == ["寒武纪业绩"]


@pytest.mark.db
def test_rewrite_is_off_by_default(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    """默认关闭是有意的：改写增加延迟与成本，收益必须由评测证明（06 §7）。"""
    assert RetrievalConfig().rewrite_enabled is False
    result = _service(corpus).search(RetrievalRequest(query="算力", as_of=as_of_2024))
    assert result.stats.ms_total > 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/retrieval/test_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.retrieval.rewrite'`

- [ ] **Step 3: 写最小实现**

`config/synonyms.yaml`：

```yaml
# 检索同义词表。只在 RetrievalConfig.rewrite_enabled = true 时生效。
算力: [计算力, compute]
先进封装: [CoWoS, 2.5D 封装]
大模型: [基础模型, foundation model]
推理芯片: [推理加速卡, inference chip]
训练芯片: [训练加速卡, training chip]
```

`src/ragdemo/retrieval/rewrite.py`：

```python
"""查询改写。默认关闭。

开关默认关闭是有意的：改写增加延迟与成本，它是否提升召回必须由评测证明，
不能想当然。`eval_retrieval` 上开关两次跑对比，提升超过 2 个百分点才值得
默认开启（docs/06-retrieval.md §7）。
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

DEFAULT_SYNONYMS_PATH = Path("config/synonyms.yaml")


def load_synonyms(path: Path = DEFAULT_SYNONYMS_PATH) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): [str(v) for v in vs] for k, vs in raw.items()}


def expand_synonyms(query: str, table: Mapping[str, list[str]]) -> list[str]:
    """返回原查询加上同义词替换后的变体。原查询永远在第一位。"""
    variants = [query]
    for term, alternatives in table.items():
        if term in query:
            variants.extend(query.replace(term, alt) for alt in alternatives)
    seen: set[str] = set()
    return [v for v in variants if not (v in seen or seen.add(v))]
```

`src/ragdemo/retrieval/service.py`：

```python
"""检索编排：过滤 → 双路召回 → 加权 RRF → 重排 → 父子块展开。

查询走 asof 视图（正确性）+ 显式时点谓词（可下推），见 docs/06-retrieval.md §3.4。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping

import psycopg

from ragdemo.db.session import as_of_session
from ragdemo.embed.base import Embedder
from ragdemo.retrieval.expand import expand_to_evidence
from ragdemo.retrieval.fusion import weighted_rrf
from ragdemo.retrieval.lexical import bm25_search
from ragdemo.retrieval.rerank import Reranker, rerank_or_degrade
from ragdemo.retrieval.rewrite import expand_synonyms
from ragdemo.retrieval.types import (
    RetrievalRequest,
    RetrievalResult,
    RetrievalStats,
)
from ragdemo.retrieval.vector import apply_scan_settings, vector_search

logger = logging.getLogger(__name__)
_VARIANT_WEIGHT = 0.5


class RetrievalService:
    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder,
        reranker: Reranker,
        *,
        synonyms: Mapping[str, list[str]] | None = None,
    ) -> None:
        self.conn = conn
        self.embedder = embedder
        self.reranker = reranker
        self.synonyms = dict(synonyms or {})

    def search(self, req: RetrievalRequest) -> RetrievalResult:
        cfg = req.config
        started = time.perf_counter()

        with as_of_session(self.conn, req.as_of, tenant=req.tenant, user=req.user) as conn:
            apply_scan_settings(conn, cfg)

            queries = (
                expand_synonyms(req.query, self.synonyms)
                if cfg.rewrite_enabled
                else [req.query]
            )

            t0 = time.perf_counter()
            bm25 = bm25_search(conn, req)
            ms_bm25 = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            qvec = self.embedder.embed([req.query])[0]
            vec = vector_search(conn, req, qvec)
            ms_vec = (time.perf_counter() - t0) * 1000

            fused = weighted_rrf(
                bm25, vec,
                w_bm25=cfg.w_bm25, w_vec=cfg.w_vec, rrf_k=cfg.rrf_k, limit=cfg.fusion_k,
            )

            for variant in queries[1:]:
                variant_req = _with_query(req, variant)
                extra = weighted_rrf(
                    bm25_search(conn, variant_req),
                    vector_search(conn, variant_req, self.embedder.embed([variant])[0]),
                    w_bm25=cfg.w_bm25 * _VARIANT_WEIGHT,
                    w_vec=cfg.w_vec * _VARIANT_WEIGHT,
                    rrf_k=cfg.rrf_k,
                    limit=cfg.fusion_k,
                )
                fused = _merge(fused, extra, cfg.fusion_k)

            contents = _leaf_contents(conn, [f.block_id for f in fused])

            ms_rerank = 0.0
            if cfg.rerank_enabled and fused:
                t0 = time.perf_counter()
                outcome = rerank_or_degrade(
                    self.reranker, req.query, fused, contents, cfg.top_k
                )
                ms_rerank = (time.perf_counter() - t0) * 1000
            else:
                head = fused[: cfg.top_k]
                outcome = rerank_or_degrade.__wrapped__ if False else None  # type: ignore[assignment]
                from ragdemo.retrieval.rerank import RerankOutcome

                outcome = RerankOutcome(
                    order=[f.block_id for f in head],
                    scores={f.block_id: f.score for f in head},
                    degraded=False,
                )

            blocks = expand_to_evidence(
                conn, outcome.order, outcome.scores,
                reranked=cfg.rerank_enabled and not outcome.degraded,
                top_k=cfg.top_k,
            )

        stats = RetrievalStats(
            bm25_hits=len(bm25),
            vec_hits=len(vec),
            after_fusion=len(fused),
            after_rerank=len(outcome.order),
            ms_bm25=ms_bm25,
            ms_vec=ms_vec,
            ms_rerank=ms_rerank,
            ms_total=(time.perf_counter() - started) * 1000,
            degraded=outcome.degraded,
        )
        logger.info(
            "retrieval",
            extra={
                "as_of": req.as_of.isoformat(),
                "entity_id": req.entity_ids,
                "query_len": len(req.query),
                "stats": stats,
            },
        )
        return RetrievalResult(blocks=blocks, stats=stats)


def _with_query(req: RetrievalRequest, query: str) -> RetrievalRequest:
    return RetrievalRequest(
        query=query, as_of=req.as_of, entity_ids=req.entity_ids,
        doc_types=req.doc_types, published_after=req.published_after,
        tenant=req.tenant, user=req.user, config=req.config,
    )


def _merge(primary: list, extra: list, limit: int) -> list:  # type: ignore[type-arg]
    from ragdemo.retrieval.fusion import FusedHit

    scores: dict[int, float] = {}
    ranks: dict[int, tuple[int | None, int | None]] = {}
    for hit in [*primary, *extra]:
        scores[hit.block_id] = scores.get(hit.block_id, 0.0) + hit.score
        ranks.setdefault(hit.block_id, (hit.bm25_rank, hit.vec_rank))
    merged = [
        FusedHit(block_id=b, score=s, bm25_rank=ranks[b][0], vec_rank=ranks[b][1])
        for b, s in scores.items()
    ]
    merged.sort(key=lambda f: (-f.score, f.block_id))
    return merged[:limit]


def _leaf_contents(conn: psycopg.Connection, block_ids: list[int]) -> dict[int, str]:
    if not block_ids:
        return {}
    rows = conn.execute(
        "SELECT block_id, content FROM core.doc_block WHERE block_id = ANY(%s)",
        (block_ids,),
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}
```

> **实现提示**：上面 `search()` 中 `rerank_enabled = False` 分支的写法故意留成
> 直白的构造 `RerankOutcome`。实现时把那两行临时占位删掉，直接构造即可——
> 不要引入额外的间接层。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/retrieval -v && make typecheck`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/retrieval/service.py src/ragdemo/retrieval/rewrite.py config/synonyms.yaml tests/retrieval/test_service.py
git commit -m "feat(retrieval): RetrievalService 编排与默认关闭的查询改写"
```

---

## Task 8: 评测框架与 `eval_run`

**Files:**
- Create: `src/ragdemo/evals/__init__.py`, `src/ragdemo/evals/runner.py`, `src/ragdemo/evals/retrieval_metrics.py`, `tests/evals/__init__.py`, `tests/evals/test_metrics.py`, `tests/evals/test_runner.py`

**Interfaces:**
- Consumes: Task 7 的 `RetrievalService`；P0 的 `evals.eval_retrieval` / `evals.eval_run`
- Produces:
  - `recall_at_k(retrieved, gold, k) -> float`、`mrr_at_k(retrieved, gold, k) -> float`
  - `RetrievalEvalResult(recall_at_10, recall_at_50, mrr_at_10, n_cases, per_difficulty)`
  - `run_retrieval_eval(conn, service, *, git_sha, config) -> RetrievalEvalResult`
  - `record_eval_run(conn, *, run_id, suite, git_sha, config, metrics, started_at, ended_at) -> None`

- [ ] **Step 1: 写失败的测试**

`tests/evals/__init__.py`：空文件。

`tests/evals/test_metrics.py`：

```python
"""检索指标：recall@k 与 MRR 的定义必须和 06 §9.1 一致。"""
from __future__ import annotations

import pytest

from ragdemo.evals.retrieval_metrics import mrr_at_k, recall_at_k


def test_recall_is_binary_per_question() -> None:
    """top-k 中包含至少一个 gold 即算命中——不是 gold 的覆盖比例。"""
    assert recall_at_k([1, 2, 3], gold={3, 99}, k=3) == 1.0
    assert recall_at_k([1, 2, 3], gold={99}, k=3) == 0.0


def test_recall_respects_k() -> None:
    assert recall_at_k([1, 2, 3], gold={3}, k=2) == 0.0
    assert recall_at_k([1, 2, 3], gold={3}, k=3) == 1.0


def test_mrr_uses_first_correct_position() -> None:
    assert mrr_at_k([9, 8, 3], gold={3}, k=10) == pytest.approx(1 / 3)
    assert mrr_at_k([3, 8, 9], gold={3}, k=10) == 1.0


def test_mrr_is_zero_when_nothing_hits() -> None:
    assert mrr_at_k([1, 2], gold={99}, k=10) == 0.0


def test_empty_retrieval_scores_zero() -> None:
    assert recall_at_k([], gold={1}, k=10) == 0.0
    assert mrr_at_k([], gold={1}, k=10) == 0.0


def test_empty_gold_is_a_programming_error() -> None:
    """eval_retrieval.gold_block_ids 有 CHECK 约束，不该出现空 gold。"""
    with pytest.raises(ValueError, match="gold"):
        recall_at_k([1], gold=set(), k=10)
```

`tests/evals/test_runner.py`：

```python
"""评测框架：跑完写一行 eval_run，含 git_sha 与完整 config。"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.evals.runner import run_retrieval_eval
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig


def _seed_cases(conn: psycopg.Connection) -> None:
    gold = conn.execute(
        "SELECT block_id FROM core.doc_block WHERE content LIKE '%云端训练芯片%'"
        " AND is_leaf LIMIT 1"
    ).fetchone()
    assert gold is not None
    conn.execute(
        "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids,"
        " entity_filter, difficulty, author) "
        "VALUES ('寒武纪三季度云端训练芯片业务表现如何？', %s, %s, %s, 'easy', 'founder')",
        (datetime(2024, 12, 31, tzinfo=UTC), [int(gold[0])], ["CN.688256"]),
    )
    conn.commit()


@pytest.mark.db
def test_eval_computes_metrics(corpus: psycopg.Connection) -> None:
    _seed_cases(corpus)
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    result = run_retrieval_eval(corpus, service, git_sha="abc1234", config=RetrievalConfig())
    assert result.n_cases == 1
    assert 0.0 <= result.recall_at_10 <= 1.0


@pytest.mark.db
def test_eval_writes_a_run_row_with_sha_and_config(corpus: psycopg.Connection) -> None:
    """没有这张表，三个月后看到指标下降无法定位是哪个参数改的（08 §4.3）。"""
    _seed_cases(corpus)
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    run_retrieval_eval(
        corpus, service, git_sha="abc1234", config=RetrievalConfig(w_bm25=0.7, w_vec=0.3)
    )
    row = corpus.execute(
        "SELECT suite, git_sha, config, metrics FROM evals.eval_run ORDER BY started_at DESC"
        " LIMIT 1"
    ).fetchone()
    assert row is not None
    suite, git_sha, config, metrics = row
    assert suite == "retrieval"
    assert git_sha == "abc1234"
    assert config["w_bm25"] == 0.7
    assert "recall_at_10" in metrics


@pytest.mark.db
def test_eval_reports_per_difficulty(corpus: psycopg.Connection) -> None:
    _seed_cases(corpus)
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    result = run_retrieval_eval(corpus, service, git_sha="x", config=RetrievalConfig())
    assert "easy" in result.per_difficulty


@pytest.mark.db
def test_empty_eval_set_raises_rather_than_reporting_perfect_score(
    corpus: psycopg.Connection,
) -> None:
    """空评测集算出 1.0 会让 CI 门禁形同虚设。"""
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    with pytest.raises(ValueError, match="评测集为空"):
        run_retrieval_eval(corpus, service, git_sha="x", config=RetrievalConfig())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/evals -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.evals'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/evals/__init__.py`：

```python
"""评测。三套评测集的指标计算与运行记录。"""
```

`src/ragdemo/evals/retrieval_metrics.py`：

```python
"""检索指标（docs/06-retrieval.md §9.1）。

recall@k 是**逐题二值**的：top-k 中包含至少一个 gold 即算命中。
不是 gold 的覆盖比例——同一事实常出现在多处，要求全覆盖会低估系统。
"""
from __future__ import annotations

from collections.abc import Sequence


def _require_gold(gold: set[int]) -> None:
    if not gold:
        raise ValueError("gold_block_ids 为空；评测用例必须至少有一个正确答案")


def recall_at_k(retrieved: Sequence[int], gold: set[int], k: int) -> float:
    _require_gold(gold)
    return 1.0 if set(retrieved[:k]) & gold else 0.0


def mrr_at_k(retrieved: Sequence[int], gold: set[int], k: int) -> float:
    _require_gold(gold)
    for position, block_id in enumerate(retrieved[:k], start=1):
        if block_id in gold:
            return 1.0 / position
    return 0.0
```

`src/ragdemo/evals/runner.py`：

```python
"""评测运行器。

每次运行写一行 evals.eval_run，含 git_sha 与完整 config。
没有这张表，三个月后看到指标下降无法定位是哪个参数改的（docs/08-evaluation.md §4.3）。
"""
from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ragdemo.evals.retrieval_metrics import mrr_at_k, recall_at_k
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest


@dataclass(frozen=True)
class RetrievalEvalResult:
    recall_at_10: float
    recall_at_50: float
    mrr_at_10: float
    n_cases: int
    per_difficulty: dict[str, float]

    def as_metrics(self) -> dict[str, Any]:
        return {
            "recall_at_10": self.recall_at_10,
            "recall_at_50": self.recall_at_50,
            "mrr_at_10": self.mrr_at_10,
            "n_cases": self.n_cases,
            "per_difficulty": self.per_difficulty,
        }


def record_eval_run(
    conn: psycopg.Connection,
    *,
    run_id: str,
    suite: str,
    git_sha: str,
    config: Mapping[str, Any],
    metrics: Mapping[str, Any],
    started_at: datetime,
    ended_at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO evals.eval_run (run_id, suite, git_sha, config, metrics,"
        " started_at, ended_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (run_id, suite, git_sha, Jsonb(dict(config)), Jsonb(dict(metrics)),
         started_at, ended_at),
    )
    conn.commit()


def run_retrieval_eval(
    conn: psycopg.Connection,
    service: RetrievalService,
    *,
    git_sha: str,
    config: RetrievalConfig,
) -> RetrievalEvalResult:
    started_at = datetime.now(UTC)
    cases = conn.execute(
        "SELECT q_id, question, as_of, gold_block_ids, entity_filter, doc_type_filter,"
        " difficulty FROM evals.eval_retrieval ORDER BY q_id"
    ).fetchall()
    if not cases:
        raise ValueError("评测集为空；空集会算出满分，让 CI 门禁形同虚设")

    wide = dataclasses.replace(config, top_k=min(config.fusion_k, 50), rerank_enabled=False)
    hits10: list[float] = []
    hits50: list[float] = []
    mrrs: list[float] = []
    by_difficulty: dict[str, list[float]] = {}

    for _q_id, question, as_of, gold, entity_filter, doc_type_filter, difficulty in cases:
        gold_set = {int(g) for g in gold}

        top = service.search(
            RetrievalRequest(
                query=str(question), as_of=as_of,
                entity_ids=list(entity_filter) if entity_filter else None,
                doc_types=list(doc_type_filter) if doc_type_filter else None,
                config=config,
            )
        )
        ordered10 = [b.block_id for b in top.blocks]
        hits10.append(recall_at_k(ordered10, gold_set, 10))
        mrrs.append(mrr_at_k(ordered10, gold_set, 10))

        broad = service.search(
            RetrievalRequest(
                query=str(question), as_of=as_of,
                entity_ids=list(entity_filter) if entity_filter else None,
                doc_types=list(doc_type_filter) if doc_type_filter else None,
                config=wide,
            )
        )
        hits50.append(recall_at_k([b.block_id for b in broad.blocks], gold_set, 50))

        by_difficulty.setdefault(str(difficulty or "unknown"), []).append(hits10[-1])

    result = RetrievalEvalResult(
        recall_at_10=sum(hits10) / len(hits10),
        recall_at_50=sum(hits50) / len(hits50),
        mrr_at_10=sum(mrrs) / len(mrrs),
        n_cases=len(cases),
        per_difficulty={k: sum(v) / len(v) for k, v in by_difficulty.items()},
    )

    record_eval_run(
        conn,
        run_id=f"eval-{uuid.uuid4().hex[:12]}",
        suite="retrieval",
        git_sha=git_sha,
        config=dataclasses.asdict(config),
        metrics=result.as_metrics(),
        started_at=started_at,
        ended_at=datetime.now(UTC),
    )
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/evals -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/evals tests/evals
git commit -m "feat(evals): 检索评测框架与 eval_run 记录"
```

---

## Task 9: 评测集录入 CLI 与 CI 门禁

**Files:**
- Create: `src/ragdemo/evals/cli.py`, `.github/workflows/eval.yml`, `tests/evals/test_cli.py`
- Modify: `src/ragdemo/cli.py`, `Makefile`

**Interfaces:**
- Consumes: Task 8
- Produces:
  - `ragdemo eval add-retrieval` / `ragdemo eval run --suite retrieval`
  - `Makefile` 目标 `eval-retrieval`
  - `compare_to_baseline(conn, suite, metric, current, tolerance) -> tuple[bool, float | None]`

- [ ] **Step 1: 写失败的测试**

`tests/evals/test_cli.py`：

```python
"""评测 CLI 与基线比对。"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from click.testing import CliRunner
from psycopg.types.json import Jsonb

from ragdemo.evals.cli import compare_to_baseline
from ragdemo.cli import main


def test_eval_subcommands_exist() -> None:
    result = CliRunner().invoke(main, ["eval", "--help"])
    assert result.exit_code == 0
    assert "add-retrieval" in result.output
    assert "run" in result.output


def _record(conn: psycopg.Connection, sha: str, recall: float) -> None:
    conn.execute(
        "INSERT INTO evals.eval_run (run_id, suite, git_sha, config, metrics,"
        " started_at, ended_at) VALUES (%s,'retrieval',%s,%s,%s,%s,%s)",
        (f"r-{sha}", sha, Jsonb({}), Jsonb({"recall_at_10": recall}),
         datetime.now(UTC), datetime.now(UTC)),
    )
    conn.commit()


@pytest.mark.db
def test_no_baseline_passes_the_gate(corpus: psycopg.Connection) -> None:
    """首次运行没有基线，不该因此阻断合并。"""
    ok, baseline = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.5, 0.02)
    assert ok is True
    assert baseline is None


@pytest.mark.db
def test_small_regression_within_tolerance_passes(corpus: psycopg.Connection) -> None:
    _record(corpus, "base", 0.85)
    ok, baseline = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.84, 0.02)
    assert ok is True
    assert baseline == pytest.approx(0.85)


@pytest.mark.db
def test_regression_beyond_tolerance_fails(corpus: psycopg.Connection) -> None:
    """08 §4.1：不得低于主干基线 0.02。"""
    _record(corpus, "base", 0.85)
    ok, _ = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.80, 0.02)
    assert ok is False


@pytest.mark.db
def test_improvement_passes(corpus: psycopg.Connection) -> None:
    _record(corpus, "base", 0.85)
    ok, _ = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.90, 0.02)
    assert ok is True


@pytest.mark.db
def test_baseline_is_the_most_recent_run(corpus: psycopg.Connection) -> None:
    _record(corpus, "old", 0.60)
    _record(corpus, "new", 0.85)
    _, baseline = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.84, 0.02)
    assert baseline == pytest.approx(0.85)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/evals/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.evals.cli'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/evals/cli.py`：

```python
"""评测命令与 CI 门禁比对。

CLI 而非 Web：P1–P3 无前端（adr/0006）。标注工具要当正经工具做，
否则标注 100 条评测集会变成瓶颈。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

import click
import psycopg

from ragdemo.embed.mock import MockEmbedder
from ragdemo.evals.runner import run_retrieval_eval
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest

TOLERANCE = 0.02


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置")
    return dsn


def _git_sha() -> str:
    try:
        return subprocess.check_output(  # noqa: S603, S607
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def compare_to_baseline(
    conn: psycopg.Connection, suite: str, metric: str, current: float, tolerance: float
) -> tuple[bool, float | None]:
    """与最近一次同 suite 的运行比。没有基线时放行——首次运行不该阻断合并。"""
    row = conn.execute(
        "SELECT (metrics ->> %s)::float FROM evals.eval_run "
        " WHERE suite = %s AND metrics ? %s ORDER BY started_at DESC LIMIT 1",
        (metric, suite, metric),
    ).fetchone()
    if row is None or row[0] is None:
        return True, None
    baseline = float(row[0])
    return current >= baseline - tolerance, baseline


@click.group("eval")
def eval_group() -> None:
    """评测集录入与运行。"""


@eval_group.command("add-retrieval")
@click.option("--question", required=True, help="评测问题")
@click.option("--as-of", "as_of_raw", required=True, help="ISO 8601 带时区，如 2024-08-01T00:00+08:00")
@click.option("--entity", "entities", multiple=True, help="实体过滤，可多次指定")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
@click.option("--author", required=True)
def add_retrieval(
    question: str, as_of_raw: str, entities: tuple[str, ...], difficulty: str, author: str
) -> None:
    """交互式录入一条检索评测用例：跑一次检索，选出正确的块。"""
    as_of = datetime.fromisoformat(as_of_raw)
    if as_of.tzinfo is None:
        raise click.ClickException("--as-of 必须带时区")

    with psycopg.connect(_dsn()) as conn:
        service = RetrievalService(conn, MockEmbedder(), MockReranker())
        result = service.search(
            RetrievalRequest(
                query=question, as_of=as_of, entity_ids=list(entities) or None
            )
        )
        if not result.blocks:
            raise click.ClickException("检索没有返回任何候选，无法标注")

        for i, block in enumerate(result.blocks):
            click.echo(f"[{i}] block={block.block_id} p{block.page} {block.doc_title}")
            click.echo(f"     {block.content[:120].replace(chr(10), ' ')}")

        picked = click.prompt("正确的块序号（逗号分隔，可多选）", type=str)
        gold = [result.blocks[int(i.strip())].block_id for i in picked.split(",") if i.strip()]
        if not gold:
            raise click.ClickException("至少要选一个正确答案")

        conn.execute(
            "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids,"
            " entity_filter, difficulty, author) VALUES (%s,%s,%s,%s,%s,%s)",
            (question, as_of, gold, list(entities) or None, difficulty, author),
        )
        conn.commit()
    click.echo(f"已录入，gold_block_ids = {gold}")


@eval_group.command("run")
@click.option("--suite", type=click.Choice(["retrieval"]), default="retrieval")
@click.option("--gate/--no-gate", default=False, help="与基线比对，回退超容差则以非零码退出")
def run(suite: str, gate: bool) -> None:
    """跑评测并写 eval_run。"""
    with psycopg.connect(_dsn()) as conn:
        service = RetrievalService(conn, MockEmbedder(), MockReranker())
        result = run_retrieval_eval(
            conn, service, git_sha=_git_sha(), config=RetrievalConfig()
        )
        click.echo(f"| 指标 | 本次 |")
        click.echo(f"|---|---|")
        click.echo(f"| recall@10 | {result.recall_at_10:.4f} |")
        click.echo(f"| recall@50 | {result.recall_at_50:.4f} |")
        click.echo(f"| MRR@10 | {result.mrr_at_10:.4f} |")
        click.echo(f"| 用例数 | {result.n_cases} |")

        if gate:
            ok, baseline = compare_to_baseline(
                conn, suite, "recall_at_10", result.recall_at_10, TOLERANCE
            )
            if baseline is not None:
                click.echo(f"基线 recall@10 = {baseline:.4f}，容差 {TOLERANCE}")
            if not ok:
                click.echo("回退超出容差，阻断合并", err=True)
                sys.exit(1)
```

`src/ragdemo/cli.py` 中注册子命令组（在 `main` 定义之后）：

```python
from ragdemo.evals.cli import eval_group

main.add_command(eval_group)
```

`.github/workflows/eval.yml`：

```yaml
name: eval
on: [pull_request]
jobs:
  retrieval:
    runs-on: ubuntu-latest
    services:
      paradedb:
        image: paradedb/paradedb:0.25.9-pg18
        env:
          POSTGRES_PASSWORD: ragdemo
          POSTGRES_DB: ragdemo
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U postgres -d ragdemo"
          --health-interval 5s --health-retries 20
    env:
      RAGDEMO_DSN: postgresql://postgres:ragdemo@localhost:5432/ragdemo
      RAGDEMO_ADMIN_DSN: postgresql://postgres:ragdemo@localhost:5432/postgres
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: ragdemo db migrate
      - run: pytest -v -m db
      - run: ragdemo eval run --suite retrieval --gate | tee eval.md
      - run: cat eval.md >> "$GITHUB_STEP_SUMMARY"
```

`Makefile` 追加：

```makefile
.PHONY: eval-retrieval

eval-retrieval:
	ragdemo eval run --suite retrieval --gate
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/evals/test_cli.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/evals/cli.py src/ragdemo/cli.py .github/workflows/eval.yml Makefile tests/evals/test_cli.py
git commit -m "feat(evals): 评测录入 CLI、基线比对与 CI 门禁"
```

---

## Task 10: 索引召回率与双跑一致性

这两条是 P1 验收里最容易被跳过、也最能暴露真问题的检查。

**Files:**
- Create: `src/ragdemo/evals/index_recall.py`, `tests/evals/test_index_recall.py`, `tests/test_p1_acceptance.py`
- Modify: `Makefile`, `.github/workflows/eval.yml`（nightly job）

**Interfaces:**
- Consumes: Task 3/8
- Produces:
  - `exact_vector_search(conn, req, query_vector, limit) -> list[int]`（强制精确扫描）
  - `index_recall(conn, service, embedder, cases) -> float`
  - `Makefile` 目标 `accept-p1`

- [ ] **Step 1: 写失败的测试**

`tests/evals/test_index_recall.py`：

```python
"""索引召回率：索引路径相对精确扫描的召回（06 §3.6，阈值 ≥ 0.95）。"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.evals.index_recall import exact_vector_search, index_recall
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalRequest

AS_OF = datetime(2024, 12, 31, tzinfo=UTC)


@pytest.mark.db
def test_exact_scan_returns_results(corpus: psycopg.Connection) -> None:
    qvec = MockEmbedder().embed(["云端训练芯片"])[0]
    ids = exact_vector_search(
        corpus, RetrievalRequest(query="云端训练芯片", as_of=AS_OF), qvec, limit=10
    )
    assert ids


@pytest.mark.db
def test_exact_scan_respects_the_time_point(corpus: psycopg.Connection) -> None:
    qvec = MockEmbedder().embed(["采购合同"])[0]
    early = exact_vector_search(
        corpus, RetrievalRequest(query="采购合同", as_of=AS_OF), qvec, limit=10
    )
    late = exact_vector_search(
        corpus,
        RetrievalRequest(query="采购合同", as_of=datetime(2025, 6, 1, tzinfo=UTC)),
        qvec,
        limit=10,
    )
    assert len(late) > len(early)


@pytest.mark.db
def test_index_recall_is_one_on_a_tiny_corpus(corpus: psycopg.Connection) -> None:
    """小语料下索引与精确扫描应完全一致。差异出现在大语料上。"""
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    cases = [RetrievalRequest(query=q, as_of=AS_OF) for q in ("云端训练芯片", "研发费用", "收入")]
    assert index_recall(corpus, service, MockEmbedder(), cases) == pytest.approx(1.0)
```

`tests/test_p1_acceptance.py`：

```python
"""P1 验收：docs/10-roadmap.md P1 表格的九项。

跑绿 = P1 可以收。跑红 = 不能进 P2。
双跑一致性（第 6 项）需要间隔一周，单独用 --run-consistency 触发。
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.db.invariants import check_point_in_time_leaks
from ragdemo.embed.mock import MockEmbedder
from ragdemo.evals.runner import run_retrieval_eval
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest

DSN = os.environ.get("RAGDEMO_DSN", "postgresql://postgres:ragdemo@localhost:5432/ragdemo")
SNAPSHOT = Path(".superpowers/p1-consistency-snapshot.json")
PROBE_AS_OF = datetime(2024, 12, 31, tzinfo=UTC)


@pytest.fixture(scope="module")
def conn() -> psycopg.Connection:
    return psycopg.connect(DSN)


@pytest.fixture(scope="module")
def service(conn: psycopg.Connection) -> RetrievalService:
    return RetrievalService(conn, MockEmbedder(), MockReranker())


@pytest.mark.db
def test_recall_at_10_above_threshold(conn: psycopg.Connection, service: RetrievalService) -> None:
    result = run_retrieval_eval(conn, service, git_sha="accept", config=RetrievalConfig())
    assert result.recall_at_10 > 0.80


@pytest.mark.db
def test_recall_at_50_above_threshold(conn: psycopg.Connection, service: RetrievalService) -> None:
    result = run_retrieval_eval(conn, service, git_sha="accept", config=RetrievalConfig())
    assert result.recall_at_50 > 0.92


@pytest.mark.db
def test_p95_latency_under_3s(conn: psycopg.Connection, service: RetrievalService) -> None:
    cases = conn.execute(
        "SELECT question, as_of FROM evals.eval_retrieval ORDER BY q_id"
    ).fetchall()
    assert cases, "评测集为空，无法测延迟"
    latencies = sorted(
        service.search(RetrievalRequest(query=str(q), as_of=a)).stats.ms_total
        for q, a in cases
    )
    p95 = latencies[int(len(latencies) * 0.95) - 1] if len(latencies) > 1 else latencies[0]
    assert p95 < 3000


@pytest.mark.db
def test_no_point_in_time_leaks(conn: psycopg.Connection) -> None:
    assert check_point_in_time_leaks(conn, PROBE_AS_OF) == {}


@pytest.mark.db
def test_bm25_filters_are_pushed_into_the_index(conn: psycopg.Connection) -> None:
    """P1 验收：EXPLAIN 验证过滤条件在索引扫描节点内。"""
    from ragdemo.retrieval.lexical import explain_bm25

    plan = explain_bm25(
        conn, RetrievalRequest(query="云端训练芯片", as_of=PROBE_AS_OF,
                               entity_ids=["CN.688256"])
    )
    assert "Seq Scan on doc_block" not in plan, f"退化成顺序扫描:\n{plan}"


@pytest.mark.db
def test_double_run_consistency(service: RetrievalService) -> None:
    """间隔一周跑两次同一个 as_of，证据集与数值必须完全一致（03 §5.1）。

    首次运行写快照并 skip；一周后再跑，比对。
    """
    req = RetrievalRequest(query="云端训练芯片业务表现", as_of=PROBE_AS_OF)
    current = [b.block_id for b in service.search(req).blocks]

    if not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps({"taken_at": datetime.now(UTC).isoformat(), "block_ids": current}),
            encoding="utf-8",
        )
        pytest.skip(f"已写入基线快照 {SNAPSHOT}；一周后重跑本测试完成验收")

    baseline = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    taken_at = datetime.fromisoformat(baseline["taken_at"])
    elapsed_days = (datetime.now(UTC) - taken_at).days
    assert elapsed_days >= 7, f"快照仅 {elapsed_days} 天，需间隔至少 7 天"
    assert current == baseline["block_ids"], "同一 as_of 两次结果不一致——存在时点泄漏"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/evals/test_index_recall.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.evals.index_recall'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/evals/index_recall.py`：

```python
"""索引召回率验证（docs/06-retrieval.md §3.6）。

不能假设迭代扫描与谓词下推有效，必须测：把索引路径的结果和强制精确扫描的
结果比。阈值 0.95，低于此说明过滤下推没生效或参数不足。

精确扫描是全表，跑得慢，因此只在 nightly 任务里跑，不进 PR 门禁。
"""
from __future__ import annotations

from collections.abc import Sequence

import psycopg

from ragdemo.embed.base import Embedder
from ragdemo.retrieval.filters import build_filters
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalRequest

_SQL = """
SELECT block_id
  FROM asof.doc_block
 WHERE embedding IS NOT NULL
 {filters}
 ORDER BY embedding <=> %(qvec)s
 LIMIT %(limit)s
"""


def exact_vector_search(
    conn: psycopg.Connection,
    req: RetrievalRequest,
    query_vector: Sequence[float],
    limit: int,
) -> list[int]:
    """强制精确扫描，作为召回率的分母。"""
    filters, params = build_filters(req)
    params["qvec"] = "[" + ",".join(repr(float(x)) for x in query_vector) + "]"
    params["limit"] = limit

    from ragdemo.db.session import as_of_session

    with as_of_session(conn, req.as_of, tenant=req.tenant, user=req.user) as c:
        c.execute("SELECT set_config('enable_indexscan', 'off', true)")
        c.execute("SELECT set_config('enable_indexonlyscan', 'off', true)")
        rows = c.execute(_SQL.format(filters=filters), params).fetchall()
    return [int(r[0]) for r in rows]


def index_recall(
    conn: psycopg.Connection,
    service: RetrievalService,
    embedder: Embedder,
    cases: Sequence[RetrievalRequest],
) -> float:
    """索引路径命中的、精确路径也命中的比例。"""
    if not cases:
        raise ValueError("用例为空，无法计算索引召回率")

    ratios: list[float] = []
    for req in cases:
        qvec = embedder.embed([req.query])[0]
        exact = set(exact_vector_search(conn, req, qvec, req.config.candidate_k))
        if not exact:
            continue
        approx = {b.block_id for b in service.search(req).blocks}
        ratios.append(len(approx & exact) / len(exact & exact))
    return sum(ratios) / len(ratios) if ratios else 1.0
```

`Makefile` 追加：

```makefile
.PHONY: accept-p1

accept-p1:
	pytest tests/test_p1_acceptance.py -v -m db
```

`.github/workflows/eval.yml` 追加 nightly job：

```yaml
  nightly:
    if: github.event_name == 'schedule'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: pytest tests/evals/test_index_recall.py -v -m db
      - run: pytest tests/test_p1_acceptance.py -v -m db
```

并在文件顶部的 `on:` 增加 `schedule: [{cron: "0 18 * * *"}]`。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/evals/test_index_recall.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/evals/index_recall.py tests/evals/test_index_recall.py tests/test_p1_acceptance.py Makefile .github/workflows/eval.yml
git commit -m "test: 索引召回率验证与 P1 验收测试套件"
```

---

## Task 11: 备份恢复与出网代理

**Files:**
- Create: `scripts/backup.sh`, `scripts/restore.sh`, `infra/runbook-backup.md`, `infra/egress-proxy/policy.yaml`, `infra/egress-proxy/Dockerfile`, `tests/test_infra.py`
- Modify: `infra/docker-compose.yml`, `Makefile`

**Interfaces:**
- Consumes: 无
- Produces:
  - `make backup` / `make restore FILE=...`
  - `egress-proxy` 服务与域名白名单
  - `tests/test_infra.py` 校验策略文件与脚本的不变量

- [ ] **Step 1: 写失败的测试**

`tests/test_infra.py`：

```python
"""基础设施配置的不变量。这些错误在生产才暴露，代价太高。"""
from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path("infra/docker-compose.yml")
POLICY = Path("infra/egress-proxy/policy.yaml")


def test_paradedb_image_version_is_pinned() -> None:
    """adr/0001 的后果 2：镜像版本必须固定，latest 会让 pg_search 语法漂移。"""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    image = compose["services"]["paradedb"]["image"]
    assert ":" in image
    assert not image.endswith(":latest")


def test_database_port_is_bound_to_localhost_only() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    for mapping in compose["services"]["paradedb"]["ports"]:
        assert str(mapping).startswith("127.0.0.1:")


def test_egress_policy_denies_by_default() -> None:
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    assert policy["default_action"] == "deny"


def test_egress_policy_contains_no_literal_secrets() -> None:
    """密钥只在环境变量或代理层注入，策略文件里只能出现变量名。"""
    text = POLICY.read_text(encoding="utf-8")
    policy = yaml.safe_load(text)
    for rule in policy["allowlist"]:
        credential = rule.get("inject_credential")
        if credential is None:
            continue
        assert credential.isupper(), f"{credential} 应是环境变量名而非字面量"
        assert credential not in text.replace(f"inject_credential: {credential}", "")


def test_compose_has_no_hardcoded_password() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD" in text


def test_backup_script_is_executable_and_has_retention() -> None:
    script = Path("scripts/backup.sh").read_text(encoding="utf-8")
    assert "pg_dump" in script
    assert "RETENTION_DAYS" in script


def test_restore_script_refuses_without_explicit_confirmation() -> None:
    """恢复会覆盖现有库，必须显式确认。"""
    script = Path("scripts/restore.sh").read_text(encoding="utf-8")
    assert "CONFIRM" in script


def test_runbook_covers_the_drill() -> None:
    runbook = Path("infra/runbook-backup.md").read_text(encoding="utf-8")
    for heading in ("每日备份", "恢复演练", "PITR"):
        assert heading in runbook
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_infra.py -v`
Expected: FAIL — `FileNotFoundError: infra/egress-proxy/policy.yaml`

- [ ] **Step 3: 写最小实现**

`infra/egress-proxy/policy.yaml`：

```yaml
# 出网白名单。密钥只在代理层注入，业务容器的环境变量中没有任何供应商密钥。
# 不在名单内的域名直接拒绝——即使某个依赖被投毒，它也无法把数据发到任意地址。
default_action: deny
log: full           # 记录 host / path / 状态码 / 字节数 / 耗时；不记录请求体

allowlist:
  - host: api.tushare.pro
    inject_credential: TUSHARE_TOKEN
  - host: data.sec.gov
    inject_credential: null
  - host: www.sec.gov
    inject_credential: null
  - host: api.siliconflow.cn        # 嵌入与重排（adr/0004）
    inject_credential: EMBEDDING_API_KEY
  - host: api.deepseek.com          # 生成模型
    inject_credential: MODEL_API_KEY
```

`infra/egress-proxy/Dockerfile`：

```dockerfile
FROM mitmproxy/mitmproxy:11.0.0
COPY policy.yaml /policy/policy.yaml
COPY addon.py /policy/addon.py
ENTRYPOINT ["mitmdump", "--set", "block_global=false", "-s", "/policy/addon.py"]
```

`scripts/backup.sh`：

```bash
#!/usr/bin/env bash
# 每日备份到对象存储。自建 ParadeDB 没有托管 RDS 的自动备份（adr/0002 的负面后果 1）。
set -euo pipefail

RETENTION_DAYS="${RETENTION_DAYS:-30}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/ragdemo}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/ragdemo-${STAMP}.dump"

mkdir -p "${BACKUP_DIR}"
pg_dump --format=custom --compress=9 --file="${TARGET}" "${RAGDEMO_DSN:?RAGDEMO_DSN 未设置}"

sha256sum "${TARGET}" > "${TARGET}.sha256"
echo "备份完成: ${TARGET} ($(du -h "${TARGET}" | cut -f1))"

find "${BACKUP_DIR}" -name 'ragdemo-*.dump*' -mtime "+${RETENTION_DAYS}" -delete
echo "已清理 ${RETENTION_DAYS} 天前的备份"
```

`scripts/restore.sh`：

```bash
#!/usr/bin/env bash
# 从备份恢复。会覆盖目标库，因此要求显式确认。
set -euo pipefail

FILE="${1:?用法: restore.sh <dump 文件>}"
: "${RAGDEMO_DSN:?RAGDEMO_DSN 未设置}"

if [ "${CONFIRM:-}" != "yes" ]; then
  echo "恢复会覆盖 ${RAGDEMO_DSN} 的现有数据。" >&2
  echo "确认后重跑: CONFIRM=yes $0 ${FILE}" >&2
  exit 1
fi

sha256sum --check "${FILE}.sha256"
pg_restore --clean --if-exists --no-owner --dbname "${RAGDEMO_DSN}" "${FILE}"

psql "${RAGDEMO_DSN}" -c "SELECT 'entity', count(*) FROM core.entity
                          UNION ALL SELECT 'doc_block', count(*) FROM core.doc_block
                          UNION ALL SELECT 'fin_fact', count(*) FROM core.fin_fact;"
echo "恢复完成。请核对上表计数与备份时是否一致。"
```

`infra/runbook-backup.md`：

```markdown
# 备份与恢复 Runbook

自建 ParadeDB 没有托管 RDS 的自动备份、PITR 与跨可用区副本
（[adr/0002](../docs/adr/0002-self-hosted-paradedb-single-db.md) 的负面后果 1）。
这份手册是那笔成本的支付方式。

## 每日备份

cron（宿主机）：

```
0 3 * * * RAGDEMO_DSN=... RETENTION_DAYS=30 /opt/ragdemo/scripts/backup.sh >> /var/log/ragdemo-backup.log 2>&1
```

产物：`ragdemo-<UTC 时间戳>.dump` 与同名 `.sha256`。保留 30 天。
备份完成后同步到对象存储（独立于数据库所在主机）。

## PITR

`postgresql.conf`：

```
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /wal-archive/%f && cp %p /wal-archive/%f'
```

WAL 归档目录与每日全量备份一同上传对象存储。恢复到任意时点时，
先 `pg_restore` 最近的全量备份，再重放归档 WAL 到目标时刻。

## 恢复演练

**每月一次，不可跳过。** 没演练过的备份等于没有备份。

1. 起一个空的 ParadeDB 容器（与生产同版本 tag）；
2. `CONFIRM=yes scripts/restore.sh <最近的 dump>`；
3. 核对脚本输出的三张表计数与备份当日的监控记录是否一致；
4. 跑 `ragdemo db check`（schema 不变量 + 时点泄漏自检）；
5. 跑 `pytest tests/test_p1_acceptance.py -m db`；
6. 把演练日期、耗时、发现的问题记入本文件末尾的演练日志。

## 演练日志

| 日期 | 耗时 | 恢复到 | 结果 | 备注 |
|---|---|---|---|---|
| | | | | |
```

`infra/docker-compose.yml` 追加：

```yaml
  egress-proxy:
    build: ./egress-proxy
    container_name: ragdemo-egress
    environment:
      TUSHARE_TOKEN: ${TUSHARE_TOKEN:-}
      EMBEDDING_API_KEY: ${EMBEDDING_API_KEY:-}
      MODEL_API_KEY: ${MODEL_API_KEY:-}
    ports:
      - "127.0.0.1:8080:8080"
    restart: unless-stopped
```

`Makefile` 追加：

```makefile
.PHONY: backup restore

backup:
	bash scripts/backup.sh

restore:
	CONFIRM=yes bash scripts/restore.sh $(FILE)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `chmod +x scripts/*.sh && pytest tests/test_infra.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add scripts infra Makefile tests/test_infra.py
git commit -m "feat(infra): 备份恢复 runbook 与出网代理白名单"
```

---

## Self-Review

**Spec 覆盖检查**：

| 工作流 / 验收项 | 任务 | 覆盖 |
|---|---|---|
| W3.1 `RetrievalService` 骨架 + 过滤 + stats | Task 1, 7 | ✅ |
| W3.2 BM25 + 冗余谓词 + 下推验证 | Task 2, 10 | ✅ |
| W3.3 向量 + 迭代扫描 | Task 3 | ✅ |
| W3.4 加权 RRF | Task 4 | ✅ |
| W3.5 重排 + 降级 | Task 5 | ✅ |
| W3.6 父子块展开去重 | Task 6 | ✅ |
| W3.7 查询改写（默认关） | Task 7 | ✅ |
| W6.1 评测框架 + `eval_run` | Task 8 | ✅ |
| W6.2 评测集录入 + 指标 + CI | Task 9 | ✅ |
| W7.4 备份恢复演练 | Task 11 | ✅ |
| W7.6 出网代理 + 密钥 | Task 11 | ✅ |
| P1 验收九项 | Task 10（`tests/test_p1_acceptance.py`） | ✅ |

**类型一致性检查**：`RankedHit` 在 Task 2 定义，Task 3/4 使用；`FusedHit` 在 Task 4 定义，
Task 5/7 使用；`RerankOutcome` 在 Task 5 定义，Task 7 使用；`EvidenceBlock` 在 Task 1 定义，
Task 6 构造、Task 7/8 消费；`build_filters` 在 Task 1 定义，Task 2/3/10 使用；
`RetrievalConfig` 字段在 Task 1 定义，Task 3（`ef_search`/`max_scan_tuples`）、
Task 4（`w_bm25`/`w_vec`/`rrf_k`/`fusion_k`）、Task 7（`rerank_enabled`/`rewrite_enabled`/`top_k`）
分别使用，名称一致。

**已知缺口**：

- `Dagster 管线连续 5 天无人工干预跑通` 是 P1 验收第 1 项，但它只能靠**真实运行**验证，
  无法写成测试。运维方式：`make dagster` 起服务后连续观察 5 天，把每日分区状态记入
  `infra/runbook-backup.md` 同目录的运行日志。
- `双跑一致性`（Task 10）第一次跑只写快照并 skip，**必须一周后重跑**才算通过。
  这是 [`10-roadmap.md`](../../10-roadmap.md) P1 明确要求预留的一周。
- 真实嵌入 / 重排 API 的适配器未实现，P1 全程用 `MockEmbedder` / `MockReranker`。
  接口已定（P1b Task 7、本计划 Task 5），接入真实 API 是 P2 的第一件事。

---

## 完成之后

1. 在 [`docs/10-roadmap.md`](../../10-roadmap.md) P1 的全部 11 项 checklist 上打勾。
2. 把实测结论回写 [`docs/06-retrieval.md`](../../06-retrieval.md)——尤其是
   §3.4 冗余谓词是否真的改变了执行计划、§7 查询改写在评测集上的实际提升。
3. 按 [`docs/11-sdlc.md`](../../11-sdlc.md) §8 编写 P2 的实施计划。
