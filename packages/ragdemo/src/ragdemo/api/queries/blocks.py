"""块查询：SQL + 行映射 + 纯函数 build_tree。不 import fastapi——可以脱离
HTTP 层单独在 REPL/单测里跑，`build_tree` 更是完全不碰连接。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import psycopg

from ragdemo.api.schemas import BlockAncestor, BlockDetail, FlatBlock, TreeNode, TreeResponse
from ragdemo.api.serialize import as_bbox, as_int, as_optional_float, as_optional_str, as_str

_PREVIEW_CHARS = 120

# 扁平块查询显式列清单——不用 SELECT *，防止 1024 维的 embedding 或未来
# 新增的敏感列悄悄混进响应（tests/api/test_sql_guards.py 会扫这一点）。
_FLAT_BLOCK_SQL = """
    SELECT block_id, parent_block_id, block_type::text, section_path, ordinal,
           page, is_leaf, char_len, parse_confidence,
           left(content, %(preview_chars)s) AS preview
      FROM asof.doc_block
     WHERE doc_id = %(doc_id)s
       AND owner_tenant IS NULL AND owner_user IS NULL
     ORDER BY ordinal
"""


def list_blocks(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    doc_id: int,
    as_of: datetime,
) -> list[FlatBlock] | None:
    """None 表示这份文档在这个 as_of/可见性下根本不存在——与"文档存在但
    零块可见"（空列表，如 edgar 来源零块的真实文档）区分开，否则未知
    doc_id 会静默返回一个空列表而不是 404，和 get_tree/get_document 的
    404 契约不一致。
    """
    from ragdemo_core.db.session import as_of_session

    with as_of_session(conn, as_of):
        doc_row = conn.execute(
            "SELECT 1 FROM asof.document"
            " WHERE doc_id = %(doc_id)s AND owner_tenant IS NULL AND owner_user IS NULL",
            {"doc_id": doc_id},
        ).fetchone()
        if doc_row is None:
            return None
        rows = conn.execute(
            _FLAT_BLOCK_SQL, {"doc_id": doc_id, "preview_chars": _PREVIEW_CHARS}
        ).fetchall()
    return [
        FlatBlock(
            block_id=as_int(r[0]),
            parent_block_id=as_int(r[1]) if r[1] is not None else None,
            block_type=as_str(r[2]),
            section_path=as_str(r[3]),
            ordinal=as_int(r[4]),
            page=as_int(r[5]) if r[5] is not None else None,
            is_leaf=bool(r[6]),
            char_len=as_int(r[7]) if r[7] is not None else None,
            parse_confidence=as_optional_float(r[8]),
            preview=as_str(r[9]),
        )
        for r in rows
    ]


@dataclass(frozen=True)
class RawBlock:
    """build_tree 的输入形状——从数据库行剥离出来的纯数据，
    不带任何 psycopg/asyncpg 依赖，方便单测直接构造。"""

    block_id: int
    parent_block_id: int | None
    block_type: str
    section_path: str
    ordinal: int
    page: int | None
    is_leaf: bool
    char_len: int | None


@dataclass(frozen=True)
class TreeBuildResult:
    roots: list[TreeNode]
    node_count: int
    max_depth: int
    orphan_leaf_count: int
    section_path_mismatch_count: int
    cycle_detected: bool


@dataclass
class _BuildStats:
    """build_tree 内部递归时的可变累加状态——用一个非 frozen dataclass
    而不是嵌套函数里的 nonlocal，纯粹是可读性选择：属性名比裸变量名
    更清楚地表明这几个数字是同一次构建过程的伴生统计量。"""

    cycle_detected: bool = False
    section_mismatch: int = 0
    max_depth: int = 0


def _detect_upward_cycle(by_id: dict[int, RawBlock]) -> bool:
    """沿 parent_block_id 走亲代链，专门抓"整条链都在环里、从任何 root
    都到不了"的退化情况——这类节点全部互为祖先，root 遍历永远碰不到它们，
    必须单独查一遍父指针图本身（三色标记法：灰色=在当前路径上，
    再次撞见灰色节点即成环）。
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[int, int] = dict.fromkeys(by_id, WHITE)

    def visit(node_id: int) -> bool:
        color[node_id] = GRAY
        parent_id = by_id[node_id].parent_block_id
        if parent_id is not None and parent_id in by_id:
            if color[parent_id] == GRAY:
                return True
            if color[parent_id] == WHITE and visit(parent_id):
                return True
        color[node_id] = BLACK
        return False

    return any(color[block_id] == WHITE and visit(block_id) for block_id in by_id)


