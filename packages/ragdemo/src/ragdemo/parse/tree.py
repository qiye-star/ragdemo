"""父子块构造。

检索与生成对块长度要求相反：检索要短（信号集中），生成要长（上下文完整）。
父子块让两者各取所需（docs/05-document-pipeline.md §4）。

is_leaf 必须是显式的列：表格块既是父块也是叶子块，用「有没有父块」判断
会把所有表格排除出召回。
"""

from __future__ import annotations

from ragdemo.parse.chunker import Chunk

_TABLE = "table"
_SECTION = "section"

_GroupKey = tuple[str, str]


def build_tree(leaves: list[Chunk]) -> list[Chunk]:
    """为每个小节建一个父块，返回父块 + 叶子块的完整列表，ordinal 重排且连续。

    表格块直接透传：它既是叶子（可召回）也没有父块（不能只给片段，05 §4.2 规则 3）。

    分组用 `section_path` 做键（docs/05-document-pipeline.md §4.2 规则 1：同一小节的
    内容构成一个父块），但发出顺序按每个分组在输入里第一次出现的位置来——不是
    先发完所有表格再发所有小节。`doc_block.ordinal` 的注释是「文档内顺序，用于
    还原上下文」（docs/02-data-model.md §5.2），如果表格被整体挪到最前面，按
    ordinal 排序还原出的阅读顺序就和原文档对不上了。
    """
    groups: dict[_GroupKey, list[Chunk]] = {}
    order: list[_GroupKey] = []
    for index, leaf in enumerate(leaves):
        key: _GroupKey = (
            (_TABLE, str(index)) if leaf.block_type == "table" else (_SECTION, leaf.section_path)
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(leaf)

    out: list[Chunk] = []
    next_ordinal = 0
    for key in order:
        kind = key[0]
        members = groups[key]
        if kind == _TABLE:
            out.append(_with_ordinal(members[0], next_ordinal, parent=None, is_leaf=True))
            next_ordinal += 1
            continue

        parent_ordinal = next_ordinal
        out.append(_make_parent(members, parent_ordinal))
        next_ordinal += 1
        for member in members:
            out.append(_with_ordinal(member, next_ordinal, parent=parent_ordinal, is_leaf=True))
            next_ordinal += 1

    return out


def _make_parent(members: list[Chunk], ordinal: int) -> Chunk:
    """父块内容 = 小节内所有叶子内容拼接，去掉 chunker.py 逐叶子加的
    `section_path\\n` 前缀（父块自己的 section_path 字段已经携带这个信息，
    照原样拼接会让小节标题在正文里重复 N 次，N = 叶子数）。

    page 取小节内最早出现的页码：引用标注永远用叶子自己的 block_id/page
    （05 §4.3），父块的 page 只用于人工定位小节在原文的起点，取最小页最贴合
    这个用途，且不依赖叶子在输入列表里是否已按页码排好序。
    """
    section = members[0].section_path
    content = "\n".join(_strip_section_prefix(m.content, section) for m in members)
    pages = [m.page for m in members if m.page is not None]
    return Chunk(
        ordinal=ordinal,
        block_type="paragraph",
        section_path=section,
        content=content,
        page=min(pages) if pages else None,
        bbox=None,
        is_leaf=False,
        parent_ordinal=None,
    )


def _strip_section_prefix(content: str, section: str) -> str:
    if not section:
        return content
    prefix = f"{section}\n"
    return content.removeprefix(prefix)


def _with_ordinal(chunk: Chunk, ordinal: int, *, parent: int | None, is_leaf: bool) -> Chunk:
    return Chunk(
        ordinal=ordinal,
        block_type=chunk.block_type,
        section_path=chunk.section_path,
        content=chunk.content,
        page=chunk.page,
        bbox=chunk.bbox,
        is_leaf=is_leaf,
        parent_ordinal=parent,
    )
