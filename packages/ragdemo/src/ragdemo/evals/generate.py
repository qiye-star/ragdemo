"""合成检索评测用例（阶段 5 的 100 条评测集，由模型据真实语料生成）。

**这是合成集，不是人工标注集**——用例的 `author` 一律写 `auto:<模型>:<版本>`，`notes` 记录
生成参数与词面重合度，任何时候都能把它们与人工用例区分开、单独剔除。合成的固有偏差：
问题是看着答案段落写的，词面会偏向答案；所以按目标难度分三档写法（easy 可带关键词，
medium 换说法，hard 抽象间接、不使用原文术语），并按**实测**的词面重合度回填难度标签，
而不是只信生成时的意图。

流程：分层抽样选块（沿用 `evals.sampler`，固定种子可复现）→ 过滤没有检索价值的块（模板句、
勾选项、过短）→ 找出同实体内正文完全相同的块，一并列为 gold（同一句话在多期报告里重复出现，
只标一个会把检索"找对了"判成错）→ 让模型写一个问题 → 拦截合规词与近似原文的问题 → 入库。

问题里如出现买卖 / 评级类字样一律丢弃（CLAUDE.md §0：产品不产出买卖建议，评测也不该
鼓励）；生成失败的块跳过、换下一个候选，直到凑够 `n` 条或候选耗尽。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from ragdemo.adapters.http import HttpClient
from ragdemo.evals.sampler import DEFAULT_SEED, SampleCandidate, sample_candidates
from ragdemo.parse.describe import FORBIDDEN_PATTERNS
from ragdemo.parse.filters import is_boilerplate

PROMPT_VERSION = "v1"
CHAT_ENDPOINT = "/chat/completions"
MIN_BODY_CHARS = 60
MAX_PASSAGE_CHARS = 1500
MAX_GOLD = 5
_CHECKBOX = re.compile(r"^[□√☑✓\s是否适用不适用无]*$")

_STYLE = {
    "easy": "可以直接使用段落里的关键术语提问，问得具体。",
    "medium": "不要照抄段落里的原句，用你自己的话换一种说法提问，关键术语可以用同义词替换。",
    "hard": (
        "问得抽象、间接：不要使用段落里出现的专有术语和数字，"
        "用它的上位概念或者效果、用途去问，让人必须理解语义才能找到这段话。"
    ),
}

_PROMPT = """你在为一个上市公司公告检索系统写评测问题。下面是一段公告原文（来自{doc_title}）。

请写**一个**研究人员可能会问的问题，要求：
1. 这段原文能直接回答这个问题；
2. 问题要自成一体：涉及具体公司或报告期时，把公司简称与期别写进问题里
   （例如"海光信息 2025 年第三季度……"），不要用"该公司""本期"这类指代；
3. {style}
4. 只问原文里明确写着的事实，不问预测、评价、投资建议，
   不出现买入 / 卖出 / 增持 / 减持 / 评级 / 目标价等词；
5. 只输出问题本身，一句话，不要解释、不要编号。

