# packages/ragdemo/src/ragdemo/parse — 把原始文档变成带页码的块（切块、置信度、验收、TextIn 封装）

## 这个包做什么
管线位置：`ingest`（取原文）→ **parse**（TextIn 解析 → 切块 → 建父子树 → 打分/验收）→ `ingest.documents` 入库 → `retrieval`。
生产解析只有一条路：TextIn xParse（L3），可选经 `tiered/` 套一层逐页台账与验收。`local/` 是本地确定性排版（L0 v2），
**已休眠**，只剩几个工具函数还在被生产代码引用。本包不碰数据库（`router.py`、`tiered/ledger.py` 除外），不决定入库。

## 关键模块
| 文件 | 职责 |
|---|---|
| `textin.py` | TextIn 客户端 `TextInParser`、`MockDocumentParser`、`blocks_from_detail`（**按 detail[] 切块，不按 markdown**）、`XPARSE_PARAMS`、`param_fingerprint`、`PageBudget`、异常分类 |
| `chunker.py` / `config.py` | `chunk_document`（纯函数）；`ChunkConfig.version` = 算法版本 + 参数指纹，落 `doc_block.chunking_version`（当前 `chunker:v2+…`） |
| `tree.py` | `build_tree`：父子块（叶子块进检索，父块供生成） |
| `describe.py` / `describe_llm.py` / `describer_factory.py` | 表格描述（Mock / DeepSeek），`validate_description` 拒绝违禁词与解读性措辞，只允许陈述，prompt 在 `agents/prompts/table_describe/` |
| `confidence.py` | 四项加权解析置信度（`score_document` / `score_pages`），权重写死 |
| `acceptance.py` | 逐页验收 P1–P8 纯函数，`ACCEPTANCE_VERSION = "acc-v2"`，6 种 `DocState` |
| `router.py` | C 档二次解析路由与月度页预算（读 `core.parse_tier_policy`，读写 `core.parse_retry_queue`） |
| `figures.py` | 图块裁剪成 PNG + 元数据（不做 OCR、不调模型） |
| `textin_audit.py` | 已缓存 TextIn JSON 的逐页数字审计（`PageStore`、`split_detail_by_page`） |
| `filters.py` / `validate.py` | 模板句过滤；块元数据入库前校验 |
| `local/`、`tiered/` | 各有自己的 CLAUDE.md |

## 对外入口
- `ingest.assets_docs.prepare_documents` / `ingest.cli` 调 `TextInParser`（经 `tiered.factory.with_tiered_routing` 包装）→ `chunk_document` → `build_tree` → `validate_chunks`。
- `tables/` 反向被 `textin.py` 引用（`bind_context`、`grid_from_textin_item`）；`tables.verify_cells` 又引用 `parse.local.*`。改 import 时留意循环：`tiered/types.py`、`tiered/records.py` 刻意不 import parse 内其他模块。
- 业务代码不得 import 供应商 SDK（§1.5）：TextIn 走 httpx，认证头由出网代理注入，本包里没有密钥。

## 本包的硬规则
- 切块规则三条不可破（`docs/05-document-pipeline.md` §3）：表格块永不切分、块不跨小节、块首拼 `section_path`；相邻短段落合并不跨页、不跨表。
- 改 `XPARSE_PARAMS` 或 `ChunkConfig` 任一字段 = `param_fp` / `chunking_version` 变化 = 评测集 `gold_block_ids` 失效、存量全量重解析。必须按 §1.4 跑评测；变算法时手动升 `_ALGORITHM_VERSION`。
- `apply_image_analysis` 保持 0（会把图片送大模型，越过 §1.2）；`get_image` 保持 `none`（URL 30 天过期）。
- 私有文档不外送：`TEXTIN_ALLOW_PRIVATE` 默认 false，否则 `PrivateDocumentEgressBlocked`。
- 验收结论保守：TextIn 页数字不能溯源 → `textin_unverified`，**绝不落到 `accepted`**；改 P1–P8 口径要升 `ACCEPTANCE_VERSION`（逐页验收定义见 `docs/10.12-plan/验收记录/逐页验收定义.md`）。
- 页码基：TextIn `page_id` 可能 0 或 1 起，`tiered/page_offset_guard.py` 核对，错一页的块不得入库（§0 溯源）。
- 测试里不得发真实 TextIn / 模型请求（根 conftest 会清掉 `MODEL_BASE_URL`）；TextIn 用 `MockDocumentParser` 或 `httpx.MockTransport`。

## 怎么测
- 免费、不需数据库：`uv run --no-sync pytest tests/parse -q -m "not db"`（子目录 `tests/parse/local`、`tests/parse/tiered`）。
- `tests/parse/test_router.py` 等标 `db` 的需要本地 ParadeDB（根 `tests/conftest.py` 的 `temp_db` 每测试建随机库、结束即删，不碰开发库）。
- 付费调用只发生在 `ragdemo docs ... --yes`（见 `ingest/CLAUDE.md`），测试里没有。

## 常见坑
- 已知失败：`tests/parse/test_figures.py` 3 条裁剪测试（Windows 下 `.png.part` 改名报 FileNotFoundError）；与本包逻辑改动无关，别顺手当回归。
- Windows 控制台是 GBK，`ragdemo ... --help` 与带中文输出要先 `export PYTHONUTF8=1`。
- `confidence.py` 的权重不是调参项；`router.py` 只判断"要不要走 C 档"，真正重解析由 `ingest.assets_docs` 编排。
- 缓存键 = `content_hash` + `param_fp`；参数一改缓存全部失效（`reparse-cached` 因此只能重放同参数的旧缓存）。
