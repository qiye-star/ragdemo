"""父子块：同小节的叶子共享一个父块；表格既是父块也是叶子。"""

from __future__ import annotations

from ragdemo.parse.chunker import Chunk
from ragdemo.parse.tree import build_tree


def _leaf(ordinal: int, section: str, content: str, block_type: str = "paragraph") -> Chunk:
    return Chunk(
        ordinal=ordinal,
        block_type=block_type,
        section_path=section,
        content=content,
        page=1,
        bbox=None,
        is_leaf=True,
    )


def test_same_section_leaves_share_one_parent() -> None:
    tree = build_tree([_leaf(0, "第一节", "甲"), _leaf(1, "第一节", "乙")])
    parents = [c for c in tree if not c.is_leaf]
    leaves = [c for c in tree if c.is_leaf]
    assert len(parents) == 1
    assert {leaf.parent_ordinal for leaf in leaves} == {parents[0].ordinal}


def test_different_sections_get_different_parents() -> None:
    tree = build_tree([_leaf(0, "第一节", "甲"), _leaf(1, "第二节", "乙")])
    assert len({c.ordinal for c in tree if not c.is_leaf}) == 2


def test_parent_content_is_the_concatenated_section() -> None:
    tree = build_tree([_leaf(0, "第一节", "甲甲"), _leaf(1, "第一节", "乙乙")])
    parent = next(c for c in tree if not c.is_leaf)
    assert "甲甲" in parent.content and "乙乙" in parent.content


def test_table_is_both_leaf_and_parentless() -> None:
    """表格既是父块也是叶子块——它不能切，也不能只给片段（05 §4.2 规则 3）。"""
    tree = build_tree([_leaf(0, "第一节", "| a | b |", block_type="table")])
    assert len(tree) == 1
    table = tree[0]
    assert table.is_leaf is True
    assert table.parent_ordinal is None


def test_is_leaf_cannot_be_derived_from_parent_ordinal() -> None:
    """这正是需要显式 is_leaf 列的原因：表格的 parent_ordinal 是 None 但它可被召回。"""
    tree = build_tree(
        [_leaf(0, "第一节", "甲"), _leaf(1, "第一节", "乙"), _leaf(2, "第二节", "| a |", "table")]
    )
    parentless = [c for c in tree if c.parent_ordinal is None]
    assert any(c.is_leaf for c in parentless)  # 表格
    assert any(not c.is_leaf for c in parentless)  # 父块


def test_ordinals_are_reassigned_contiguously() -> None:
    tree = build_tree([_leaf(0, "第一节", "甲"), _leaf(1, "第二节", "乙")])
    assert sorted(c.ordinal for c in tree) == list(range(len(tree)))


def test_single_leaf_section_still_gets_a_parent() -> None:
    """即使小节只有一个叶子，也建父块——生成端的接口才能一致。"""
    tree = build_tree([_leaf(0, "第一节", "甲")])
    assert len(tree) == 2
    assert sum(1 for c in tree if not c.is_leaf) == 1


# ---------------------------------------------------------------------------
# 自查补充：brief 给的 7 条测试用的都是不带 section 前缀、单页、连续排列的
# 简化夹具，掩盖了三个真实场景下会暴露的问题。下面三条用更贴近
# chunk_document 真实输出形状的夹具把它们钉住。
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_tree() -> None:
    assert build_tree([]) == []


def test_every_leaf_parent_ordinal_resolves_to_a_parent_in_the_result() -> None:
    """不只是「存在父块」，而是每个叶子的 parent_ordinal 都要精确命中某个
    实际返回的父块 ordinal——这是生成端展开逻辑（06 §6）能工作的前提。"""
    tree = build_tree(
        [
            _leaf(0, "第一节", "甲"),
            _leaf(1, "第二节", "乙"),
            _leaf(2, "第二节", "丙"),
            _leaf(3, "第三节", "| t |", "table"),
        ]
    )
    parent_ordinals = {c.ordinal for c in tree if not c.is_leaf}
    for leaf in tree:
        if leaf.parent_ordinal is not None:
            assert leaf.parent_ordinal in parent_ordinals


