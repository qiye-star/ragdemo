# packages/ragdemo/src/ragdemo/metrics — 指标口径的版本化管理、Excel 导入、手工指标、派生值与汇率换算

## 这个包做什么
两件事。其一，管「口径」：指标定义（`node_metric`）、供应商字段映射与换算系数（`metric_source_map`）、AI 收入拆分规则——
全部**追加新版本、旧版本 superseded，从不覆盖**，读取经 `asof` 视图。其二，做「算」：同比/环比/TTM、币种换算，
全部纯 Python + `Decimal`（CLAUDE.md §1.2：模型只决定算什么，不决定结果）。另有手工录入行业指标的校验与写入。

## 关键模块
| 文件 | 职责 |
|---|---|
| `definitions.py` | `publish_metric`/`retire_metric`/`publish_source_map`/`publish_ai_revenue_rule` 与 `*_as_of` 读取；`PublishOutcome`；时间线乱序抛 `OutOfOrderVersion` |
| `derived.py` | `derive(kind, facts, period, metric_id=)`；`yoy`/`qoq`/`ttm`；算不出来时 `value=None` 且 `note` 写原因，不编数 |
| `fx.py` | `convert(...)`：取 `rate_date <= on` 的最近一条，过陈旧抛 `FxUnavailable`；非 CNY 互转经 CNY；不四舍五入 |
| `workbook.py` / `importer.py` | Excel 模板导出与宽松读取 → `plan_import`（只读，差异预览，一次报全）→ `apply_import`（整批一个事务） |
| `manual.py` | 手工指标 CSV：来源、出处必填，`known_at` 取报告发布日；整文件作为 `provider='manual'` 快照留底；任一行不过整批不写 |
| `cli.py` | `ragdemo metrics` 命令组 |

## 对外入口
- CLI：`ragdemo metrics export-template` / `import --file X --author A [--note N] [--yes]`（默认只预览，`--yes` 才写）/ `manual-template` / `import-manual`。
- `dataapi/mock.py` 与 `dataapi/pg.py` 调 `metrics.derived.derive`。
- import 关系：本包 import `ragdemo_core.db.session`、`ingest.writer`/`ingest.snapshot`（`manual.py`）、`seed.taxonomy`（`workbook.py`）、`dataapi.contract`（`derived.py` 的返回类型）。

## 本包的硬规则
- 数值计算与时点逻辑必须有单测，无例外（CLAUDE.md §3）；金额/比率一律 `Decimal`，不用 float。
- `publish_*` **不提交事务**，调用方把一批修改放在一个事务里；不得在内部 commit。
- 口径写入只追加版本；读取必须经 `as_of_session` + `asof` 视图，不查基表（CLAUDE.md §1.1）。`core.node_metric`/`core.metric_source_map` 仍是「当前值」表（外键引用），版本在 `*_version` 表。
- 手工录入不是溯源的例外：每行必须有来源与出处，`known_at` 取发布日而非录入日；推导指标（`derived`）不接受手工值；单位必须等于指标定义单位，不在此换算（CLAUDE.md §0）。
- 同比不对负/零基数硬算；累计口径需先拆单季才能做环比/TTM（`derived.py` docstring）。

## 怎么测
仓库根目录运行：`pytest tests/metrics`。`test_derived.py`、`test_manual.py` 偏纯逻辑；`test_definitions.py`、`test_excel_import.py`、`test_fx.py` 为 `@pytest.mark.db`，用 `temp_db` 随机空库。均免费。

## 常见坑
- Excel 读取故意宽松：解析问题记进 `WorkbookData.problems`，与查库校验合并后一次报全，别改成遇错即抛。
- 迁移 018 引入版本表；改口径表结构要新增迁移，不得改已执行的迁移文件。
- 周末/节假日汇率取上一交易日；`as_of` 当天 09:00 看不到 09:15 才公布的中间价（`known_at` 由 `asof.fx_rate` 保证）。
