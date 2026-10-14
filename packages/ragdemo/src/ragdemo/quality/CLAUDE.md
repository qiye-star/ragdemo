# packages/ragdemo/src/ragdemo/quality — 管线质量门禁、月度成本预算、时点泄漏抽样重放

## 这个包做什么
「没有监控的管线等于没有数据」：这里放文档管线的质量检查（Dagster `@asset_check`，失败即阻断下游嵌入物化）、
月度计费预算的判定与停手、以及对历史 `doc_block` 做抽样重放以发现「写入后被无痕篡改」的时点泄漏。
所有指标统一写入 `quality.quality_metric`，看趋势而不是单点。

## 关键模块
| 文件 | 职责 |
|---|---|
| `metrics.py` | `MetricResult`、`record_metric`：`quality.quality_metric` **唯一写入路径**，通过与失败都写 |
| `checks.py` | 八项 `*_check`（解析成功率、表格闭合率、置信度 p50、孤儿块、叶子长度合规、向量覆盖、每源对账、接入延迟）与汇总 `ALL_CHECKS` |
| `budget.py` | `month_to_date`、`ensure_budget`（≥100% 抛 `BudgetExceeded`，计费调用发起前停）、`record_budget_metric`；账本是 `core.provider_snapshot.cost_cents`，北京时间自然月 |
| `budget_asset.py` | Dagster 资产 `monthly_cost`，每日记录预算使用比例；单独成文件，不含 `from __future__ import annotations` |
| `replay.py` | `sample_block_ids`、`replay_and_check`：每日抽 200 块 × 三个历史 as_of，经 `asof.doc_block` 重放，哈希与基线表 `quality.asof_replay_baseline` 比对 |

## 对外入口
- CLI：`ragdemo db replay-check [--partition-date YYYY-MM-DD]`（`cli.py`，不一致以非零码退出）。
- `ingest/definitions.py` 注册 `ALL_CHECKS` 与 `monthly_cost`；`ingest/schedules.py`、`ingest/assets_docs.py`、`ingest/reconcile.py`、`ingest/cli.py` 调 `record_metric`、`ensure_budget`、`replay_and_check`；`ops/quality_alerts.py` 读预算与指标出告警。
- 本包 import `ingest.*`、`parse.*`（`checks.py`）、`ragdemo.config`（`budget_asset.py`），所以 `ingest` 反向 import 本包时注意循环（见现有调用均是函数级/定义层，勿在模块顶层新增互引）。

## 本包的硬规则
- 每个 `阻断=True` 的检查失败必须阻断 `block_embeddings`，不要降级为仅告警（`checks.py` docstring）。
- 预算：≥80% 告警（指标 `monthly_cost_ratio` 记未通过）、≥100% 阻断；NULL 成本的调用单独计数并告警，**不当成免费**、也不因未知而阻断整条管线。
- 重放抽样种子由 `partition_date` 派生，同一天结果必须可复现；三个探测点只在**第一次**抽中该块时算出并永久写入基线，之后只读基线，不得改成每次现算（否则篡改会连同基线一起「自愈」）。
- 「首次遇到的 (block_id, as_of)」只记基线、不算不一致；只有「见过但对不上」才是泄漏信号。
- 重放走 `asof.doc_block` 视图（CLAUDE.md §1.1）。注意 `checks.py`/`replay.py` 的抽样与统计查询直接读 `core.doc_block`/`core.document`，这是管线侧运维检查，**不是 Agent/应用查询路径**，不得照搬到 `api/` 或 `dataapi/`。
- `quality.dashboard` 视图无 `computed_at <= as_of` 过滤，对外展示要自己加（见 `api/queries/quality.py`）。

## 怎么测
仓库根目录运行：`pytest tests/quality`（`test_budget.py`、`test_checks.py`、`test_replay.py` 均含 `@pytest.mark.db` 用例，用 `temp_db` 随机空库）。免费。

## 常见坑
- Dagster 在装饰期直接比较 `context` 注解对象，凡含 `@asset`/`@asset_check` 的文件不要加 `from __future__ import annotations`（`checks.py`、`budget_asset.py` 均是）。
- 月度预算上限来自 `RAGDEMO_MONTHLY_BUDGET_CNY`，默认 5000（`config.py`）。
- 本包的预算是**计费金额**预算；解析页数预算是 `parse/` 里的另一套，别混用。