def test_table_keeps_its_relative_position_between_sections() -> None:
    """docs/02-data-model.md §5.2：`doc_block.ordinal` 的注释是「文档内顺序，
    用于还原上下文」。brief 参考实现的两遍扫描（先转发所有表格、再逐小节建父块）会把
    表格一律搬到最前面：三块 [第一节段落, 表格, 第二节段落] 经过它会变成
    [表格, 第一节父块+子块, 第二节父块+子块]——按 ordinal 排序还原出的顺序
    与原文档不符，破坏了这条注释承诺的语义。这里锁住修正后的行为：
    表格仍然夹在两个小节父块之间。"""
    tree = build_tree(
        [
            _leaf(0, "第一节", "甲"),
            _leaf(1, "第一节", "table-between", "table"),
            _leaf(2, "第二节", "乙"),
        ]
    )
    ordered = sorted(tree, key=lambda c: c.ordinal)
    table_index = next(i for i, c in enumerate(ordered) if c.block_type == "table")

    # 表格前面是第一节的父块（其后紧跟第一节的叶子），表格后面紧接第二节的父块。
    assert ordered[0].section_path == "第一节" and not ordered[0].is_leaf
    assert table_index > 0
    assert ordered[table_index + 1].section_path == "第二节"
    assert not ordered[table_index + 1].is_leaf


def test_parent_content_does_not_repeat_the_section_header_per_leaf() -> None:
    """chunk_document 给每个叶子的 content 前缀了 `section_path\\n`
    （chunker.py 的 _with_section_prefix）。父块自己已经有 section_path
    字段，逐叶子原样拼接会让小节标题在父块正文里重复 N 次（N = 叶子数），
    稀释生成阶段本该完整可用的上下文——这正是"父块要不多不少地重建小节"
    这条要求要盯住的问题。"""
    prefixed = [
        _leaf(0, "第一节 概况", "第一节 概况\n甲乙丙"),
        _leaf(1, "第一节 概况", "第一节 概况\n丁戊己"),
    ]
    tree = build_tree(prefixed)
    parent = next(c for c in tree if not c.is_leaf)
    assert parent.content.count("第一节 概况") == 0
    assert "甲乙丙" in parent.content and "丁戊己" in parent.content


def test_parent_page_is_the_earliest_page_among_its_leaves() -> None:
    """父块横跨多页时，page 该填哪一页是本任务要求「刻意选择」的一点：取
    小节内最早出现的页码——引用永远用叶子自己的 page（05 §4.3），父块的
    page 只是给人工阅读时定位小节起点用，选最小页最符合这个用途，并且
    不依赖叶子在输入列表里是否按页码排好序。"""
    members = [
        Chunk(
            ordinal=0,
            block_type="paragraph",
            section_path="第一节",
            content="甲",
            page=5,
            bbox=None,
            is_leaf=True,
        ),
        Chunk(
            ordinal=1,
            block_type="paragraph",
            section_path="第一节",
            content="乙",
            page=3,
            bbox=None,
            is_leaf=True,
        ),
        Chunk(
            ordinal=2,
            block_type="paragraph",
            section_path="第一节",
            content="丙",
            page=4,
            bbox=None,
            is_leaf=True,
        ),
    ]
    tree = build_tree(members)
    parent = next(c for c in tree if not c.is_leaf)
    assert parent.page == 3


def test_parent_page_is_none_when_no_leaf_has_a_page() -> None:
    members = [
        Chunk(
            ordinal=0,
            block_type="paragraph",
            section_path="第一节",
            content="甲",
            page=None,
            bbox=None,
            is_leaf=True,
        ),
    ]
    tree = build_tree(members)
    parent = next(c for c in tree if not c.is_leaf)
    assert parent.page is None
