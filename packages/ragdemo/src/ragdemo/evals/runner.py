"""评测运行器。

每次运行写一行 evals.eval_run，含 git_sha 与完整 config。
没有这张表，三个月后看到指标下降无法定位是哪个参数改的（docs/08-evaluation.md §4.3）。
"""

from __future__ import annotations

import dataclasses
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ragdemo.evals.grouping import CaseOutcome, case_group, engine_group, summarize_groups
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
    # 每条 top-k 检索的端到端耗时（毫秒，含重排；不含 recall@50 那次宽检索）。
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    per_group: dict[str, dict[str, float | int | bool]] = field(default_factory=dict)
    per_engine: dict[str, dict[str, float | int | bool]] = field(default_factory=dict)
    # 逐题各阶段的 gold 排名（最佳 gold 的名次，None = 不在该阶段候选里）。
    # 存进 metrics JSON，不需要迁移；事后分析失败题不用再付费重跑。
    per_question: list[dict[str, Any]] = field(default_factory=list)
    # 重排超时而回退融合顺序的查询数（仅统计 top-k 那次检索）。
    rerank_timeouts: int = 0

    def as_metrics(self) -> dict[str, Any]:
        return {
            "recall_at_10": self.recall_at_10,
            "recall_at_50": self.recall_at_50,
            "mrr_at_10": self.mrr_at_10,
            "n_cases": self.n_cases,
            "per_difficulty": self.per_difficulty,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "per_group": self.per_group,
            "per_engine": self.per_engine,
            "per_question": self.per_question,
            "rerank_timeouts": self.rerank_timeouts,
        }


