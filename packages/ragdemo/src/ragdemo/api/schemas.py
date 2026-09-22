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


class KnownAtRange(BaseModel):
    """当前 as_of 下，可见公开文档的 known_at 最早/最晚值。

    两个字段同为 None 表示「这个 as_of 下一篇公开文档都不可见」，不是
    「没查到」——前端据此在 as_of 选择器里给出「往后调时点」的引导，
    而不是留一个不知道该填什么的空输入框。
    """

    earliest: str | None
    latest: str | None


class MetaResponse(BaseModel):
    banner: str
    read_only: bool
    as_of: str
    db: DbIdentity
    visible: VisibleCounts
    known_at_range: KnownAtRange


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


# --- 质量门禁看板 ---------------------------------------------------------


class MetricSpec(BaseModel):
    """指标注册表的一行——前端据此排版，不靠 `SELECT DISTINCT metric` 猜。

    `quality.quality_metric` 里实际有 11 个 metric 名，不是只有 8 个门禁
    （见 docs/superpowers/plans/2026-09-22-web-diagnostic-ui.md 陷阱 1）。
    `direction` 与 `is_gate`/`blocking` 都是静态元数据、不是从数据反推的——
    `MetricResult` 的方向语义本来就没有存进数据库（`quality/metrics.py`
    docstring 明说 record_metric 不猜方向），猜就是在生成论断。
    """

    metric: str
    is_gate: bool
    blocking: bool | None
    direction: str | None  # "higher_is_better" / "lower_is_better" / None
    has_no_sample_sentinel: bool
    description: str


class MetricPoint(BaseModel):
    # None 用在 /metrics/{metric}/history：那条路由的 URL 本身已经点名
    # 了 metric，每个点重复带一遍纯属冗余。/quality/dashboard 汇总多个
    # metric 在同一个列表里，这里必须非 None，否则前端按 metric 分组时
    # 会把所有点都归到同一个 undefined 桶——这正是一次真实发生过的
    # bug（web-diagnostic-ui 计划 Task 12 手工验收抓到）。
    metric: str | None = None
    source_id: str | None
    partition_date: str
    value: float
    threshold: float | None
    passed: bool
    note: str | None
    computed_at: str


class DashboardResponse(BaseModel):
    as_of: str
    source_table: str
    as_of_filter: str
    note: str
    metrics: list[MetricPoint]


class MetricHistoryResponse(BaseModel):
    as_of: str
    metric: str
    source_id: str | None
    points: list[MetricPoint]


# --- 分档与预算 -----------------------------------------------------------


class TierBucket(BaseModel):
    tier: str
    documents: int
    budget_exceeded: int
    confidence_avg: float | None
    confidence_min: float | None


class ParseEngineBucket(BaseModel):
    parse_engine: str | None
    documents: int


class TierDistributionResponse(BaseModel):
    as_of: str
    tier_rule: str
    tiers: list[TierBucket]
    by_parse_engine: list[ParseEngineBucket]


class PolicyRow(BaseModel):
    policy_id: int
    doc_type: str | None
    confidence_below: float | None
    closure_below: float | None
    monthly_cap_cny: str  # Decimal 序列化成字符串，不是 float——金额精度不能丢
    enabled: bool
    known_at: str
    superseded_at: str | None
    active_at_as_of: bool


class PoliciesResponse(BaseModel):
    as_of: str
    policies: list[PolicyRow]


class BudgetBucket(BaseModel):
    doc_type: str
    pages_spent: int
    rows: int  # 重复计入的原始行数——与 monthly_pages_spent 用同一条裸 sum(value)，
    # 哪怕会把 run 重试写入的重复行算两遍：暴露问题，不是修问题。
    cap_cny: str | None
    cost_per_page_configured: bool
    spent_cny: str | None


class BudgetResponse(BaseModel):
    as_of: str
    month_start: str
    month_end: str
    buckets: list[BudgetBucket]


class RetryQueueRow(BaseModel):
    source: str
    provider_doc_id: str
    first_failed_at: str
    retry_deadline: str
    last_seen_at: str
    attempts: int
    overdue: bool


