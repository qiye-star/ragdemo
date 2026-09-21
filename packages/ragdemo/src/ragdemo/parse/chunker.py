"""切块器。纯函数：NormalizedDocument -> list[Chunk]，不碰数据库、不调网络。

三条硬规则（docs/05-document-pipeline.md §3）：
1. 表格块永不切分，无论多长——切开的表格无法理解；
2. 块不跨小节——跨小节会混入不相关内容，稀释检索信号；
3. 块首拼接 section_path——让块自带上下文。
"""

from __future__ import annotations

from dataclasses import dataclass

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.filters import is_boilerplate

_SENTENCE_ENDERS = "。！？；\n"


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    block_type: str
    section_path: str
    content: str
    page: int | None
    bbox: tuple[float, float, float, float] | None
    is_leaf: bool
    parent_ordinal: int | None = None


def _last_sentence_boundary(text: str, lo: int, hi: int) -> int | None:
    """在 [lo, hi) 区间内找最靠右的句末标点，返回标点之后的位置；找不到则 None。

    调用方保证 lo <= hi，且命中时返回值严格大于 lo（用于保证切分前进）。
    """
    best = -1
    for ender in _SENTENCE_ENDERS:
        idx = text.rfind(ender, lo, hi)
        if idx > best:
            best = idx
    return best + 1 if best != -1 else None


def split_text(text: str, cfg: ChunkConfig) -> list[str]:
    """按句边界切，尽量落在 [leaf_min, leaf_max] 区间，相邻块重叠 overlap_chars。

    优先在句末标点处切分；一段话里如果没有标点（如无标点的长串、长表格行），
    退化为定长滑窗硬切。两种情况都保证前后相邻块有 overlap_chars 的真实重叠，
    且每一步的前进距离恒为正——cfg.validate() 保证 overlap_chars < leaf_min_chars，
    这是切分收敛的关键。
    """
    cfg.validate()
    stripped = text.strip()
    if not stripped:
        return []
    if len(stripped) <= cfg.leaf_max_chars:
        return [stripped]

    n = len(stripped)
    parts: list[str] = []
    start = 0
    while start < n:
        end = min(start + cfg.leaf_max_chars, n)
        if end < n:
            boundary = _last_sentence_boundary(stripped, start + cfg.leaf_min_chars, end)
            if boundary is not None:
                end = boundary
        parts.append(stripped[start:end])
        if end >= n:
            break
        start = end - cfg.overlap_chars
    return parts


def chunk_document(doc: NormalizedDocument, cfg: ChunkConfig) -> list[Chunk]:
    """产出叶子块。父块由 parse.tree 构造。"""
    cfg.validate()
    chunks: list[Chunk] = []
    ordinal = 0

    for block in doc.blocks:
        if cfg.drop_boilerplate and block.block_type != "table" and is_boilerplate(block.content):
            continue

        if block.block_type == "title":
            continue  # 标题不单独成块，它以 section_path 的形式进入每个块

        if block.block_type == "table" and cfg.keep_table_whole:
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    block_type="table",
                    section_path=block.section_path,
                    content=block.content,
                    page=block.page,
                    bbox=block.bbox,
                    is_leaf=True,
                )
            )
            ordinal += 1
            continue

        for piece in split_text(block.content, cfg):
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    block_type=block.block_type,
                    section_path=block.section_path,
                    content=_with_section_prefix(block.section_path, piece),
                    page=block.page,
                    bbox=block.bbox,
                    is_leaf=True,
                )
            )
            ordinal += 1

    return chunks


def _with_section_prefix(section_path: str, text: str) -> str:
    return f"{section_path}\n{text}" if section_path else text
