"""评测 gold 块号重映射（旧文档缓存重解析之后）。

重解析（`DocumentWriter.reparse_document`）给旧块标 `superseded_at`、在新 doc_id 下插入新块；
`evals.eval_retrieval.gold_block_ids` 仍指向旧块号。本模块给每个被取代的 gold 块在**同一份文档的
现行后继版本**里找替身，确定性、不调用任何模型：

1. **exact**：规范化文本（去掉全部空白）完全相同的现行叶子块；多个时取同页、ordinal 最近、块号最小。
2. **overlap**：切块边界变了。候选必须与旧块**同页**，且两者是包含关系
   （新块包含旧块，或被旧块包含），旧块文本被新块覆盖的比例 >= 0.9
   （新块包含旧块时为 1.0；新块被旧块包含时为 新长 / 旧长）。
   包含关系按去掉「章节路径 + 换行」前缀后的正文判断。
   取覆盖最高、长度差最小、ordinal 最近、块号最小者。
3. **lost**：都不满足（含：文档没有现行后继）。旧块号原样保留，交人工裁决。

现行块（未被取代）记 `live`，不动。只生成 SQL（带守卫：gold 必须仍等于生成时读到的值，
否则整体报错回滚），是否在真实库执行由人决定（`ragdemo docs remap-gold`，默认只出报告与 SQL）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import psycopg

RemapKind = Literal["live", "exact", "overlap", "lost"]
MIN_OVERLAP: Final = 0.9
_WS = re.compile(r"\s+")
_RANK: Final[dict[str, int]] = {"live": 0, "exact": 1, "overlap": 2, "lost": 3}


def normalize_text(text: str) -> str:
    return _WS.sub("", text)


def coverage(old_norm: str, new_norm: str) -> float:
    """旧块（规范化）文本被新块覆盖的比例；只认包含关系，部分交叠一律 0。"""
    if not old_norm or not new_norm:
        return 0.0
    if old_norm in new_norm:
        return 1.0
    if new_norm in old_norm:
        return len(new_norm) / len(old_norm)
    return 0.0


@dataclass(frozen=True)
class BlockText:
    block_id: int
    doc_id: int
    page: int | None
    ordinal: int
    content: str
    section_path: str = ""

    def body(self) -> str:
        """去掉切块器加的「章节路径 + 换行」前缀后的正文（见 chunker._with_section_prefix）。"""
        if not self.section_path:
            return self.content
        return self.content.removeprefix(self.section_path + "\n")


@dataclass(frozen=True)
class BlockRemap:
    old_block_id: int
    kind: RemapKind
    new_block_id: int | None
    coverage: float
    old_doc_id: int | None = None
    new_doc_id: int | None = None
    page: int | None = None
    note: str = ""


def remap_block(
    old: BlockText, candidates: Sequence[BlockText], *, min_overlap: float = MIN_OVERLAP
) -> BlockRemap:
    """在候选（同一文档现行版本的叶子块）里给旧块找替身。"""
    old_norm = normalize_text(old.content)
    old_body = normalize_text(old.body())

    def same_page(c: BlockText) -> bool:
        return old.page is not None and c.page == old.page

    exact = [c for c in candidates if normalize_text(c.content) == old_norm and old_norm]
    if exact:
        best = min(
            exact, key=lambda c: (not same_page(c), abs(c.ordinal - old.ordinal), c.block_id)
        )
        return BlockRemap(
            old.block_id, "exact", best.block_id, 1.0, old.doc_id, best.doc_id, old.page
        )
    # 边界变了时比较正文：前缀紧贴着各自的首句，带前缀永远构不成包含关系。
    scored = [(coverage(old_body, normalize_text(c.body())), c) for c in candidates if same_page(c)]
    ok = [(cov, c) for cov, c in scored if cov >= min_overlap]
    if ok:
        cov, best = min(
            ok,
            key=lambda x: (
                -x[0],
                abs(len(normalize_text(x[1].body())) - len(old_body)),
                abs(x[1].ordinal - old.ordinal),
                x[1].block_id,
            ),
        )
        return BlockRemap(
            old.block_id, "overlap", best.block_id, cov, old.doc_id, best.doc_id, old.page
        )
    top = max((cov for cov, _ in scored), default=0.0)
    doc = candidates[0].doc_id if candidates else None
    return BlockRemap(
        old.block_id,
        "lost",
        None,
        top,
        old.doc_id,
        doc,
        old.page,
        "无现行候选" if not candidates else f"同页最高覆盖 {top:.2f} < {min_overlap}",
    )


@dataclass(frozen=True)
class CaseRemap:
    q_id: int
    author: str
    old_ids: tuple[int, ...]
    new_ids: tuple[int, ...]
    blocks: tuple[BlockRemap, ...]

    @property
    def changed(self) -> bool:
        return self.old_ids != self.new_ids

    @property
    def kind(self) -> RemapKind:
        return max((b.kind for b in self.blocks), key=lambda k: _RANK[k], default="live")


def case_remap(
    q_id: int, author: str, gold: Sequence[int], resolve: Callable[[int], BlockRemap]
) -> CaseRemap:
    blocks = tuple(resolve(g) for g in gold)
    new_ids: list[int] = []
    for b in blocks:
        target = b.new_block_id if b.kind in ("exact", "overlap") else b.old_block_id
        assert target is not None
        if target not in new_ids:
            new_ids.append(target)
    return CaseRemap(q_id, author, tuple(gold), tuple(new_ids), blocks)


def _live_successor(conn: psycopg.Connection, doc_id: int) -> int | None:
    """沿 supersedes_doc_id 往后找到现行版本；文档本身现行则返回自己。"""
    seen: set[int] = set()
    current = doc_id
    while current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT superseded_at FROM core.document WHERE doc_id = %s", (current,)
        ).fetchone()
        if row is None:
            return None
        if row[0] is None:
            return current
        nxt = conn.execute(
            "SELECT doc_id FROM core.document WHERE supersedes_doc_id = %s"
            " ORDER BY doc_id DESC LIMIT 1",
            (current,),
        ).fetchone()
        if nxt is None:
            return None
        current = int(nxt[0])
    return None


def _leaves(conn: psycopg.Connection, doc_id: int) -> list[BlockText]:
    rows = conn.execute(
        "SELECT block_id, doc_id, page, ordinal, content, section_path FROM core.doc_block"
        " WHERE doc_id = %s AND superseded_at IS NULL AND is_leaf ORDER BY ordinal",
        (doc_id,),
    ).fetchall()
    return [
        BlockText(int(r[0]), int(r[1]), r[2], int(r[3]), str(r[4]), str(r[5] or "")) for r in rows
    ]


def load_case_remaps(
    conn: psycopg.Connection, *, min_overlap: float = MIN_OVERLAP
) -> list[CaseRemap]:
    """读全部评测题的 gold，逐块给出重映射（只读）。"""
    cases = conn.execute(
        "SELECT q_id, author, gold_block_ids FROM evals.eval_retrieval ORDER BY q_id"
    ).fetchall()
    cache: dict[int, BlockRemap] = {}
    leaves_of: dict[int, list[BlockText]] = {}

    def resolve(block_id: int) -> BlockRemap:
        if block_id in cache:
            return cache[block_id]
        row = conn.execute(
            "SELECT doc_id, page, ordinal, content, superseded_at, section_path FROM core.doc_block"
            " WHERE block_id = %s",
            (block_id,),
        ).fetchone()
        if row is None:
            out = BlockRemap(block_id, "lost", None, 0.0, note="块不存在")
        elif row[4] is None:
            out = BlockRemap(block_id, "live", block_id, 1.0, int(row[0]), int(row[0]), row[1])
        else:
            old = BlockText(
                block_id, int(row[0]), row[1], int(row[2]), str(row[3]), str(row[5] or "")
            )
            succ = _live_successor(conn, old.doc_id)
            if succ is None:
                out = BlockRemap(
                    block_id, "lost", None, 0.0, old.doc_id, None, old.page, "文档没有现行后继"
                )
            else:
                if succ not in leaves_of:
                    leaves_of[succ] = _leaves(conn, succ)
                out = remap_block(old, leaves_of[succ], min_overlap=min_overlap)
        cache[block_id] = out
        return out

    return [case_remap(int(q), str(a), [int(g) for g in gold], resolve) for q, a, gold in cases]


def _array(ids: Sequence[int]) -> str:
    return "ARRAY[" + ",".join(str(int(i)) for i in ids) + "]::bigint[]"


def remap_sql(remaps: Sequence[CaseRemap]) -> str:
    """只含 gold 有变化的题。整段一个事务；守卫：gold 必须仍等于生成时读到的值，
    实际更新行数与预期不等就 RAISE，整体回滚（重复执行同一份 SQL 也会因此失败，不会二次改写）。"""
    changed = [r for r in remaps if r.changed]
    lines = [
        "-- 评测 gold 块号重映射（ragdemo docs remap-gold 生成）",
        "-- 审阅后执行：psql -v ON_ERROR_STOP=1 -f <本文件>",
        f"-- 变更题数 {len(changed)}；lost 的块号原样保留，见报告",
        "BEGIN;",
        "CREATE TEMP TABLE gold_remap (q_id bigint PRIMARY KEY, old_ids bigint[] NOT NULL,"
        " new_ids bigint[] NOT NULL) ON COMMIT DROP;",
    ]
    if changed:
        values = ",\n  ".join(
            f"({r.q_id}, {_array(r.old_ids)}, {_array(r.new_ids)})" for r in changed
        )
        lines.append(f"INSERT INTO gold_remap VALUES\n  {values};")
    lines.append(
        "DO $$\nDECLARE n integer; expected integer;\nBEGIN\n"
        "  SELECT count(*) INTO expected FROM gold_remap;\n"
        "  UPDATE evals.eval_retrieval e SET gold_block_ids = r.new_ids\n"
        "    FROM gold_remap r WHERE e.q_id = r.q_id AND e.gold_block_ids = r.old_ids;\n"
        "  GET DIAGNOSTICS n = ROW_COUNT;\n"
        "  IF n <> expected THEN\n"
        "    RAISE EXCEPTION 'gold remap: expected % rows, updated %', expected, n;\n"
        "  END IF;\nEND $$;"
    )
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def remap_report_rows(remaps: Sequence[CaseRemap]) -> list[dict[str, object]]:
    return [
        {
            "q_id": r.q_id,
            "author": r.author,
            "case_kind": r.kind,
            "old_block_id": b.old_block_id,
            "kind": b.kind,
            "new_block_id": b.new_block_id if b.new_block_id is not None else "",
            "coverage": f"{b.coverage:.3f}",
            "old_doc_id": b.old_doc_id if b.old_doc_id is not None else "",
            "new_doc_id": b.new_doc_id if b.new_doc_id is not None else "",
            "page": b.page if b.page is not None else "",
            "note": b.note,
        }
        for r in remaps
        for b in r.blocks
    ]
