"""表格描述生成。

纯数字表格的向量表示很弱，这句描述是它被语义检索命中的主要途径
（docs/05-document-pipeline.md §3.2）。

描述必须只陈述表里有什么。让小模型顺手「总结一下」，等于在检索层就
掺进了未经验证、无来源的判断——而输出中每个论断都必须可溯源。
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

INTERPRETATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(显示|说明|表明|反映|意味着|体现出)"),
    re.compile(r"(改善|恶化|强劲|疲软|亮眼|承压|超预期|不及预期)"),
    re.compile(r"(预计|预期|有望|将会|料将)"),
    re.compile(r"(大幅|显著|明显)(增长|下滑|提升|下降)"),
)

FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(买入|卖出|增持|减持|推荐|目标价|评级|建议配置|仓位)"),
)


class DescriptionRejected(ValueError):
    """描述含解读、推断或违禁词。"""


def validate_description(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise DescriptionRejected("描述为空")
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(stripped):
            raise DescriptionRejected(f"描述含违禁词: {pattern.pattern}")
    for pattern in INTERPRETATION_PATTERNS:
        if pattern.search(stripped):
            raise DescriptionRejected(f"描述含解读而非陈述: {pattern.pattern}")
    return stripped


@runtime_checkable
class TableDescriber(Protocol):
    model: str
    prompt_version: str

    def describe(self, table_markdown: str, *, title: str, section_path: str) -> str: ...


class MockTableDescriber:
    """确定性描述器。从表头与行数推导，不调模型——测试与离线开发用。"""

    model = "mock"
    prompt_version = "v1"

    def describe(self, table_markdown: str, *, title: str, section_path: str) -> str:
        rows = [r for r in table_markdown.splitlines() if r.strip().startswith("|")]
        if not rows:
            raise DescriptionRejected("不是 Markdown 表格")
        headers = [c.strip() for c in rows[0].strip("| ").split("|") if c.strip()]
        body_count = max(len(rows) - 1, 0)
        leaf_section = section_path.split(">")[-1].strip() or title
        return validate_description(
            f"{leaf_section}表，列为 {'、'.join(headers)}，共 {body_count} 行。"
        )
