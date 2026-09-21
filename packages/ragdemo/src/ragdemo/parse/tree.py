"""父子块构造。

检索与生成对块长度要求相反：检索要短（信号集中），生成要长（上下文完整）。
父子块让两者各取所需（docs/05-document-pipeline.md §4）。

is_leaf 必须是显式的列：表格块既是父块也是叶子块，用「有没有父块」判断
会把所有表格排除出召回。

**返回列表的实际契约（brief 的 Produces 行，逐字实现）**：所有父块在前，
按各自小节在输入里第一次出现的顺序排列；然后是全部叶子块，严格按输入
`leaves` 的原始顺序——不做任何按 section_path 的重新分组或重排。表格叶子
和普通叶子在这一层混排，位置就是它们在原文档里的位置。`ordinal` 在这个
「先父块、后叶子」的完整列表上从 0 连续重排。

这带来一个需要显式接受的推论：`ordinal` 只在叶子层内部编码真实的阅读
顺序（同小节内、跨小节间叶子的相对次序 = 原文档顺序），不跨父子两层
编码顺序——父块是没有独立文档位置的合成聚合体，排在所有叶子之前是
这层信息的正确表达，不是妥协。docs/02-data-model.md §5.2 「文档内顺序，
用于还原上下文」这条注释因此应理解为：对叶子层排序即可还原阅读顺序；
父块本身不参与「还原顺序」这件事，它只用 section_path/parent_ordinal
被叶子引用。

已知局限（有意不修，见 task-3-report.md）：`split_text` 切分长段落时相邻
叶子之间会有 `cfg.overlap_chars` 个字符的真实重叠（chunker.py 既有设计，
供检索用）。`build_tree` 拼接同小节多个叶子为父块内容时**不去重**这部分
重叠——`Chunk` 不携带"这个叶子来自原文档哪个 block"的信息，也不携带
`cfg.overlap_chars`，无法可靠区分"两个叶子是同一个 block 切出的相邻
片段（该去重）"还是"两个叶子来自小节内两个独立 block，只是巧合首尾
有共同字符（不该去重）"。在没有这个信息的前提下做启发式猜测，风险是
把小节内两段本不相关的正文误判为重叠而丢字符，比"父块正文里重复
一点重叠字符"更严重，所以选择记录为已知局限而不是发明一个不保真的
启发式。
"""

from __future__ import annotations

from ragdemo.parse.chunker import Chunk

_TABLE = "table"


def build_tree(leaves: list[Chunk]) -> list[Chunk]:
    """为每个小节建一个父块，返回「父块在前、叶子在后」的完整列表。

    父块：按小节在输入里首次出现的顺序排列，ordinal 从 0 开始。
    叶子：紧随所有父块之后，严格按输入 `leaves` 的原始顺序——不重新分组、
    不按 section_path 聚拢，表格叶子和普通叶子混排在它们原本的位置。

    表格块（block_type == "table"）不参与父块聚合：它既是叶子（可召回）
    也没有父块（不能只给片段，05 §4.2 规则 3），但它仍然按输入顺序出现
    在叶子层里，保留在原文档中的相对位置。
    """
    section_order: list[str] = []
    by_section: dict[str, list[Chunk]] = {}
    for leaf in leaves:
        if leaf.block_type == _TABLE:
            continue
        if leaf.section_path not in by_section:
            by_section[leaf.section_path] = []
            section_order.append(leaf.section_path)
        by_section[leaf.section_path].append(leaf)

    out: list[Chunk] = []
    parent_ordinal_of: dict[str, int] = {}
    for ordinal, section in enumerate(section_order):
        parent_ordinal_of[section] = ordinal
        out.append(_make_parent(by_section[section], ordinal))

    next_ordinal = len(section_order)
    for leaf in leaves:
        if leaf.block_type == _TABLE:
            out.append(_with_ordinal(leaf, next_ordinal, parent=None, is_leaf=True))
        else:
            parent = parent_ordinal_of[leaf.section_path]
            out.append(_with_ordinal(leaf, next_ordinal, parent=parent, is_leaf=True))
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