def build_tree(blocks: list[RawBlock]) -> TreeBuildResult:
    """按 parent_block_id 建两层树（章节父块 + 叶子）。

    28 个表格叶子 `parent_block_id IS NULL` 是 `tree.py` 的设计本身
    （表格块既是叶子又不参与父块聚合），不是数据缺陷——它们在这里各自
    成为一个没有子节点的 root，由 `orphan_leaf_count` 显式计数，而不是
    被过滤掉或报错。

    `doc_block` 只有 `CHECK(parent_block_id IS DISTINCT FROM block_id)`，
    挡得住自环挡不住 A→B→A——这里用一个「当前路径上的祖先集合」做环检测，
    命中就把 `cycle_detected` 置真并停止在那个方向继续递归，不会无限循环。
    """
    by_id = {b.block_id: b for b in blocks}
    children: dict[int | None, list[RawBlock]] = defaultdict(list)
    for b in blocks:
        children[b.parent_block_id].append(b)
    for lst in children.values():
        lst.sort(key=lambda b: b.ordinal)

    orphan_leaf_count = sum(1 for b in blocks if b.parent_block_id is None and b.is_leaf)

    stats = _BuildStats()
    # 独立于「从 roots 出发遍历」的环检测：一个环如果不含任何
    # parent_block_id IS NULL 的节点、也不含悬空引用，它就完全够不成任何
    # root，_build() 从 roots 出发的深度优先遍历永远走不到它——这类"纯环"
    # （例如两节点互为父子）必须单独查一遍，不能只靠遍历时顺手发现。
    stats.cycle_detected = _detect_upward_cycle(by_id)

    def _build(block: RawBlock, ancestors: frozenset[int], depth: int) -> TreeNode:
        stats.max_depth = max(stats.max_depth, depth)
        kid_nodes: list[TreeNode] = []
        for child in children.get(block.block_id, []):
            if child.block_id in ancestors:
                stats.cycle_detected = True
                continue
            if block.section_path and not child.section_path.startswith(block.section_path):
                stats.section_mismatch += 1
            kid_nodes.append(_build(child, ancestors | {block.block_id}, depth + 1))
        return TreeNode(
            block_id=block.block_id,
            block_type=block.block_type,
            section_path=block.section_path,
            ordinal=block.ordinal,
            page=block.page,
            is_leaf=block.is_leaf,
            char_len=block.char_len,
            children=kid_nodes,
        )

    # 正常根节点：parent_block_id IS NULL。
    # 悬空引用：parent_block_id 指向一个不在这批结果里的 block_id——理论上
    # 不该发生（FK 只保证父块存在，不保证与子块同一 doc_id），但真出现时
    # 不该让这个块从 roots 里悄悄消失，诊断工具应该让它可见。
    dangling = [
        b for b in blocks if b.parent_block_id is not None and b.parent_block_id not in by_id
    ]
    root_candidates = sorted(children.get(None, []) + dangling, key=lambda b: b.ordinal)

    roots = [_build(b, frozenset({b.block_id}), 0) for b in root_candidates]

    return TreeBuildResult(
        roots=roots,
        node_count=len(blocks),
        max_depth=stats.max_depth,
        orphan_leaf_count=orphan_leaf_count,
        section_path_mismatch_count=stats.section_mismatch,
        cycle_detected=stats.cycle_detected,
    )


_BLOCK_DETAIL_SQL = """
    SELECT block_id, doc_id, parent_block_id, block_type::text, section_path, ordinal,
           page, bbox, is_leaf, char_len, parse_confidence, content, content_desc,
           table_html, chunking_version, embedding_version
      FROM asof.doc_block
     WHERE block_id = %(block_id)s
       AND owner_tenant IS NULL AND owner_user IS NULL
"""

_ANCESTOR_CHAIN_SQL = """
    WITH RECURSIVE chain AS (
        SELECT block_id, parent_block_id, block_type::text, section_path, 0 AS depth
          FROM asof.doc_block
         WHERE block_id = %(block_id)s
           AND owner_tenant IS NULL AND owner_user IS NULL
        UNION ALL
        SELECT p.block_id, p.parent_block_id, p.block_type::text, p.section_path,
               c.depth + 1
          FROM asof.doc_block p
          JOIN chain c ON p.block_id = c.parent_block_id
         WHERE p.owner_tenant IS NULL AND p.owner_user IS NULL
    )
    SELECT block_id, block_type, section_path, depth FROM chain ORDER BY depth DESC
"""