原文：
{passage}
"""


@dataclass(frozen=True)
class GeneratedCase:
    question: str
    gold_block_ids: tuple[int, ...]
    entity_filter: tuple[str, ...] | None
    difficulty: str
    notes: str


@dataclass
class GenerationReport:
    inserted: int = 0
    skipped: dict[str, int] | None = None

    def skip(self, reason: str) -> None:
        if self.skipped is None:
            self.skipped = {}
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[\s，。、；：:,.()（）\[\]|]", "", text)
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def lexical_overlap(question: str, passage: str) -> float:
    """问题里的字二元组有多少比例出现在原文里——越高越容易被词面检索命中。"""
    q = _bigrams(question)
    return len(q & _bigrams(passage)) / len(q) if q else 0.0


def measured_difficulty(overlap: float) -> str:
    """按实测词面重合度定难度标签（阈值是经验值，只在合成集内部相对分档）。"""
    if overlap >= 0.75:
        return "easy"
    if overlap >= 0.5:
        return "medium"
    return "hard"


def _body(content: str, section_path: str) -> str:
    prefix = f"{section_path}\n"
    return content[len(prefix) :] if section_path and content.startswith(prefix) else content


def _usable(body: str, min_chars: int = MIN_BODY_CHARS) -> bool:
    text = body.strip()
    if len(text) < min_chars:
        return False
    return not (_CHECKBOX.match(text) or is_boilerplate(text))


def _gold_ids(
    conn: psycopg.Connection, entity_id: str | None, section_path: str, body: str
) -> list[int]:
    """同实体内正文与这一块完全相同的活块（跨期重复的固定表述），都算正确答案。"""
    rows = conn.execute(
        "SELECT block_id, section_path, content FROM core.doc_block b JOIN core.document d"
        " USING (doc_id) WHERE b.is_leaf AND b.superseded_at IS NULL AND d.superseded_at IS NULL"
        " AND b.entity_id IS NOT DISTINCT FROM %s AND b.content LIKE %s ORDER BY block_id",
        (entity_id, f"%{body[:40]}%"),
    ).fetchall()
    return [int(r[0]) for r in rows if _body(str(r[2]), str(r[1])).strip() == body.strip()][
        :MAX_GOLD
    ]


def _ask(http: HttpClient, model: str, prompt: str) -> str:
    raw = http.post_json(
        CHAT_ENDPOINT,
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 120,
        },
    )
    payload: Any = raw.payload
    content = str(payload["choices"][0]["message"]["content"])
    for line in content.strip().splitlines():
        text = line.strip().strip("\"“”'").strip()
        text = re.sub(r"^\d+[.、)）]\s*", "", text)
        if text:
            return text
    return ""


def _clean_question(question: str) -> str | None:
    q = question.strip()
    if not q or not q.endswith(("？", "?")) or len(q) < 8 or len(q) > 120:
        return None
    if any(p.search(q) for p in FORBIDDEN_PATTERNS):
        return None
    return q


def generate_cases(
    conn: psycopg.Connection,
    http: HttpClient,
    *,
    model: str,
    n: int = 100,
    seed: int = DEFAULT_SEED,
    entity_filter_ratio: float = 0.5,
    min_body_chars: int = MIN_BODY_CHARS,
    candidates: Sequence[SampleCandidate] | None = None,
) -> tuple[list[GeneratedCase], GenerationReport]:
    """生成至多 `n` 条用例（不写库）。候选取 3 倍于 `n` 的分层抽样，逐个尝试直到凑够。"""
    pool = (
        list(candidates)
        if candidates is not None
        else sample_candidates(conn, n=max(n * 3, n), seed=seed)
    )
    report = GenerationReport()
    cases: list[GeneratedCase] = []
    seen_questions: set[str] = set()
    used_blocks: set[int] = set()
    targets = ("easy", "medium", "medium", "hard", "hard")  # 目标难度：20% / 40% / 40%

    for cand in pool:
        if len(cases) >= n:
            break
        row = conn.execute(
            "SELECT b.content, b.section_path, d.title, b.block_type, coalesce(b.content_desc, '')"
            " FROM core.doc_block b JOIN core.document d USING (doc_id) WHERE b.block_id = %s",
            (cand.block_id,),
        ).fetchone()
        if row is None:
            report.skip("block_gone")
            continue
        content, section_path, title, block_type, desc = (str(x) for x in row)
        body = _body(content, section_path)
        if block_type == "table" and desc:
            body = f"{desc}\n{body}"
        if not _usable(body, min_body_chars):
            report.skip("not_useful")
            continue
        if cand.block_id in used_blocks:
            report.skip("dup_block")
            continue
        target = targets[len(cases) % len(targets)]
        prompt = _PROMPT.format(
            doc_title=title, style=_STYLE[target], passage=body[:MAX_PASSAGE_CHARS]
        )
        try:
            question = _clean_question(_ask(http, model, prompt)) or ""
        except Exception as exc:
            report.skip(f"llm_error:{type(exc).__name__}")
            continue
        if not question:
            report.skip("bad_question")
            continue
        overlap = lexical_overlap(question, body)
        if overlap > 0.95:  # 几乎照抄原文
            report.skip("verbatim")
            continue
        if question in seen_questions:
            report.skip("dup_question")
            continue
        gold = _gold_ids(conn, cand.entity_id, section_path, _body(content, section_path))
        gold = gold or [cand.block_id]
        if cand.block_id not in gold:
            gold = [cand.block_id, *gold][:MAX_GOLD]
        seen_questions.add(question)
        used_blocks.update(gold)
        use_filter = cand.entity_id is not None and (len(cases) % 100) < entity_filter_ratio * 100
        cases.append(
            GeneratedCase(
                question=question,
                gold_block_ids=tuple(gold),
                entity_filter=(cand.entity_id,) if use_filter and cand.entity_id else None,
                difficulty=measured_difficulty(overlap),
                notes=(
                    f"合成用例：{model}，提示词 {PROMPT_VERSION}，目标难度 {target}，"
                    f"词面重合度 {overlap:.2f}，源块 {cand.block_id}（{block_type}），"
                    f"{title}；gold 含 {len(gold)} 个内容相同的块"
                ),
            )
        )
    return cases, report


def insert_cases(
    conn: psycopg.Connection, cases: Sequence[GeneratedCase], *, as_of: datetime, author: str
) -> int:
    for c in cases:
        conn.execute(
            "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids, entity_filter,"
            " difficulty, notes, author) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                c.question,
                as_of,
                list(c.gold_block_ids),
                list(c.entity_filter) if c.entity_filter else None,
                c.difficulty,
                c.notes,
                author,
            ),
        )
    conn.commit()
    return len(cases)