def _percentile(values: list[float], q: float) -> float:
    """最近秩法分位数（不插值）：p95 就是"有 95% 的请求不慢于它"。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _best_rank(order: Sequence[int], gold: set[int]) -> int | None:
    """gold 中名次最靠前者的名次（从 1 起）；一个都不在则为 None。"""
    for i, block_id in enumerate(order):
        if block_id in gold:
            return i + 1
    return None


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
        (run_id, suite, git_sha, Jsonb(dict(config)), Jsonb(dict(metrics)), started_at, ended_at),
    )
    conn.commit()


def run_retrieval_eval(
    conn: psycopg.Connection,
    service: RetrievalService,
    *,
    git_sha: str,
    config: RetrievalConfig,
    extra_config: Mapping[str, Any] | None = None,
    author_prefix: str | None = None,
) -> RetrievalEvalResult:
    started_at = datetime.now(UTC)
    cases = conn.execute(
        "SELECT q_id, question, as_of, gold_block_ids, entity_filter, doc_type_filter,"
        " difficulty, author FROM evals.eval_retrieval WHERE (%s::text IS NULL OR author LIKE %s)"
        " ORDER BY q_id",
        (author_prefix, f"{author_prefix}%" if author_prefix else None),
    ).fetchall()
    if not cases:
        raise ValueError("评测集为空；空集会算出满分，让 CI 门禁形同虚设")

    all_gold = sorted({int(g) for case in cases for g in case[3]})
    gold_rows = conn.execute(
        "SELECT block_id, block_type::text, parse_source FROM core.doc_block"
        " WHERE block_id = ANY(%s)",
        (all_gold,),
    ).fetchall()
    gold_types = {int(r[0]): str(r[1]) for r in gold_rows}
    gold_sources: dict[int, str | None] = {int(r[0]): r[2] for r in gold_rows}

    wide = dataclasses.replace(config, top_k=min(config.fusion_k, 50), rerank_enabled=False)
    hits10: list[float] = []
    hits50: list[float] = []
    mrrs: list[float] = []
    by_difficulty: dict[str, list[float]] = {}
    latencies_ms: list[float] = []
    outcomes: list[CaseOutcome] = []
    engine_outcomes: list[CaseOutcome] = []
    per_question: list[dict[str, Any]] = []

    for (
        q_id,
        question,
        as_of,
        gold,
        entity_filter,
        doc_type_filter,
        difficulty,
        author,
    ) in cases:
        gold_set = {int(g) for g in gold}

        t0 = time.perf_counter()
        top = service.search(
            RetrievalRequest(
                query=str(question),
                as_of=as_of,
                entity_ids=list(entity_filter) if entity_filter else None,
                doc_types=list(doc_type_filter) if doc_type_filter else None,
                config=config,
            ),
            trace=True,
        )
        latencies_ms.append((time.perf_counter() - t0) * 1000)
        ordered10 = [b.block_id for b in top.blocks]
        tr = top.trace
        per_question.append(
            {
                "q_id": int(q_id),
                "gold": sorted(gold_set),
                "bm25_rank": _best_rank([s.block_id for s in tr.bm25], gold_set) if tr else None,
                "vec_rank": _best_rank([s.block_id for s in tr.vec], gold_set) if tr else None,
                "fused_rank": _best_rank([s.block_id for s in tr.fused], gold_set) if tr else None,
                "rerank_rank": (
                    _best_rank([s.block_id for s in tr.rerank], gold_set)
                    if tr and tr.rerank_attempted
                    else None
                ),
                "final_rank": _best_rank(ordered10, gold_set),
                "ms_total": round(latencies_ms[-1], 1),
                "ms_rerank": round(top.stats.ms_rerank, 1),
                "degraded_reason": top.stats.degraded_reason,
                "ms_embed_vec": round(top.stats.ms_vec, 1),
                "ms_bm25": round(top.stats.ms_bm25, 1),
            }
        )
        hits10.append(recall_at_k(ordered10, gold_set, 10))
        mrrs.append(mrr_at_k(ordered10, gold_set, 10))

        broad = service.search(
            RetrievalRequest(
                query=str(question),
                as_of=as_of,
                entity_ids=list(entity_filter) if entity_filter else None,
                doc_types=list(doc_type_filter) if doc_type_filter else None,
                config=wide,
            )
        )
        hits50.append(recall_at_k([b.block_id for b in broad.blocks], gold_set, 50))

        outcomes.append(
            CaseOutcome(
                case_group({gold_types.get(g, "unknown") for g in gold_set}, author=str(author)),
                hits10[-1],
                hits50[-1],
                mrrs[-1],
            )
        )
        engine_outcomes.append(
            CaseOutcome(
                engine_group({gold_sources.get(g) for g in gold_set}, author=str(author)),
                hits10[-1],
                hits50[-1],
                mrrs[-1],
            )
        )
        by_difficulty.setdefault(str(difficulty or "unknown"), []).append(hits10[-1])

    result = RetrievalEvalResult(
        recall_at_10=sum(hits10) / len(hits10),
        recall_at_50=sum(hits50) / len(hits50),
        mrr_at_10=sum(mrrs) / len(mrrs),
        n_cases=len(cases),
        per_difficulty={k: sum(v) / len(v) for k, v in by_difficulty.items()},
        latency_p50_ms=_percentile(latencies_ms, 0.50),
        latency_p95_ms=_percentile(latencies_ms, 0.95),
        per_group={k: v.as_dict() for k, v in summarize_groups(outcomes).items()},
        per_engine={k: v.as_dict() for k, v in summarize_groups(engine_outcomes).items()},
        per_question=per_question,
        rerank_timeouts=sum(1 for q in per_question if q["degraded_reason"] == "rerank_timeout"),
    )

    record_eval_run(
        conn,
        run_id=f"eval-{uuid.uuid4().hex[:12]}",
        suite="retrieval",
        git_sha=git_sha,
        config={**dataclasses.asdict(config), **(dict(extra_config) if extra_config else {})},
        metrics=result.as_metrics(),
        started_at=started_at,
        ended_at=datetime.now(UTC),
    )
    return result