def get_block(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    block_id: int,
    as_of: datetime,
) -> BlockDetail | None:
    """单块全文 + 祖先链。约束 6 的第一道防线在 SQL 里（owner_* 过滤），
    RLS 是第二道；不可见/不存在两种情况刻意返回同一个 None——路由层据此
    统一报 404，不区分"存在但私有"与"根本不存在"，可区分本身就是一个
    存在性侧信道。
    """
    from ragdemo_core.db.session import as_of_session

    with as_of_session(conn, as_of):
        row = conn.execute(_BLOCK_DETAIL_SQL, {"block_id": block_id}).fetchone()
        if row is None:
            return None
        ancestor_rows = conn.execute(_ANCESTOR_CHAIN_SQL, {"block_id": block_id}).fetchall()

    bbox, bbox_malformed = as_bbox(row[7])
    # 祖先链的最后一行（depth 最大）就是块自身，去掉它只留真正的祖先。
    ancestors = [
        BlockAncestor(
            block_id=as_int(r[0]),
            block_type=as_str(r[1]),
            section_path=as_str(r[2]),
            depth=as_int(r[3]),
        )
        for r in ancestor_rows[:-1]
    ]
    return BlockDetail(
        block_id=as_int(row[0]),
        doc_id=as_int(row[1]),
        parent_block_id=as_int(row[2]) if row[2] is not None else None,
        block_type=as_str(row[3]),
        section_path=as_str(row[4]),
        ordinal=as_int(row[5]),
        page=as_int(row[6]) if row[6] is not None else None,
        bbox=bbox,
        bbox_malformed=bbox_malformed,
        is_leaf=bool(row[8]),
        char_len=as_int(row[9]) if row[9] is not None else None,
        parse_confidence=as_optional_float(row[10]),
        content=as_str(row[11]),
        content_desc=as_optional_str(row[12]),
        table_html=as_optional_str(row[13]),
        chunking_version=as_optional_str(row[14]),
        embedding_version=as_optional_str(row[15]),
        ancestors=ancestors,
    )


_TREE_SOURCE_SQL = """
    SELECT block_id, parent_block_id, block_type::text, section_path, ordinal,
           page, is_leaf, char_len
      FROM asof.doc_block
     WHERE doc_id = %(doc_id)s AND owner_tenant IS NULL AND owner_user IS NULL
     ORDER BY ordinal
"""


def get_tree(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    doc_id: int,
    as_of: datetime,
) -> TreeResponse | None:
    """文档存在但没有任何可见块（第一道防线过滤掉全部私有块，或者这份
    文档本身零块——如 edgar 来源零块的真实文档）时返回一棵空树，
    不是 None；None 只表示"这份文档在这个 as_of/可见性下根本不存在"。
    """
    from ragdemo_core.db.session import as_of_session

    with as_of_session(conn, as_of):
        doc_row = conn.execute(
            "SELECT 1 FROM asof.document"
            " WHERE doc_id = %(doc_id)s AND owner_tenant IS NULL AND owner_user IS NULL",
            {"doc_id": doc_id},
        ).fetchone()
        if doc_row is None:
            return None
        rows = conn.execute(_TREE_SOURCE_SQL, {"doc_id": doc_id}).fetchall()

    raw_blocks = [
        RawBlock(
            block_id=as_int(r[0]),
            parent_block_id=as_int(r[1]) if r[1] is not None else None,
            block_type=as_str(r[2]),
            section_path=as_str(r[3]),
            ordinal=as_int(r[4]),
            page=as_int(r[5]) if r[5] is not None else None,
            is_leaf=bool(r[6]),
            char_len=as_int(r[7]) if r[7] is not None else None,
        )
        for r in rows
    ]
    result = build_tree(raw_blocks)
    return TreeResponse(
        doc_id=doc_id,
        roots=result.roots,
        node_count=result.node_count,
        max_depth=result.max_depth,
        orphan_leaf_count=result.orphan_leaf_count,
        section_path_mismatch_count=result.section_path_mismatch_count,
        cycle_detected=result.cycle_detected,
    )


__all__ = (
    "RawBlock",
    "TreeBuildResult",
    "build_tree",
    "get_block",
    "get_tree",
    "list_blocks",
)
