"""期别感知的重排修正（阶段 5 调参）。

披露类文档有一个检索通用语义无法区分的维度：**期别**。海光 2022、2023、2024 年的年报里
"公司主要产品有哪些"几乎是同一段话——语义相似度分不出该给哪一年的，而用户问的往往是明确的
一年（"2023 年年度报告里……"）。词面 / 向量 / 重排都只看内容相似，结果是同文异年的块排在一起、
排在前面的常常是别的年份。

这里在重排之后按问题里明确写出的**年份**修正分数（不改变候选集合，只改排序）：
- 问题里恰有一个明确年份 Y（"2023 年""2023年"）；
- 候选块所属文档标题里也带明确年份：等于 Y → 分数 ×(1 + boost)；不等于 Y → ×(1 − boost)；
- 标题不含年份（如"关于……的公告"）→ 不动。

`boost = 0`（默认）时完全不改变原有行为。问题里有多个不同年份（比较类问题）时不做修正——
无法判断该偏向哪一年。这是一个**排序偏置**，不是过滤：年份写错、或标题年份与期别不同
（年报标题里是报告期年份，披露年份晚一年）时，正确答案仍在候选里，只是排序略受影响。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TypeVar

import psycopg

from ragdemo.retrieval.fusion import FusedHit

_YEAR = re.compile(r"(?<!\d)(20\d{2})\s*年")

T = TypeVar("T")


def explicit_year(text: str) -> str | None:
    """文本里唯一的明确年份；没有或有多个不同的则返回 None。"""
    years = set(_YEAR.findall(text))
    return years.pop() if len(years) == 1 else None


def _titles(conn: psycopg.Connection, block_ids: Sequence[int]) -> dict[int, str]:
    if not block_ids:
        return {}
    rows = conn.execute(
        "SELECT b.block_id, d.title FROM asof.doc_block b JOIN asof.document d USING (doc_id)"
        " WHERE b.block_id = ANY(%s)",
        (list(block_ids),),
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def period_factor(query_year: str, title: str, boost: float) -> float:
    title_year = explicit_year(title)
    if title_year is None:
        return 1.0
    return 1.0 + boost if title_year == query_year else 1.0 - boost


def boost_scores(
    conn: psycopg.Connection,
    query: str,
    scores: Mapping[int, float],
    boost: float,
) -> dict[int, float]:
    """按期别修正 `block_id -> 分数`。`boost <= 0` 或问题里没有唯一年份时原样返回。
    必须在 `as_of_session` 内调用（读 asof 视图）。"""
    year = explicit_year(query)
    if boost <= 0 or year is None or not scores:
        return dict(scores)
    titles = _titles(conn, list(scores))
    return {
        block_id: score * period_factor(year, titles.get(block_id, ""), boost)
        for block_id, score in scores.items()
    }


def boost_fused(
    conn: psycopg.Connection, query: str, fused: Sequence[FusedHit], boost: float
) -> list[FusedHit]:
    """对融合结果按期别修正分数并重排（同分按 block_id，与 `weighted_rrf` 一致）。"""
    boosted = boost_scores(conn, query, {f.block_id: f.score for f in fused}, boost)
    out = [replace(f, score=boosted[f.block_id]) for f in fused]
    out.sort(key=lambda f: (-f.score, f.block_id))
    return out
