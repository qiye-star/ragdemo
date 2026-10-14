# packages/ragdemo/src/ragdemo/evals — 检索评测：题集生成/标注/导入、运行并记 `evals.eval_run`、冻结语料、表格专项门槛

## 这个包做什么
CLAUDE.md §1.4 的执行器：改 prompt / 切块 / 检索参数 / 模型之前后，用同一批题跑 `eval run`，把 recall@10 / recall@50 / MRR@10 与逐题名次写入 `evals.eval_run`，
再用 `table-gate` 判表格专项是否回退。位置：`retrieval` 之后、合并之前。题集在表 `evals.eval_retrieval`（`gold_block_ids` 指向 `core.doc_block.block_id`），本包负责出题与导入，不负责检索实现。
（`eval_group` 即 `ragdemo eval`；三套评测集中的 `eval_extraction` / 观点评分不在本包，这里只有检索一套。）

## 关键模块
| 文件 | 职责 |
|---|---|
| `cli.py` | `ragdemo eval`：`run` `table-gate` `sample` `freeze` `freeze-diff` `generate` `label-sheet` `table-label-sheet` `label-import` `add-retrieval` |
| `runner.py` | `run_retrieval_eval` / `record_eval_run`；`per_question` 逐题记录 `bm25_rank / vec_rank / fused_rank / cand_rank / rerank_rank / final_rank` 与 `degraded_reason`；`rerank_timeouts` |
| `retrieval_metrics.py` | `recall_at_k`（逐题二值：top-k 含任一 gold 即命中）、`mrr_at_k` |
| `grouping.py` | `case_group`（`table|other` / `auto|human`）、`engine_group`（`parse_source` × 出题方式）；组内 ≥ 10 题才 `gated` |
| `table_gate.py` | `GATES` 写死的门槛：`table/auto` 掉 ≤ 1 题且 MRR 掉 ≤ 0.03；`other/*`、`table/human` 掉 ≤ 2 题；整体 recall@10 掉 ≤ 0.02；题数变了 → `GateIncomparable` |
| `generate.py` | 合成题（模型出题，`author = auto:<模型>:<版本>`，拦违禁词、同实体同文 gold 一并列入） |
| `human_labels.py` / `table_labels.py` | 导出标注表、校验导入（`ORIGIN_NOTES`）；表格表只导公告类表格叶子块，`passage` 不含 `content_desc`（防与索引同源抬高分数） |
| `sampler.py` | 按 `doc_type × parse_engine 族 × entity_id` 分层抽样，固定种子可复现 |
| `freeze.py` | 冻结 `as_of` 下的 block_id 集合 + `chunking_version` / `embedding_version` 到 JSON |
| `index_recall.py` | 向量路径对精确扫描的召回（阈值 0.95，仅 nightly，不进 PR 门禁） |

## 对外入口
命令都经 `ragdemo eval ...`；`evals.freeze` 等被测试和脚本直接 import。依赖 `retrieval`（跑检索）、`embed`、`parse.describe` / `parse.filters`（生成题时拦违禁词）、`ingest.stage2_crosscheck`（`table_labels`）；**不得被 `retrieval` / `parse` import**。

## 本包的硬规则
- **author 前缀决定题集身份**：`auto:`（合成，只作前后对比与回归护栏）、`human:`（人工）、`blindmodel:`（模型出题 + 独立复核的表格题，`tbl-v1`）、`blindmodel-text:`（正文盲标，`text-v1`）。前缀互不包含，`--author-prefix` 才能分别选中；模型出题**不得记成 `human:`**。`case_group` 把所有非 `auto:` 题都归 `human` 组。
- `table/auto` 偏高（题是看着描述写的），**改进证据只认 `table/human`**；盲标题集不是人工题，汇报时如实写。
- **gold_block_ids 稳定性**：重解析/重切块（`reparse --replace-differing`、`rechunk`、`ChunkConfig` 或 `XPARSE_PARAMS` 变更）会让旧 gold 悬空；必须先 `docs remap-gold` 或重冻结。`eval run` 在题集为空时抛错（空集会算满分）。
- **基线口径**（`docs/10.12-plan/验收记录/新配置基线B2.md`，B2 取代 B1，冻结文件 `新配置基线B1-freeze.json`）：
  - 付费 C2：`--models siliconflow --rerank --vector-backend pg`，丰富重排输入开，**`--rerank-timeout-s 60`** 排除接口抖动；生产默认 3 s 会让部分查询丢重排（B1 实测 11/15/10 次超时），**不能拿 3 s 的数字当基线**，且延迟数字不属于基线。
  - 免费 C1：`--models mock --no-rerank --w-vec 0`。
  - B2 数字：auto R@10 0.88 / MRR 0.7708 / R@50 0.90；盲标表格 0.871 / 0.6893；盲标正文 0.8857 / 0.8381；R@50 仍 < 0.92、p95 未达标。
  - 不合并线（B1 文档）：表格题 R@10 回退 > 0.02，或普通题 MRR 回退 > 0.02；之后任何检索改动用 `--rerank-timeout-s 60` 并按同一题集前缀对比。
  - 对比前后必须同 `--author-prefix`、同题数、同冻结语料；改动后跑 `table-gate --before <run_id> --after <run_id> [--by engine]`，输出整段贴 PR。
- 旋钮默认值必须与 `retrieval/types.py::RetrievalConfig` 一致：CLI 里 `--rerank-rich-input` 默认开、`--rerank-timeout-s` 默认 3.0、`--bm25-desc`/`--period-boost`/`--rerank-vector-guarantee` 默认关。
- 标注走 CLI + Excel/WPS，不做 Web 标注界面（ADR-0006 / 0010）。

## 怎么测
- `uv run --no-sync pytest tests/evals -q`：大多带 `db` 标记（`temp_db` 随机库，不碰开发库）；`-m "not db"` 只剩 `test_metrics.py`、`test_grouping.py`、`test_table_gate.py`、`test_cli.py` 的非库部分。
- **免费**：`eval run --models mock ...`、`table-gate`、`sample`、`freeze`、`freeze-diff`、`label-sheet`、`table-label-sheet`、`label-import`（不加 `--yes` 只校验）。
- **付费**：`eval run --models siliconflow`（嵌入 + 重排）、`eval generate --yes`（DeepSeek 出题并写库）。agent 不得自行跑，除非用户明确要求。
- 注意：`eval run` 即使免费也会往 `RAGDEMO_DSN`（`.env` 指向开发库）的 `evals.eval_run` **插一行**；`label-import --yes`、`generate --yes` 写题集。

## 常见坑
- `--gate` 取的是该 suite **最近一次**运行的 recall@10（不分配置、不分 author 前缀）作基线，容差 `TOLERANCE = 0.02`；先读基线再跑（读晚了会和自己比）。多配置混跑时 `--gate` 结果不可信，正式判定用 `table-gate`。
- 近似同分块的排名会因 BM25 索引状态漂移（原地 UPDATE 后未 `REINDEX INDEX core.doc_block_bm25`），评测数字会"无故"下降 ~0.03，见 `docs/10.12-plan/验收记录/旧文档缓存重解析演练.md`。
- 重解析沿用旧 `known_at`，同一历史 `as_of` 下新旧版本块可同时可见；`freeze` 落块号集合正是为了留证据，不是多余。
- 逐题名次存在 `metrics.per_question`（JSON，无需迁移），失败题分析不必重跑付费评测：看 `cand_rank` 判断 gold 是否进了重排候选。
- Windows 控制台 GBK：`export PYTHONUTF8=1`，否则中文表格输出乱码。
