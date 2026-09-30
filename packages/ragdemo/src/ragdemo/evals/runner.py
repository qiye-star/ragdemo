"""评测运行器。

每次运行写一行 evals.eval_run，含 git_sha 与完整 config。
没有这张表，三个月后看到指标下降无法定位是哪个参数改的（docs/08-evaluation.md §4.3）。
"""

from __future__ import annotations

import dataclasses
import math
import time
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
    # 每条 top-k 检索的端到端耗时（毫秒，含重排；不含 recall@50 那次宽检索）。
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0

    def as_metrics(self) -> dict[str, Any]:
        return {
            "recall_at_10": self.recall_at_10,
            "recall_at_50": self.recall_at_50,
            "mrr_at_10": self.mrr_at_10,
            "n_cases": self.n_cases,
            "per_difficulty": self.per_difficulty,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
        }


def _percentile(values: list[float], q: float) -> float:
    """最近秩法分位数（不插值）：p95 就是"有 95% 的请求不慢于它"。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


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
        " difficulty FROM evals.eval_retrieval WHERE (%s::text IS NULL OR author LIKE %s)"
        " ORDER BY q_id",
        (author_prefix, f"{author_prefix}%" if author_prefix else None),
    ).fetchall()
    if not cases:
        raise ValueError("评测集为空；空集会算出满分，让 CI 门禁形同虚设")

    wide = dataclasses.replace(config, top_k=min(config.fusion_k, 50), rerank_enabled=False)
    hits10: list[float] = []
    hits50: list[float] = []
    mrrs: list[float] = []
    by_difficulty: dict[str, list[float]] = {}
    latencies_ms: list[float] = []

    for _q_id, question, as_of, gold, entity_filter, doc_type_filter, difficulty in cases:
        gold_set = {int(g) for g in gold}

        t0 = time.perf_counter()
        top = service.search(
            RetrievalRequest(
                query=str(question),
                as_of=as_of,
                entity_ids=list(entity_filter) if entity_filter else None,
                doc_types=list(doc_type_filter) if doc_type_filter else None,
                config=config,
            )
        )
        latencies_ms.append((time.perf_counter() - t0) * 1000)
        ordered10 = [b.block_id for b in top.blocks]
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

        by_difficulty.setdefault(str(difficulty or "unknown"), []).append(hits10[-1])

    result = RetrievalEvalResult(
        recall_at_10=sum(hits10) / len(hits10),
        recall_at_50=sum(hits50) / len(hits50),
        mrr_at_10=sum(mrrs) / len(mrrs),
        n_cases=len(cases),
        per_difficulty={k: sum(v) / len(v) for k, v in by_difficulty.items()},
        latency_p50_ms=_percentile(latencies_ms, 0.50),
        latency_p95_ms=_percentile(latencies_ms, 0.95),
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
