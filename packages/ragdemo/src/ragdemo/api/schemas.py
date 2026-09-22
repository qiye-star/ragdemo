"""诊断接口的响应模型。

用 pydantic `response_model` 有一个额外好处，不只是文档：FastAPI 按模型
字段序列化响应体，模型里没有的字段会被**丢弃**。约束 6/7（私有内容不可见、
不生成论断文案）因此多了一层机械保证——将来谁在某条查询里手滑多选了一列
（比如 content），只要没把它加进对应的响应模型，它就到不了 HTTP 响应。
"""

from __future__ import annotations

from pydantic import BaseModel


class DbIdentity(BaseModel):
    current_user: str
    session_user: str
    current_user_is_superuser: bool
    server_version: str


class VisibleCounts(BaseModel):
    documents: int
    blocks: int


class MetaResponse(BaseModel):
    banner: str
    read_only: bool
    as_of: str
    db: DbIdentity
    visible: VisibleCounts


# --- 文档 --------------------------------------------------------------


class DocumentSummary(BaseModel):
    doc_id: int
    entity_id: str | None
    doc_type: str
    title: str
    period: str | None
    publish_at: str
    language: str
    source: str
    page_count: int | None
    parse_engine: str | None
    parse_confidence: float | None
    known_at: str
    version_group_id: int
    is_correction: bool
    supersedes_doc_id: int | None
    parse_warnings: list[str]
    can_show_raw: bool
    time_precision: str | None
    block_count: int
    leaf_count: int


class DocumentListResponse(BaseModel):
    as_of: str
    documents: list[DocumentSummary]


class BlockTypeCount(BaseModel):
    block_type: str
    blocks: int
    with_bbox: int
    leaves: int


class PageBlockCount(BaseModel):
    page: int | None
    blocks: int


class DocumentDetail(DocumentSummary):
    block_type_counts: list[BlockTypeCount]
    page_block_counts: list[PageBlockCount]


# --- 版面还原（bbox 叠加） ------------------------------------------------


class LayoutBlock(BaseModel):
    block_id: int
    parent_block_id: int | None
    block_type: str
    section_path: str
    ordinal: int
    page: int | None
    bbox: tuple[float, float, float, float] | None
    bbox_malformed: bool
    is_leaf: bool
    char_len: int | None
    parse_confidence: float | None
    preview: str
    has_desc: bool
    has_table_html: bool


class LayoutResponse(BaseModel):
    doc_id: int
    page: int
    page_count: int | None
    # bbox 已归一化到 [0,1]；数据库没有存页面物理尺寸，前端不该自行猜测坐标系。
    bbox_normalized: bool
    bbox_missing_count: int
    blocks: list[LayoutBlock]


# --- 块（扁平 / 树 / 单块） -----------------------------------------------


class FlatBlock(BaseModel):
    block_id: int
    parent_block_id: int | None
    block_type: str
    section_path: str
    ordinal: int
    page: int | None
    is_leaf: bool
    char_len: int | None
    parse_confidence: float | None
    preview: str


class BlocksResponse(BaseModel):
    doc_id: int
    as_of: str
    blocks: list[FlatBlock]


class TreeNode(BaseModel):
    block_id: int
    block_type: str
    section_path: str
    ordinal: int
    page: int | None
    is_leaf: bool
    char_len: int | None
    children: list[TreeNode]


class TreeResponse(BaseModel):
    doc_id: int
    roots: list[TreeNode]
    node_count: int
    max_depth: int
    # 28 个表格叶子 parent_block_id IS NULL 是 tree.py 的设计本身，不是缺陷——
    # 这个数字只是让它可见，不是判定。
    orphan_leaf_count: int
    section_path_mismatch_count: int
    cycle_detected: bool


class BlockAncestor(BaseModel):
    block_id: int
    block_type: str
    section_path: str
    depth: int


class BlockDetail(BaseModel):
    block_id: int
    doc_id: int
    parent_block_id: int | None
    block_type: str
    section_path: str
    ordinal: int
    page: int | None
    bbox: tuple[float, float, float, float] | None
    bbox_malformed: bool
    is_leaf: bool
    char_len: int | None
    parse_confidence: float | None
    content: str
    content_desc: str | None
    table_html: str | None
    chunking_version: str | None
    embedding_version: str | None
    ancestors: list[BlockAncestor]
