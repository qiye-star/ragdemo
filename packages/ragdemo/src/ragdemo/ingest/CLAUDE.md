# packages/ragdemo/src/ragdemo/ingest — 取数、解析编排、时点写库：公告/文档、行情、Wind 结构化事实，以及所有 `docs` / `facts` CLI

## 这个包做什么
管线的"入口和落库"一层：适配器取原文/数据 → `parse` 解析切块 → `DocumentWriter` / `PointInTimeWriter` 写 `core.*`（带 `known_at`，旧版只标 `superseded_at`）→ 嵌入。
两种驱动方式：Dagster 资产与调度（`assets*.py`、`definitions.py`、`schedules.py`），以及手动 CLI（`cli.py`、`facts_*_cli.py`）。
两者必须调用**同一批函数**（如 `prepare_documents`），CLI 只是编排。本包很大：文档线（本文件重点）和结构化数据线（Wind 财务/股东/分部/汇率/行情）并存。

## 关键模块
| 文件 | 职责 |
|---|---|
| `documents.py` | `DocumentWriter`：`write_document` / `reparse_document`；块反规范化列必须与 document 一致；四条款只能读 `core.source_registry`；重解析沿用旧文档 `known_at` |
| `writer.py` | `PointInTimeWriter`：更正是追加（`superseded_at` = 新行 `known_at`，不是 `now()`）；`fin_fact_live_uk` 是最后防线 |
| `assets_docs.py` | `prepare_documents`（纯函数）、`build_chunks_and_descriptions`、`tier_c_reparse`、`block_embeddings`、`record_parse_snapshots` |
| `cli.py` | `ragdemo docs` 组：`ingest` `rechunk` `reparse-cached` `remap-gold` `embed` `reconcile-index` `ingest-edgar` `ingest-cninfo` `parse-pending` `render-html` `parse-cost` `announcements` `verify-announcements` `backfill-tables` |
| `reparse_cached.py` | 旧文档缓存零调用重解析：逐块相同 → 原地补 `parse_source`/`figure_meta` + 写逐页台账；有差异 → 默认保留旧块（`differs`），`--replace-differing` 才取代 |
| `gold_remap.py` | 重解析后把评测 `gold_block_ids` 映射到现行后继块（exact → overlap ≥ 0.9），不调模型 |
| `render_html.py` | EDGAR htm 离线转 `.rendered.pdf`（本机 Edge），来源记 `provider_snapshot`（`local-html-render`，成本 0） |
| `announcements.py` / `announcement_index.py` / `cninfo_docs.py` / `hkexnews_docs.py` / `edgar_*.py` | 公告三市场路由、目录去重（`core.announcement_index`）、全文下载 |
| `parse_policy.py` / `parse_pending.py` | 标题 → 解析档位（`ParseTier.FIRST/SECOND/NONE`）；对已存原文补解析 |
| `wind_facts.py` `structured_extras.py` `pit_tables.py` `stage2_*.py` `price*.py` `facts_*cli.py` | 结构化数据线（Wind / 行情三层降级 / 股东分部汇率时点表 / 阶段二体检与交叉核对） |
| `snapshot.py` / `reconcile.py` | `provider_snapshot` 留存（永不改删）；对账与 `ingest_watermark` |
| `definitions.py` / `schedules.py` / `partitions.py` | Dagster 定义、调度、分区窗口 |

## 对外入口
- 入口：`ragdemo docs <cmd>`、`ragdemo facts <cmd>`（由 `ragdemo/cli.py` 注册）；Dagster `Definitions`。
- 被引用：`tables/*` 用 `stage2_crosscheck`，`evals/table_labels.py` 用它的 `FACT_LABELS`；`retrieval/models.py` 被 `docs embed` 用。
- import 方向：可依赖 `adapters`、`parse`、`tables`、`sources`、`quality`、`ops`、`embed`、`ragdemo_core`；**不要**让 `parse`/`retrieval` 反向 import 本包。业务代码不得 import 供应商 SDK，一律走 `adapters/*`。

## 本包的硬规则
- **所有 CLI 默认 dry run，`--yes` 才写库/发请求。** `.env` 里 `RAGDEMO_DSN` 指向**开发库**，`--yes` 即真写；agent 不得自行加 `--yes`，除非用户明确要求。
- **付费/配额命令**（仅在被要求时）：`docs ingest|announcements|parse-pending --yes`（走 TextIn 时；`--parser mock` 不发请求）（TextIn 0.1356 元/页，受 `TEXTIN_MAX_PAGES_PER_RUN` 与月预算约束）；`docs embed --models siliconflow`；`facts * --yes`（Wind 额度）。`reparse-cached`、`backfill-tables`、`rechunk`、`remap-gold`、`render-html` 零外部调用。
- 时点（§1.1）：`known_at` = 公告发布时刻或供应商约定延迟后，**不是** `ingested_at`；重解析、缓存重解析、回填都沿用原 `known_at`。更正 = 追加 + `superseded_at`，不 `ON CONFLICT DO UPDATE`。
- `reparse-cached` 的原地补写**仅限** `parse_source`、`figure_meta`（迁移 033 的运维元数据，不进检索打分、不属时点列）；其他列一律走 supersede。**之后必须 `REINDEX INDEX core.doc_block_bm25`**（见常见坑）。
- 取代旧块会让新旧两版在更早的 `as_of` 上同时可见（检索重复），所以 `--replace-differing` 默认关；评测 gold 随之失效，需 `docs remap-gold`。
- 四条款（can_cache / can_show_raw / can_vectorize / time_precision）只来自 `core.source_registry`，写入不接受调用方覆盖；未登记来源 → `SourceNotRegistered`。
- 用户上传材料带 `owner_user`，不进公共空间；重解析不改归属。
- 原始响应一律先落 `provider_snapshot`（§1.5），被拒的响应也留。

## 怎么测
- 免费、无库：`uv run --no-sync pytest tests/ingest -q -m "not db"`。
- `-m db` 的（`test_reparse_cached.py`、`test_documents.py`、`test_writer.py` 等）用 `temp_db` 随机临时库，**不连开发库**。CLI 测试（`test_docs_cli.py`）用 `--parser mock`，网络类用 `httpx.MockTransport`。
- 不要为验证改动跑真 `--yes`；要看效果用 dry run 或 Mock。

## 常见坑
- 已知失败：`tests/ingest/test_schedules.py::test_prices_schedule_requests_todays_partition_in_beijing_time`（`DagsterUnknownPartitionError: 2026-10-14`，日分区定义里还没有"今天"这个键；与当前日期相关，不是本包逻辑回归）。
- `assets.py`、`assets_docs.py`、`partitions.py`、`schedules.py`、`definitions.py` **刻意不写** `from __future__ import annotations`（Dagster 装饰期直接比较注解对象）；ruff 自动加回会让 Dagster 报 `DagsterInvalidDefinitionError`。
- 原地 UPDATE 会在 pg_search BM25 索引留旧条目、改变词频统计，近似同分块排序漂移（演练中 auto R@10 0.72→0.69）；VACUUM 清不掉，只有 REINDEX。见 `docs/10.12-plan/验收记录/旧文档缓存重解析演练.md`。
- 公告 `known_at` 是日精度（发布日结束），当天内 `known_at > now()`，此时 supersede 会违反 `doc_table_time_order`，回填会跳过（`deferred`）。
- 公告下载只对试点实体（`announcements.DOWNLOAD_ENTITIES`）进行；英文标题公司暂落 `ParseTier.NONE`。
- Windows 控制台 GBK：`--help` 与中文输出先 `export PYTHONUTF8=1`；列 CLI 用 `set -a; . ./.env; set +a; uv run --no-sync ragdemo docs --help`。
