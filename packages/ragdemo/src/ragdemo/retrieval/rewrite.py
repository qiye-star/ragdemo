"""查询改写。默认关闭。

开关默认关闭是有意的：改写增加延迟与成本，它是否提升召回必须由评测证明，
不能想当然。`eval_retrieval` 上开关两次跑对比，提升超过 2 个百分点才值得
默认开启（docs/06-retrieval.md §7）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

DEFAULT_SYNONYMS_PATH = Path("config/synonyms.yaml")


def load_synonyms(path: Path = DEFAULT_SYNONYMS_PATH) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): [str(v) for v in vs] for k, vs in raw.items()}


def expand_synonyms(query: str, table: Mapping[str, list[str]]) -> list[str]:
    """返回原查询加上同义词替换后的变体。原查询永远在第一位。"""
    variants = [query]
    for term, alternatives in table.items():
        if term in query:
            variants.extend(query.replace(term, alt) for alt in alternatives)
    # 保序去重。没写成 `not (v in seen or seen.add(v))` 那种常见写法——
    # `set.add()` 返回 None，mypy --strict 会报 func-returns-value。
    seen: set[str] = set()
    deduped: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped
