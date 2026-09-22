"""build_tree：纯函数，不需要数据库。

28 个无父表格叶子（parent_block_id IS NULL AND is_leaf）是 tree.py 的设计
本身，不是数据缺陷——build_tree 必须把它们算作 orphan_leaf_count 而不是
悄悄丢弃或报错。
"""

from __future__ import annotations

from ragdemo.api.queries.blocks import RawBlock, build_tree


def _block(
    block_id: int,
    parent_block_id: int | None,
    *,
    block_type: str = "paragraph",
    section_path: str = "",
    ordinal: int = 0,
    is_leaf: bool = True,
    page: int | None = 1,
    char_len: int | None = 300,
) -> RawBlock:
    return RawBlock(
        block_id=block_id,
        parent_block_id=parent_block_id,
        block_type=block_type,
        section_path=section_path,
        ordinal=ordinal,
        page=page,
        is_leaf=is_leaf,
        char_len=char_len,
    )


def test_build_tree_nests_children_by_parent_id() -> None:
    blocks = [
        _block(1, None, block_type="title", section_path="第一节", ordinal=0, is_leaf=False),
        _block(2, 1, section_path="第一节", ordinal=1),
        _block(3, 1, section_path="第一节", ordinal=2),
    ]
    result = build_tree(blocks)
    assert result.node_count == 3
    assert len(result.roots) == 1
    root = result.roots[0]
    assert root.block_id == 1
    assert [c.block_id for c in root.children] == [2, 3]
    assert result.cycle_detected is False


def test_build_tree_counts_orphan_leaves() -> None:
    """28 个无父表格叶子：parent_block_id IS NULL 且 is_leaf——它们各自
    成为一个没有子节点的 root，而不是被丢弃。"""
    blocks = [
        _block(1, None, block_type="title", section_path="s", ordinal=0, is_leaf=False),
        _block(2, 1, section_path="s", ordinal=1),
        _block(3, None, block_type="table", section_path="s", ordinal=2, is_leaf=True),
    ]
    result = build_tree(blocks)
    assert result.orphan_leaf_count == 1
    assert len(result.roots) == 2
    table_root = next(r for r in result.roots if r.block_id == 3)
    assert table_root.children == []


def test_build_tree_detects_cycle_without_recursing_forever() -> None:
    """doc_block 只有 CHECK(parent_block_id IS DISTINCT FROM block_id)，
    挡得住自环挡不住 A->B->A——build_tree 必须靠自己的环检测终止递归。"""
    blocks = [
        _block(1, 2, section_path="s", ordinal=0),
        _block(2, 1, section_path="s", ordinal=1),
    ]
    result = build_tree(blocks)
    assert result.cycle_detected is True
    # 不应该无限递归——测试本身能跑完就是最直接的证明。
    assert result.node_count == 2


def test_build_tree_counts_section_path_mismatch() -> None:
    blocks = [
        _block(1, None, block_type="title", section_path="第一节", ordinal=0, is_leaf=False),
        _block(2, 1, section_path="第二节完全不同", ordinal=1),
    ]
    result = build_tree(blocks)
    assert result.section_path_mismatch_count == 1


def test_build_tree_max_depth_counts_nesting_levels() -> None:
    blocks = [
        _block(1, None, block_type="title", section_path="a", ordinal=0, is_leaf=False),
        _block(2, 1, block_type="title", section_path="a", ordinal=1, is_leaf=False),
        _block(3, 2, section_path="a", ordinal=2),
    ]
    result = build_tree(blocks)
    assert result.max_depth == 2


def test_build_tree_handles_empty_input() -> None:
    result = build_tree([])
    assert result.node_count == 0
    assert result.roots == []
    assert result.cycle_detected is False


def test_build_tree_dangling_parent_reference_becomes_its_own_root() -> None:
    """parent_block_id 指向一个不在这批结果里的 block_id（理论上不该发生，
    FK 只保证父块存在，不保证与子块同一 doc_id）——这样的块不该从 roots
    里悄悄消失。"""
    blocks = [_block(5, 999, section_path="s", ordinal=0)]
    result = build_tree(blocks)
    assert [r.block_id for r in result.roots] == [5]
