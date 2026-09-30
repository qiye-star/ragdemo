# 阶段 4 验收记录：常驻运行与时点验收（起算 2026-09-30）

对应总计划 `2026-09-28-数据底座完善总计划.md` 阶段 4（4.1–4.7）。

## 已完成（可当场验证的部分）

| 任务 | 状态 | 证据 |
|---|---|---|
| 4.1 常驻容器 | 完成 | `ragdemo-dagster-ui`、`ragdemo-dagster-daemon` 均 healthy；Dagster 存储在 ParadeDB `dagster` schema（迁移 024）；QueuedRunCoordinator 并发 1 |
| 4.2 调度 | 完成 | 行情 17:00（周一至周五）、公告 20:30、阶段二核对 21:30、泄漏回放 03:30、质量告警 22:00、公告对账 周日 23:00；Wind 定时作业**保持停用**（积分） |
| 4.3 告警 | 部分完成 | run 失败 sensor → `ops/alerts.py`（日志 + `audit.alert_log` + 可选 webhook，同 key 冷却 6h）；自检作业 `alert_selftest_job` 验证过日志与落库；**真实推送渠道未配置**（需 `ALERT_WEBHOOK_URL` / `ALERT_WEBHOOK_KIND`） |
| 4.6 恢复演练 | 完成 | `scripts/restore_drill.sh`：独立临时容器还原，40 张表 / 97,211 行逐表校验和一致，`db check` 通过；`infra/runbook-backup.md` 记录已知问题（重聚合含向量列可能使 pg_search 后台进程崩溃，校验和已排除嵌入列并单独校验） |
| 4.7 文档同步 | 完成 | `docs/01-architecture.md` 部署段、runbook、backfill 说明 |

## 需要真实时间才能验收（时钟 2026-09-30 起算）

- **4.4 连续 5 天无人值守**：最早 **2026-10-05** 可判定。判据：5 个自然日内每个日分区的行情 / 公告作业成功，无手工干预，`quality.quality_metric` 无未处理失败。
- **4.5 7 天双跑一致**：最早 **2026-10-07** 可判定。判据：常驻库与独立核对结果（泄漏回放哈希、公告对账、阶段二核对）逐日一致。

这两项**不能靠改代码提前通过**；到期后按判据查库出结论，再补本文件。

## 待用户决定 / 提供

1. 告警 webhook 渠道与地址（企业微信 / 飞书 / 钉钉 / 通用），配置后重跑自检即可关闭 4.3。
2. Wind 月度调用上限（按需门控用）。
3. TextIn 充值与否（海光剩余 15 份文档未解析）。

## 已知问题：ParadeDB 后台进程间歇崩溃（需持续观察）

`ragdemo-db` 日志里 `untracked child process ... exited with exit code 2` 出现过多次（9/29 03:00、07:59；9/30 04:32、04:40、07:57、08:00 UTC），随后 Postgres 整体重新初始化、自动恢复（数据完整，`core.doc_block` 1613 行不变）。07:57 与 08:00 两次发生时没有手工查询在跑，只有常驻的 dagster daemon。**这直接影响 4.4"连续 5 天无人值守"**：崩溃恢复期间（约 5–40 秒）作业连接会失败。尚未定位触发语句；下一步：在日志里打开 `log_min_error_statement` / 给 pg_search 后台进程留 core，或对照 dagster 作业时刻排查。

## 产业指标手工录入（2026-09-30，用户选定方案 2）

47 个无供应商映射的指标中：3 个 `derived` 由代码计算；约 25 个 `filing`/`announcement`/`ir_page` 走文档抽取；约 19 个 `vendor_api`（第三方行业报告）改为**手工录入**。

- `ragdemo metrics manual-template --out x.csv` 生成表头；`ragdemo metrics import-manual --file x.csv [--yes]` 校验（默认只校验）与写入。
- 每行必须有 `source`、`source_ref`；`published_on` 即 `known_at`（日精度取当日 23:59:59.999999 北京时间）；单位必须等于指标定义；推导指标拒绝；整批一次报全、有错不写。
- 整个 CSV 作为一条 `provider='manual'` 快照留底，事实行盖 `snapshot_id`；`metric_source_map` 自动登记 `manual`（字段名=metric_id，不换算）；重复导入跳过，改值走更正追加。
- **限制：** 行业汇总类指标（全球出货量、渗透率等）没有对应实体，`fin_fact.entity_id` 有外键。目前只能录到已存在的实体（如英伟达、台积电）。汇总类要不要建"节点级"伪实体（会改变实体计数与环节归属语义）或单独建表，待决定。
