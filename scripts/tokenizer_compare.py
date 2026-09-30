"""BM25 分词器对比（阶段 5.7）：`chinese_lindera`（现用）vs `chinese_compatible`。

只测**词面一路**（不调任何嵌入 / 重排 API，零成本、结果确定）：对评测集里的每个问题，
用 `bm25_search` 取 BM25 排名，算 recall@10 / recall@50 / MRR@10，gold 是叶子块 id。

用法：对同一份语料的两个库各跑一次（一个用现有 lindera 索引，一个把 `doc_block_bm25`
重建成 chinese_compatible），再比较输出。重建索引的库通常是 `scripts/restore_drill.sh`
恢复出来的临时容器——不要在现库上 DROP / CREATE 索引。

    RAGDEMO_DSN=<库 DSN> uv run python scripts/tokenizer_compare.py <标签>
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import psycopg

from ragdemo.evals.retrieval_metrics import mrr_at_k, recall_at_k
from ragdemo.retrieval.lexical import bm25_search
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest
from ragdemo_core.db.session import as_of_session


def main(label: str) -> None:
    dsn = os.environ["RAGDEMO_DSN"]
    cfg = RetrievalConfig()
    hits10: list[float] = []
    hits50: list[float] = []
    mrrs: list[float] = []
    with psycopg.connect(dsn) as conn:
        tokenizer = conn.execute(
            "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'doc_block_bm25'"
        ).fetchone()
        used = "lindera" if tokenizer and "chinese_lindera" in str(tokenizer[0]) else "compatible"
        cases = conn.execute(
            "SELECT question, as_of, gold_block_ids, entity_filter FROM evals.eval_retrieval"
            " ORDER BY q_id"
        ).fetchall()
        for question, as_of, gold, entity_filter in cases:
            assert isinstance(as_of, datetime)
            req = RetrievalRequest(
                query=str(question),
                as_of=as_of,
                entity_ids=list(entity_filter) if entity_filter else None,
                config=cfg,
            )
            with as_of_session(conn, as_of):
                ranked = [h.block_id for h in bm25_search(conn, req)]
            gold_set = {int(g) for g in gold}
            hits10.append(recall_at_k(ranked, gold_set, 10))
            hits50.append(recall_at_k(ranked, gold_set, 50))
            mrrs.append(mrr_at_k(ranked, gold_set, 10))
    n = len(cases)
    print(
        f"{label}\t索引分词器={used}\t用例={n}\t"
        f"recall@10={sum(hits10) / n:.4f}\trecall@50={sum(hits50) / n:.4f}\t"
        f"MRR@10={sum(mrrs) / n:.4f}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bm25-only")
