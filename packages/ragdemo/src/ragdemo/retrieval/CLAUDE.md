# packages/ragdemo/src/ragdemo/retrieval — 时点安全的混合检索：BM25 + 向量 → 加权 RRF → 重排 → 父子块展开

## 这个包做什么
给定 `RetrievalRequest(query, as_of, ...)`，返回带 `block_id` / 页码 / 表格元信息的证据块。流程：时点与归属过滤 → BM25（ParadeDB `pg_search`）与向量（Chroma 或 pgvector）两路各取 `candidate_k` →
加权 RRF 融合到 `fusion_k` → 叶子块内容重排取 `top_k` → 按父块重建完整小节，**引用仍标叶子块 `block_id`**。
位置：`ingest` 入库并嵌入之后；`evals` 与 `api/queries/retrieval.py`、`dataapi/pg.py` 调用它。

## 关键模块
| 文件 | 职责 |
|---|---|
| `service.py` | `RetrievalService.search`（编排）、`chroma_backed_service`（生产构造，拒绝公网 Chroma 地址，用户材料落独立 collection） |
| `types.py` | `RetrievalConfig`（全部可调参数与默认值）、`RetrievalRequest`（`as_of` 必填、必须带时区）、`EvidenceBlock`、`RetrievalTrace` |
| `filters.py` | `build_filters`：时点谓词 + owner 谓词 + entity / doc_type / published_after；两路共用 |
| `lexical.py` | `bm25_search`（`content ||| query`；`bm25_desc` 开时再 OR `content_desc`） |
| `vector.py` / `vector_index.py` | 向量候选：`VectorIndex` 抽象，`ChromaVectorIndex` / `PgVectorIndex`；候选一律回穿 `asof.doc_block` 再确认，不够时按 `oversample_growth` 过采样 |
| `fusion.py` | `weighted_rrf`、`guarantee_vector_top` |
| `rerank.py` / `rerank_siliconflow.py` | `rerank_or_degrade`（超时/异常降级为融合顺序并标 `degraded_reason`）；真实硅基流动重排适配器 |
| `period.py` | 期别（年份）排序偏置 `boost_scores` / `boost_fused` |
| `expand.py` | `expand_to_evidence`：父子块展开与去重（只查 `asof` 视图） |
| `rewrite.py` / `models.py` | 同义词改写（默认关）；`build_models("mock"/"siliconflow")` 构造嵌入器与重排器 |

## 对外入口
`RetrievalService(conn, embedder, reranker, synonyms=None, index=None).search(req, trace=False)`；`build_models`；`RetrievalConfig`。
只依赖 `ragdemo_core.db`、`embed`、`adapters`、`config`；**不得 import `ingest` / `parse` / `evals`**（`ingest/cli.py` 反向用 `models` 与 `vector_index` 是允许方向）。

## 本包的硬规则
- **时点（§1.1，最高优先级）**：只查 `asof.doc_block` / `asof.document`，查询必须包在 `as_of_session` 里（否则 `asof.current_as_of()` 因 GUC 未设报 `InvalidParameterValue`）；时点谓词即使走视图也要显式再写一遍，便于下推进 tantivy/HNSW。**Chroma 不是时点权威**，其结果必须由 PG 视图再过滤（ADR-0009）。
- owner 谓词必须是展开的 `OR ... IS NULL` 形式，不能用 `IS NOT DISTINCT FROM`（与 `paradedb.score()` 同现时 `Unsupported query shape`）；也不要在 `lexical.py` 里对 SQL 做字符串替换。
- 默认值与来由（改默认 = §1.4 评测门禁，数字贴 PR）：
  - `w_bm25=0.6, w_vec=0.4, rrf_k=60, candidate_k=50, fusion_k=50, top_k=10`；
  - `rerank_enabled=True`；`rerank_rich_input=True`（重排输入 = 嵌入同款拼法：标题+章节路径+描述+正文，2026-10-14 经 C2 评测与用户批准）；`rerank_timeout_s=3.0`（经用户批准；接口偶发 7–12 s，超时即降级并标 `rerank_timeout`）；
  - **默认关**：`bm25_desc=False`、`period_boost=0.0`、`rerank_vector_guarantee=0`、`rewrite_enabled=False`。翻这几个默认须过 `eval table-gate` 与对应评测记录（`向量保底进重排评测.md`、`表格检索改进-评测报告.md`）。
- 重排用叶子块内容，不用父块（太长稀释）；`rerank_vector_guarantee` 只在重排开启时生效，总候选数仍是 `fusion_k`。
- `ingested_at` 永远不得出现在比较/过滤里（静态守卫 `tests/test_no_ingested_at_in_filters.py`）。
- 日志含 `run_id`、`as_of`、`entity_id`（结构化）；降级率要监控，持续降级是重排本地化的 ADR-0004 推翻条件。

## 怎么测
- 免费、无库：`uv run --no-sync pytest tests/retrieval -q -m "not db"`（`test_fusion.py`、`test_types.py`、`test_rerank*.py` 等）。
- `-m db`（`test_service.py`、`test_lexical.py`、`test_vector.py`、`test_expand.py`）需要本地 ParadeDB，用 `temp_db` + 共享 `corpus` 夹具（`tests/corpus.py`，根 conftest 注册为插件）；Mock 嵌入/重排，不发请求。
- 真实嵌入/重排只在 `eval run --models siliconflow` 与 `docs embed --models siliconflow` 出现，付费，见 `evals/CLAUDE.md`。

## 常见坑
- BM25 排序只按分数、没有并列决胜：同分块先后取决于行物理位置。原地 UPDATE（如 `reparse-cached`）后必须 `REINDEX INDEX core.doc_block_bm25`，否则评测漂移（见 `ingest/CLAUDE.md`）。
- 同一文档的新旧版本在历史 `as_of` 下可能同时满足可见性（重解析沿用旧 `known_at`）——会出现重复证据，不是检索 bug。
- pgvector `<=>` 优先级低于 `*`，要加括号；本包不写 `<=>` SQL，距离全委托 `VectorIndex.query()`。
- `apply_scan_settings` 用 `SET LOCAL`，必须与向量查询同一事务；`service.search` 外层再包一层事务/保存点是故意的。
- 查询改写只对变体做打折融合，不改过滤条件；开关默认关，需评测证明提升 > 2 个百分点才值得开（`rewrite.py`）。
