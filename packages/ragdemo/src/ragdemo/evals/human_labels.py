"""人工标注检索评测集：导出标注表 → 标注员逐段写问题 → 导入校验入库。

合成集（`auto:*`）只适合前后对比；总计划阶段 5 的出口指标要用**人工标注**的 100 条裁定。
流程（标注员只用 Excel / WPS 编辑 CSV，不碰命令行）：

1. `ragdemo eval label-sheet --out sheet.csv`：分层抽样 N 个叶子块，每行给出公司、文档、页码和
   完整段落；默认排除已有评测用例用过的源块（人工集与合成集互相独立）。
2. 标注员对**值得出题的行**在 `question` 列写一个自成一体的问题（公司简称与期别写进问题，
   不用"该公司""本期"指代，不含买卖 / 评级 / 目标价用语），`difficulty` 选 easy/medium/hard；
   不出题的行留空即可。可选：`extra_gold_block_ids` 填其他同样能回答该问题的块（`|` 分隔），
   `entity_filter` 填 `Y` 表示评测时按该公司过滤。
3. `ragdemo eval label-import --file sheet.csv --annotator <名字> [--yes]`：先校验（默认只校验，
   一次报全），`--yes` 才写库。gold = 该块 + 同实体内正文完全相同的块 + 额外填写的块。
   author 记为 `human:<名字>`，与合成集始终可区分；`eval run --author-prefix human:` 只跑人工集。
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

from ragdemo.evals.generate import _clean_question, _gold_ids
from ragdemo.evals.sampler import DEFAULT_SEED, sample_candidates

SHEET_COLUMNS = (
    "block_id",
    "entity_id",
    "doc_title",
    "page",
    "passage",
    "question",
    "difficulty",
    "extra_gold_block_ids",
    "entity_filter",
)
_DIFFICULTIES = ("easy", "medium", "hard")


class LabelSheetError(ValueError):
    """校验不过；消息包含全部问题。"""


@dataclass(frozen=True)
class LabeledCase:
    question: str
    gold_block_ids: tuple[int, ...]
    entity_filter: tuple[str, ...] | None
    difficulty: str
    notes: str


def export_label_sheet(
    conn: psycopg.Connection, path: Path, *, n: int, seed: int = DEFAULT_SEED
) -> int:
    used = {
        int(b)
        for (arr,) in conn.execute("SELECT gold_block_ids FROM evals.eval_retrieval")
        for b in arr
    }
    pool = sample_candidates(conn, n=n * 2, seed=seed)
    candidates = [c for c in pool if c.block_id not in used][:n]
    texts = {
        int(r[0]): (str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT b.block_id, b.content, d.title FROM core.doc_block b JOIN core.document d"
            " USING (doc_id) WHERE b.block_id = ANY(%s)",
            ([c.block_id for c in candidates],),
        )
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(SHEET_COLUMNS)
        for c in candidates:
            content, title = texts[c.block_id]
            w.writerow(
                [c.block_id, c.entity_id or "", title, c.page or "", content, "", "", "", ""]
            )
    return len(candidates)


def parse_label_sheet(conn: psycopg.Connection, path: Path) -> list[LabeledCase]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in SHEET_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise LabelSheetError(f"缺少列：{', '.join(missing)}")
        rows = [{k: (v or "").strip() for k, v in r.items() if k} for r in reader]

    alive = {
        int(r[0]): (r[1], str(r[2]), str(r[3]))
        for r in conn.execute(
            "SELECT block_id, entity_id, section_path, content FROM core.doc_block"
            " WHERE is_leaf AND superseded_at IS NULL"
        )
    }
    problems: list[str] = []
    cases: list[LabeledCase] = []
    seen: set[str] = set()
    for i, r in enumerate(rows, start=2):
        if not r["question"]:
            continue  # 不出题的行
        before = len(problems)
        q = _clean_question(r["question"])
        if q is None:
            problems.append(
                f"第 {i} 行问题不合格（须以问号结尾、8-120 字、不含指代词和买卖 / 评级用语）："
                f"{r['question']!r}"
            )
        elif q in seen:
            problems.append(f"第 {i} 行问题重复：{q!r}")
        else:
            seen.add(q)
        if r["difficulty"] not in _DIFFICULTIES:
            problems.append(f"第 {i} 行 difficulty 须为 {'/'.join(_DIFFICULTIES)}")
        try:
            block_id = int(r["block_id"])
            extra = [int(x) for x in r["extra_gold_block_ids"].split("|") if x.strip()]
        except ValueError:
            problems.append(f"第 {i} 行 block_id / extra_gold_block_ids 必须是整数")
            continue
        bad = [b for b in (block_id, *extra) if b not in alive]
        if bad:
            problems.append(f"第 {i} 行块 {bad} 不存在或已被取代")
        if len(problems) > before or q is None:
            continue
        entity, section, content = alive[block_id]
        prefix = f"{section}\n"
        body = content[len(prefix) :] if section and content.startswith(prefix) else content
        gold = tuple(dict.fromkeys([block_id, *_gold_ids(conn, entity, section, body), *extra]))
        use_filter = bool(entity) and r["entity_filter"].upper() in ("Y", "YES", "1", "是")
        cases.append(
            LabeledCase(
                q,
                gold,
                (str(entity),) if use_filter else None,
                r["difficulty"],
                f"人工标注；源块 {block_id}；第 {i} 行",
            )
        )
    if problems:
        raise LabelSheetError("\n".join(problems))
    if not cases:
        raise LabelSheetError("没有任何一行填了 question")
    return cases


def insert_labeled(
    conn: psycopg.Connection, cases: Sequence[LabeledCase], *, as_of: datetime, annotator: str
) -> int:
    for c in cases:
        conn.execute(
            "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids, entity_filter,"
            " difficulty, notes, author) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                c.question,
                as_of,
                list(c.gold_block_ids),
                list(c.entity_filter) if c.entity_filter else None,
                c.difficulty,
                c.notes,
                f"human:{annotator}",
            ),
        )
    conn.commit()
    return len(cases)
