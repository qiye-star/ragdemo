"""解析置信度评分（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 C）。

四项加权，权重写死在代码里，理由：这是一次性设计决策，不是需要按数据调的
超参数——四类失效模式的严重性排序（覆盖率/闭合率 > 乱码 > 缺页）来自
`docs/05-document-pipeline.md` 对失败模式的描述，不是拟合出来的：

- **字符覆盖率**（0.3）——每页字符数是否达到健康阈值的代理指标。xParse 不
  暴露真实的「版面文字覆盖率」，独立 OCR 复核成本又太高；一页解析出的字符数
  低于阈值，大概率意味着这一页部分或整体没被正确提取（图片型 PDF、扫描件
  模糊、表格识别失败退化成空白等）。
- **表格闭合率**（0.3）——渲染出的 Markdown 表格里有没有空单元格。空格通常
  意味着 `cells[]` 没能填满整个行列网格（合并单元格识别错误或表格被截断）。
- **乱码字符比例**（0.2）——非中日韩、非 ASCII、非常见标点的字符占比，编码
  错误或 OCR 噪声的直接信号。
- **页面缺失率**（0.2）——`[1, page_count]` 里完全没有产出任何块的页占比，
  通常意味着整页解析失败或被供应商跳过。

`score_pages()` 按页分别打分——第 7 层的置信度降级要能区分"这份文档整体
还行，但第 12 页是乱码"与"整份文档都很差"，两者对应完全不同的处置（前者
只降级第 12 页引用的块，后者整份转 C 档）。`score_document()` 是没有页码
信息时的整体回退（比如路径 A 供应商结构化接口，不一定带 page_count）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument

W_COVERAGE = 0.3
W_TABLE_CLOSURE = 0.3
W_GARBLED = 0.2
W_MISSING_PAGE = 0.2

# 经验阈值：一页哪怕只有一个章节标题加一句话也远超这个数；真正解析失败的页
# （空白、纯图片未识别）通常是 0 或个位数字符。
MIN_CHARS_PER_PAGE = 40

# 允许出现的字符：中日韩表意文字 + CJK 标点 + 全角符号、ASCII 字母数字与
# 空白、常见西文标点。不在这个集合里的字符计入"乱码"。
_ALLOWED_CHAR = re.compile(
    r"[一-鿿　-〿＀-￯A-Za-z0-9\s"
    r".,;:!?()\[\]{}\"'`~@#$%^&*_+=<>/\\|-]"
)


def _garbled_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(1 for ch in text if not _ALLOWED_CHAR.match(ch))
    return bad / len(text)


def table_looks_closed(markdown_table: str) -> bool:
    """`table_markdown()` 把没有被任何 cell 覆盖的网格位置渲染成空单元格；
    一张真正闭合的表不应该有空单元格（哪怕是合并单元格产生的重复值也是
    非空的，见 `parse/textin.py` 的 `table_markdown` docstring）。"""
    lines = [ln for ln in markdown_table.splitlines() if ln.strip()]
    # 跳过 `|---|---|` 这一行表头分隔符：它由清一色的 '-' 组成。
    data_lines = [ln for ln in lines if set(ln.replace("|", "").strip()) - {"-"}]
    if not data_lines:
        return False
    return all(all(cell.strip() for cell in line.strip("|").split("|")) for line in data_lines)


def _coverage_score(blocks: Sequence[NormalizedBlock], page_count: int | None) -> float:
    if not page_count:
        return 1.0  # 页数未知，这一项不适用——按满分处理，不无中生有一个 0
    expected = page_count * MIN_CHARS_PER_PAGE
    if expected <= 0:
        return 1.0
    total_chars = sum(len(b.content) for b in blocks)
    return min(1.0, total_chars / expected)


def _closure_score(blocks: Sequence[NormalizedBlock]) -> float:
    tables = [b for b in blocks if b.block_type == "table"]
    if not tables:
        return 1.0  # 没有表格，这一项不适用
    closed = sum(1 for b in tables if table_looks_closed(b.content))
    return closed / len(tables)


def _missing_page_ratio(blocks: Sequence[NormalizedBlock], page_count: int | None) -> float:
    """`[1, page_count]` 里有多少比例的页完全没有产出任何块。

    用"覆盖了多少个不同的 page"而不是"blocks 是否为空"——后者在
    `score_document()` 这种跨全文档调用时永远是"非空"（只要文档里有任何
    一页有内容），会让这一项在文档级别形同虚设，实际的缺页情况全部被
    悄悄归给覆盖率一项去承担，两项的失效模式因此混在一起、互相掩盖。
    """
    if not page_count:
        return 0.0  # 页数未知，这一项不适用
    covered = {b.page for b in blocks if b.page is not None}
    return max(0.0, (page_count - len(covered)) / page_count)


def _score(blocks: Sequence[NormalizedBlock], page_count: int | None) -> float:
    coverage = _coverage_score(blocks, page_count)
    closure = _closure_score(blocks)
    garbled = _garbled_ratio("".join(b.content for b in blocks))
    missing = _missing_page_ratio(blocks, page_count)
    score = (
        W_COVERAGE * coverage
        + W_TABLE_CLOSURE * closure
        + W_GARBLED * (1.0 - garbled)
        + W_MISSING_PAGE * (1.0 - missing)
    )
    return round(max(0.0, min(1.0, score)), 4)


def score_document(doc: NormalizedDocument) -> float:
    """文档整体的解析置信度分，落 `core.document.parse_confidence`。"""
    return _score(doc.blocks, doc.page_count)


def score_pages(doc: NormalizedDocument) -> dict[int, float]:
    """按页分别打分，落每个块的 `core.doc_block.parse_confidence`
    （块用自己的 `page` 去查这张表；查不到就回退用 `score_document()`）。

    `doc.page_count` 未知时返回空字典——调用方应该回退到 `score_document()`
    的整体分，而不是假装知道页数去算一个编造出来的分布。
    """
    if not doc.page_count:
        return {}
    by_page: dict[int, list[NormalizedBlock]] = {}
    for b in doc.blocks:
        if b.page is not None:
            by_page.setdefault(b.page, []).append(b)
    return {page: _score(by_page.get(page, []), 1) for page in range(1, doc.page_count + 1)}
