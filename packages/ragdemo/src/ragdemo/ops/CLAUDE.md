# packages/ragdemo/src/ragdemo/ops — 运维告警出口（失败、超预算、对账不平、质量不达标）

## 这个包做什么
无人值守时需要主动推送：Dagster 的检查失败只在运行页面里可见。这里提供统一告警出口 `raise_alert`，
以及把当天未通过的质量指标与月度预算状态转成告警的 `evaluate_quality_alerts`（由 `daily_ops` 作业每天调用）。
位置：横切运维层，被 ingest 调度与解析账本调用。

## 关键模块
| 文件 | 职责 |
|---|---|
| `alerts.py` | `Alert`、`raise_alert`（三渠道：结构化日志 / `audit.alert_log` 落库 / webhook）、`webhook_payload`（企业微信、飞书、钉钉、通用 JSON） |
| `quality_alerts.py` | `evaluate_quality_alerts`：读 `quality.quality_metric` 当日未通过项 + `quality.budget.month_to_date`，生成告警 |

## 对外入口
- 调用方：`ingest/schedules.py`（`Alert`、`raise_alert`、`evaluate_quality_alerts`）、`ingest/assets_docs.py`、`parse/tiered/ledger.py`。
- 本包 import：`psycopg`、`httpx`、`ragdemo.quality.budget`。不得 import `ingest`/`parse`（避免与调用方成环）。
- 配置：`ALERT_WEBHOOK_URL`（环境变量，未配则跳过 webhook）。

## 本包的硬规则
- **`raise_alert` 绝不向调用方抛异常**：告警路径不能把正在处理的故障变成另一个故障；渠道失败写进 `delivered` 并记日志。
- 日志与落库两渠道永远启用；webhook 地址只来自环境变量，不得写进代码或夹具（CLAUDE.md §3）。
- 冷却：同一 `alert_key` 默认 6 小时内只对外推送一次，期间重复告警照常落库并标 `suppressed`；`critical` 也遵守冷却。告警键必须带分区日/月份，否则会被错误地合并或永久静音。
- 对账/新鲜度类指标（名字含 `reconcil`、`freshness`）→ critical；其它质量指标 → warning；预算 ≥ 80% warning、≥ 100% critical。

## 怎么测
仓库根目录运行：`pytest tests/ops/test_alerts.py`（`@pytest.mark.db`，`temp_db`；webhook 用注入的假 `httpx` transport，无真实网络、免费）。

## 常见坑
- `audit.alert_log` 表在迁移 `024_alert_log_and_dagster_schema.sql`，属于 `audit` schema，但写它的代码在 `ops/` 而不是 `audit/`（后者目前是空占位）。
- 冷却判定读 `audit.alert_log` 里 `NOT suppressed` 且在冷却窗口内的同 key 记录（`_recently_sent`），所以告警键的构造决定了去重粒度。
