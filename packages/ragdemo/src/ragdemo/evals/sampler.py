"""评测集分层抽样（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 H）。

`docs/06-retrieval.md` §9.2 要 100 条生产用例，现在 `evals.eval_retrieval`
只有 5 条自造夹具。标注本身是创始人的工作（方案 §1.2 职责边界表），本模块
只负责选出"哪些块值得被标注"——按 `doc_type × parse_engine 族 × entity_id`
分层抽样，避免 100 条候选全落在最容易的年报正文上（方案 §2.1 的顾虑）。

分层键里的 `parse_engine` 取"引擎族"（`textin`/`mock`/`vendor:xxx`，即冒号前
半段），不取带参数指纹的完整字符串——两个块只是 xParse 参数版本不同，
不该被当成两个不同的分层维度，那样会把分层打散成几十个每层只有一两条的
碎片，`min_per_stratum` 形同虚设。

抽样种子固定（不是随机种子）：同一个 `seed` 两次调用必须选出完全相同的
候选集合（H2 验收），复现性比"每次都随机换一批"更重要——标注是人工成本，
不能因为重跑了一次工具就作废之前标好的一批。
"""

from __future__ import annotations

import csv
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg

DEFAULT_SEED = 42
DEFAULT_MIN_PER_STRATUM = 5
_PREVIEW_CHARS = 160


@dataclass(frozen=True)
class SampleCandidate:
    block_id: int
    doc_id: int
    entity_id: str | None
    doc_type: str
    parse_engine: str
    section_path: str
    page: int | None
    content_preview: str


def _engine_family(parse_engine: str) -> str:
    return parse_engine.split(":", 1)[0]


def _fetch_pool(conn: psycopg.Connection) -> list[SampleCandidate]:
    """候选池：活着的叶子块，来自活着的文档——只从当前视角看得见的语料里选，
    不从已经被更正/取代的历史版本里选（那些块标注了也测不到当前的检索行为）。
    """
    rows = conn.execute(
        "SELECT b.block_id, b.doc_id, b.entity_id, b.doc_type, d.parse_engine,"
        " b.section_path, b.page, b.content"
        "  FROM core.doc_block b JOIN core.document d USING (doc_id)"
        " WHERE b.is_leaf AND b.superseded_at IS NULL AND d.superseded_at IS NULL"
        " ORDER BY b.block_id"
    ).fetchall()
    return [
        SampleCandidate(
            block_id=int(r[0]),
            doc_id=int(r[1]),
            entity_id=r[2],
            doc_type=str(r[3]),
            parse_engine=str(r[4] or "unknown"),
            section_path=str(r[5]),
            page=r[6],
            content_preview=str(r[7])[:_PREVIEW_CHARS],
        )
        for r in rows
    ]


def _stratum_key(candidate: SampleCandidate) -> tuple[str, str, str]:
    return (candidate.doc_type, _engine_family(candidate.parse_engine), candidate.entity_id or "")


def sample_candidates(
    conn: psycopg.Connection,
    *,
    n: int = 100,
    seed: int = DEFAULT_SEED,
    min_per_stratum: int = DEFAULT_MIN_PER_STRATUM,
) -> list[SampleCandidate]:
    """按 `doc_type × parse_engine 族 × entity_id` 分层抽样。

    分配规则：每个有候选的分层至少拿 `min_per_stratum` 条（分层里没有那么多
    候选就有多少拿多少，不会无中生有）；分完保底后如果还没到 `n`，按分层
    顺序轮流补，直到达到 `n` 或所有分层都被抽空——不是简单地按比例算一个
    浮点数再取整，那样在分层数量接近 `n` 时经常凑不出恰好 `n` 条。
    """
    rng = random.Random(seed)
    pool = _fetch_pool(conn)

    strata: dict[tuple[str, str, str], list[SampleCandidate]] = {}
    for candidate in pool:
        strata.setdefault(_stratum_key(candidate), []).append(candidate)

    # 分层内部的抽样顺序也要由种子决定，不能依赖 dict/查询结果的原始顺序——
    # 后者虽然当前是按 block_id 排序的稳定顺序，但"稳定"不等于"这是抽样该
    # 依赖的顺序"，显式 shuffle 让复现性来自 seed 本身，不是偶然的查询顺序。
    ordered_keys = sorted(strata.keys())
    for key in ordered_keys:
        rng.shuffle(strata[key])

    picked: dict[tuple[str, str, str], list[SampleCandidate]] = {key: [] for key in ordered_keys}
    remaining = {key: list(strata[key]) for key in ordered_keys}

    for key in ordered_keys:
        take = min(min_per_stratum, len(remaining[key]))
        picked[key].extend(remaining[key][:take])
        remaining[key] = remaining[key][take:]

    total = sum(len(v) for v in picked.values())
    while total < n and any(remaining[key] for key in ordered_keys):
        for key in ordered_keys:
            if total >= n:
                break
            if remaining[key]:
                picked[key].append(remaining[key].pop(0))
                total += 1

    result = [c for key in ordered_keys for c in picked[key]]
    return result[:n] if len(result) > n else result


def write_candidates_csv(path: Path, candidates: Sequence[SampleCandidate]) -> None:
    """落一份 CSV 给创始人：block_id 用于回填 gold_block_ids，其余列只是
    帮助人工判断"这一条值不值得写一道题"。不写 question/gold_block_ids
    列——那是标注产物，不是抽样工具能猜的。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["block_id", "doc_id", "entity_id", "doc_type", "parse_engine", "section_path",
             "page", "content_preview"]
        )
        for c in candidates:
            writer.writerow(
                [c.block_id, c.doc_id, c.entity_id or "", c.doc_type, c.parse_engine,
                 c.section_path, c.page if c.page is not None else "", c.content_preview]
            )


__all__ = (
    "DEFAULT_MIN_PER_STRATUM",
    "DEFAULT_SEED",
    "SampleCandidate",
    "sample_candidates",
    "write_candidates_csv",
)
