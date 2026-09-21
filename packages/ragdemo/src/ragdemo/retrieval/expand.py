"""父子块展开与去重（docs/06-retrieval.md §6，docs/05-document-pipeline.md §7）。

检索命中叶子块，生成用父块内容，但**引用标注用叶子块 block_id**——
溯源要精确到段落，指到整个小节等于没指。

`docs/05-document-pipeline.md §7` 里父块是「整个小节，供生成使用」：正常入库时
父块自身的 `content` 就应覆盖其下全部叶子块的文本。但父块的 `content` 是否
真的完整取决于上游解析质量，不能假定它单独就够——本实现按 `parent_block_id`
把父块与其下全部叶子块（不止命中的那些）按 `ordinal` 拼接，重建完整小节，
容错「父块自身内容只是占位/摘要」的情况，同时对齐 fixture
（`tests/retrieval/conftest.py`）里父块特意留了占位内容、真实文本分布在叶子块的设定。

只查 `asof.doc_block` / `asof.document` 安全视图，不查 `core` 基表
（CLAUDE.md §1.1）。查询前必须用 `as_of_session` 把 `as_of` 绑到本次事务，
否则视图里的 `asof.current_as_of()` 会因 GUC 未设而抛 `InvalidParameterValue`
（Task 2 在 `retrieval/lexical.py` 踩过的坑）。这也带来一个附加好处：
`order` 里若混进了在 `as_of` 时点尚不可见的 block_id（例如上游传错），
asof 视图会直接把它过滤掉，展开这一步不会替它把内容偷渡回结果。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

import psycopg

from ragdemo.retrieval.types import EvidenceBlock
from ragdemo_core.db.session import as_of_session

_SQL_BLOCKS = """
SELECT b.block_id,
       b.parent_block_id,
       b.content,
       b.doc_id,
       d.title,
       b.section_path,
       b.page,
       b.known_at
  FROM asof.doc_block b
  JOIN asof.document d ON d.doc_id = b.doc_id
 WHERE b.block_id = ANY(%(block_ids)s)
"""

_SQL_PARENT_CONTENT = """
SELECT content
  FROM asof.doc_block
 WHERE block_id = %(parent_id)s OR parent_block_id = %(parent_id)s
 ORDER BY ordinal
"""


def expand_to_evidence(
    conn: psycopg.Connection,
    order: Sequence[int],
    scores: Mapping[int, float],
    *,
    reranked: bool,
    top_k: int,
    as_of: datetime,
    tenant: str | None = None,
    user: str | None = None,
) -> list[EvidenceBlock]:
    """按 order 展开为证据集。同一父块只出现一次，取其中最高分的子块作为引用。

    去重后不足 top_k 时不补位——宁可少给几条也不引入低相关证据（docs/06-retrieval.md
    §6 规则 4）。

    Args:
        conn: 数据库连接；函数内部用 `as_of_session` 包一层事务再查 asof 视图。
        order: 重排（或降级后的 RRF）顺序，元素是叶子块 block_id。
        scores: block_id -> 相关度分，取自 `RerankOutcome.scores`（或降级时的 RRF 分）。
        reranked: 是否经过了重排，原样写入每条 EvidenceBlock，供下游判断是否降级。
        top_k: 去重后最多返回的证据条数。
        as_of: 查询时点，必须带时区；只用于给 asof 视图设置会话 GUC。
        tenant: 租户标识，透传给 `as_of_session` 用于行级隔离；None 表示只看公共数据。
        user: 用户标识，透传给 `as_of_session` 用于行级隔离；None 表示只看公共数据。
    """
    if not order:
        return []

    with as_of_session(conn, as_of, tenant=tenant, user=user):
        rows = conn.execute(_SQL_BLOCKS, {"block_ids": list(order)}).fetchall()
        by_id = {int(r[0]): r for r in rows}

        parent_ids = {int(r[1]) for r in rows if r[1] is not None}
        parent_content: dict[int, str] = {}
        for parent_id in parent_ids:
            crows = conn.execute(_SQL_PARENT_CONTENT, {"parent_id": parent_id}).fetchall()
            parent_content[parent_id] = "\n\n".join(str(c[0]) for c in crows)

    grouped: dict[int, list[int]] = {}
    group_order: list[int] = []
    for block_id in order:
        row = by_id.get(block_id)
        if row is None:
            # 在 as_of 时点不可见（未 known_at 或已 superseded），跳过，不补位。
            continue
        # 有父块的按父块分组；表格块（无父块）自成一组，用负 id 避免与真实父块 id 撞车
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
        parent_block_id = int(row[1]) if row[1] is not None else None
        content = parent_content[parent_block_id] if parent_block_id is not None else str(row[2])
        evidence.append(
            EvidenceBlock(
                block_id=best,
                parent_block_id=parent_block_id,
                content=content,
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
