# packages/ragdemo/src/ragdemo/seed — 种子数据：产业链底稿 → db/seed CSV → 数据库，以及隔离演示种子

## 这个包做什么
把「创始人维护的产业链底稿」（`docs/csv/`）变成数据库里的参考数据：实体、别名、环节归属、指标口径、关系、传导规则。
分三段，都是确定性、可重复：（1）`listing_dates` 联网向供应商取上市日期；（2）`docs_csv` 离线把底稿 + `db/seed/sources/` 整理结果生成 `db/seed/*.csv`；
（3）`loader` 先全量校验再整体写库。另有 `isolation_demo`：给诊断界面的隔离探针造合成数据。

## 关键模块
| 文件 | 职责 |
|---|---|
| `docs_csv.py` | `build_seed`/`write_seed`：底稿 → `db/seed/*.csv`；关系里的名字解析不了就整个构建失败，不静默丢关系；新关系 `valid_from` 取底稿编制日期 |
| `listing_dates.py` | `fetch_listing_dates`：A 股走 Tushare REST `stock_basic`，退回 Wind；港美台日韩走 Wind；每份响应记 `provider_snapshot`；结果写 `db/seed/sources/listing_dates.csv` |
| `loader.py` | `load_all(conn, seed_dir)`：按依赖顺序 entity → alias → node_metric → metric_source_map → relation → propagation_rule；先校验后写入，重复执行安全；`SeedError` |
| `taxonomy.py` | `load_taxonomy`：环节表（`db/seed/taxonomy.csv`）作为 `l1_layer/l2_segment/l3_node` 的唯一事实来源，导入时交叉校验 |
| `isolation_demo.py` | `seed_isolation_demo`/`clear_isolation_demo`：合成公共/租户私有/用户私有文档；仅回环地址可写（`NotLoopbackHost`） |
| `cli.py` | `seed-listing-dates`、`seed-build` 命令 |

## 对外入口
- CLI（均在 `ragdemo db` 下，`cli.py`）：`seed`（导入）、`seed-listing-dates`、`seed-build`、`seed-isolation-demo`、`clear-isolation-demo`（两者不加 `--yes` 只 dry run）。
- `metrics/workbook.py` 与 `loader.py` 共用 `taxonomy.load_taxonomy`；`api/queries/isolation.py` 引用 `isolation_demo` 的演示身份常量。
- 本包 import：`adapters.*`、`ingest.snapshot`（仅 `listing_dates.py`）、`metrics.definitions`（`loader.py`）、`ragdemo_core.blob`。

## 本包的硬规则
- 「先校验后写入」：CSV 一行错整份拒绝；部分导入比没有种子更糟（`loader.py` docstring）。
- 环节名是无约束自由文本却是五张表的联结键，一个错字会让评分基准悄悄变空——一律经 `Taxonomy` 校验，不得绕过。
- 手工数据的 `known_at = valid_from` 当日 00:00 UTC（`SEED_TZ = UTC`），**不是 `now()` 也不是入库时间**；新增非上市实体必须给 `valid_from`；新关系不得把「行业共识」回溯成早就已知（docs/03-point-in-time.md §1.1）。
- 上市日期由供应商取、不手填（`listed_date` 用于剔除次新股）；Wind 答成另一只证券时记失败不采用。
- `isolation_demo` 的合成数据 `publish_at = known_at = 2020-01-01`，刻意早于 Dagster 分区起点 2022-01-01，避免被 `vector_coverage_check` 等阻断检查算进去；直接手写 INSERT 是诊断种子特例，**业务写入路径仍不得写 `owner_tenant`**。
- 不得对非回环数据库执行 `seed-isolation-demo`。

## 怎么测
仓库根目录运行：`pytest tests/seed`。`test_docs_csv.py`、`test_taxonomy.py`、`test_listing_dates.py` 偏离线；`test_loader.py`、`test_loader_sync.py`、`test_isolation_demo.py` 需数据库（`temp_db`）。免费，`listing_dates` 用假网关。

## 常见坑
- `ragdemo db seed` 只导入 `db/seed/` 下**存在**的 CSV（`load_all` 逐文件判断）；改了 `docs/csv` 只需重跑 `seed-build`，不用再打一遍供应商。
- 种子 CSV 与说明见 `db/seed/README.md`（由其他文档维护）。
- `docs_csv.py` 依赖 `db/seed/sources/` 下的整理表（`entity_curation.csv` 等）：新增公司要同时补这些文件，否则构建失败。
