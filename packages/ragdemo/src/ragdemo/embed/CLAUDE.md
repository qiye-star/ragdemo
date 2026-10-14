# packages/ragdemo/src/ragdemo/embed — 嵌入器、内容哈希缓存、批量嵌入与向量索引对账

## 这个包做什么
给 `core.doc_block` 的叶子块生成 1024 维向量（adr/0004），并保证 PG 里的向量与本地 Chroma 候选索引一致（adr/0009）。
待办队列就是 `is_leaf AND embedding IS NULL`，不需要状态表；中断后重跑只处理剩余。
位置：解析/切块之后、检索之前（`ingest` → **embed** → `retrieval`）。

## 关键模块
| 文件 | 职责 |
|---|---|
| `base.py` | `Embedder` Protocol（`model` + `embed(texts)`）、`EMBEDDING_DIM=1024`、`l2_normalize`、`embedding_input`（超长截正文保上下文）、`content_key`（sha256） |
| `batch.py` | `embed_pending_blocks`：每批先提交 PG、再写 `VectorIndex`；待办 SELECT 带 `FOR UPDATE OF b SKIP LOCKED`；返回 `EmbedStats` |
| `cache.py` | `EmbeddingCache`：表 `core.embedding_cache`，主键 `(content_hash, model, owner_user)`，`PUBLIC_OWNER = ""` |
| `reconcile.py` | `reconcile_index`：修复「PG 已写入但 Chroma upsert 失败」造成的永久缺席，返回 `IndexDrift` |
| `mock.py` | `MockEmbedder`：由内容哈希派生的确定性伪向量（离线、可复现） |
| `siliconflow.py` | `SiliconFlowEmbedder`：经 `adapters.http.HttpClient` 调 `POST /v1/embeddings`（bge-m3） |

## 对外入口
- CLI：`ragdemo docs embed [--limit N] [--models mock|siliconflow]`、`ragdemo docs reconcile-index [--repair]`（`ingest/cli.py`）。
- 构造哪个嵌入器：`retrieval/models.py::build_models(mode)`（API、评测、CLI 共用）。检索端同一个 `Embedder` 嵌入查询。
- 本包 import：`adapters.http`/`adapters.errors`（仅 siliconflow）、`retrieval.vector_index`（`VectorIndex`/`VectorItem`）。业务代码不得直接 import 供应商 SDK。

## 本包的硬规则
- 维度固定 1024、写入前 L2 归一化；**查询端也必须归一化**，两端不一致会静默排错序（`base.py`）。
- 先提交 PG、后写向量索引；不得把 `index.upsert` 包进 try/except 吞异常——失败要暴露，由 `reconcile_index` 修复（`reconcile.py` docstring）。
- 私有分区隔离：缓存主键含 `owner_user`；`batch.py` 把缓存哨兵 `""` 映射为 `doc_block.owner_user IS NULL`，默认调用只选公共块。不得让默认调用选中私有块（CLAUDE.md §0）。
- 向量只是候选生成器，时点权威仍是 `asof` 视图（CLAUDE.md §2 / adr/0009）。
- 换模型/换嵌入器 = 「改模型」，必须先跑三套评测集（CLAUDE.md §1.4）；`--models siliconflow` 会发起付费调用并需 `SILICONFLOW_BASE_URL` 指向回环代理，密钥只在代理层。

## 怎么测
仓库根目录运行：`pytest tests/embed`（`test_base.py`、`test_siliconflow.py` 免费无库；`test_batch.py`、`test_cache.py`、`test_reconcile.py` 需数据库 `@pytest.mark.db`，用 `temp_db` 随机空库）。契约：`pytest tests/contracts/test_mock_embedder.py`。均无付费调用。

## 常见坑
- `embed/siliconflow.py` 的模块 docstring 说「不接线」，已过时：现在 `build_models("siliconflow")` 会构造它；**默认仍是 `mock`**（`docs embed`、`eval run`、API 的默认值），库里向量是否为真实向量取决于上次用哪个模式嵌入。
- `embedding_input` 默认截到 2000 字符，改它会改变所有内容哈希，缓存全部失效。
- 首次入库是数十万块，批处理 + 缓存是完成 P1 的前提，不是优化。