class RetryQueueResponse(BaseModel):
    as_of: str
    point_in_time: str
    reason: str
    entries: list[RetryQueueRow]


class WarningAggregate(BaseModel):
    warning: str
    documents: int


class WarningDocument(BaseModel):
    doc_id: int
    doc_type: str
    title: str
    source: str
    publish_at: str
    page_count: int | None
    parse_engine: str | None
    parse_confidence: float | None


class WarningsResponse(BaseModel):
    as_of: str
    aggregate: list[WarningAggregate]
    documents: list[WarningDocument]


# --- 权限隔离探针 ---------------------------------------------------------


class RelationCounts(BaseModel):
    """一个身份分支在一张关系（asof.document 或 asof.doc_block）上看到的
    行数分布——只有聚合计数，一个文本列都不取（约束 6：私有内容永不返回）。
    """

    total: int
    public_rows: int
    user_private_rows: int
    tenant_private_rows: int
    empty_owner_rows: int
    foreign_user_rows: int
    foreign_tenant_rows: int


class IdentityBranch(BaseModel):
    key: str
    label: str
    tenant: str | None
    user: str | None
    documents: RelationCounts
    blocks: RelationCounts


class ErrorBranch(BaseModel):
    key: str
    raised: bool
    sqlstate: str | None
    message_head: str | None
    passed: bool


class CheckResult(BaseModel):
    name: str
    status: str  # "passed" / "failed" / "skipped"
    detail: str


class ProbesResponse(BaseModel):
    as_of: str
    demo_seeded: bool
    identity_branches: list[IdentityBranch]
    checks: list[CheckResult]
    error_branches: list[ErrorBranch]


class PolicyDefinition(BaseModel):
    schemaname: str
    tablename: str
    policyname: str
    cmd: str
    roles: list[str]
    qual: str | None
    with_check: str | None


class RlsStatus(BaseModel):
    relname: str
    relrowsecurity: bool
    relforcerowsecurity: bool
    owner: str
    owner_is_superuser: bool


class AsofViewOwner(BaseModel):
    relname: str
    owner: str
    owner_is_superuser: bool


class ConnectionIdentity(BaseModel):
    current_user: str
    session_user: str
    current_user_is_superuser: bool
    asof_doc_block_select: bool
    core_doc_block_select: bool
    core_doc_block_insert: bool
    core_document_select: bool
    quality_metric_select: bool


class CatalogResponse(BaseModel):
    as_of: str
    as_of_affects_result: bool
    policies: list[PolicyDefinition]
    rls_status: list[RlsStatus]
    asof_view_owners: list[AsofViewOwner]
    connection_identity: ConnectionIdentity


# --- 检索诊断 -----------------------------------------------------------


class ModelsInfo(BaseModel):
    embedder: str
    reranker: str
    embedder_is_mock: bool
    reranker_is_mock: bool
    corpus_embedding_versions: list[str]
    # False 时向量列的排名没有语义意义（当前 embedder 与语料的
    # embedding_version 对不上，或语料里根本没有向量）——界面必须显式
    # 标注这件事，不能把噪声当排名端上来（web-diagnostic-ui 计划裁决 5）。
    vector_path_is_meaningful: bool


class RetrievalStatsOut(BaseModel):
    bm25_hits: int
    vec_hits: int
    after_fusion: int
    after_rerank: int
    ms_bm25: float
    ms_vec: float
    ms_rerank: float
    ms_total: float
    degraded: bool
    rerank_attempted: bool


class CoverageInfo(BaseModel):
    leaf_blocks: int
    with_embedding: int


class RetrievalRow(BaseModel):
    block_id: int
    doc_id: int
    doc_title: str
    section_path: str
    page: int | None
    preview: str
    bm25_rank: int | None
    bm25_score: float | None
    vec_rank: int | None
    vec_score: float | None
    fused_rank: int | None
    fused_score: float | None
    rerank_rank: int | None
    rerank_score: float | None
    final_position: int | None


class RetrievalSearchResponse(BaseModel):
    query: str
    as_of: str
    models: ModelsInfo
    stats: RetrievalStatsOut
    coverage: CoverageInfo
    rows: list[RetrievalRow]
