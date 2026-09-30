# 阶段 4 验收记录：常驻运行与时点验收（起算 2026-09-30）

对应总计划 `2026-09-28-数据底座完善总计划.md` 阶段 4（4.1–4.7）。

## 已完成（可当场验证的部分）

| 任务 | 状态 | 证据 |
|---|---|---|
| 4.1 常驻容器 | 完成 | `ragdemo-dagster-ui`、`ragdemo-dagster-daemon` 均 healthy；Dagster 存储在 ParadeDB `dagster` schema（迁移 024）；QueuedRunCoordinator 并发 1 |
| 4.2 调度 | 完成 | 行情 17:00（周一至周五）、公告 20:30、阶段二核对 21:30、泄漏回放 03:30、质量告警 22:00、公告对账 周日 23:00；Wind 定时作业**保持停用**（积分） |
| 4.3 告警 | **完成**（2026-09-30 已推送，待负责人确认群里收到） | run 失败 sensor → `ops/alerts.py`（日志 + `audit.alert_log` + 可选 webhook，同 key 冷却 6h）；自检作业 `alert_selftest_job` 验证过日志与落库；**真实推送渠道未配置**（需 `ALERT_WEBHOOK_URL` / `ALERT_WEBHOOK_KIND`） |
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

## ParadeDB 间歇崩溃：定位与处置（2026-09-30）

**现象：** `ragdemo-db` 日志里 `untracked child process ... exited with exit code 2`，随后整库崩溃恢复（数据完整，`core.doc_block` 1613 行不变），恢复期间连接失败。9/29–9/30 共 7 次。

**定位：** 打开语句日志后，在跑测试时复现——崩溃发生在测试库执行迁移 004 的 `CREATE INDEX ... USING bm25` 期间；崩的是被登记为 untracked 的**并行 worker**。此前的几次也都是重聚合 / 重查询场景（并行查询）。结论：`pg_search 0.25.9` 的并行 worker 有缺陷（建 bm25 索引和并行查询都会中）。

**处置：**
1. `infra/docker-compose.yml` 给数据库加 `max_parallel_maintenance_workers=0`、`max_parallel_workers_per_gather=0`（当前数据量下并行收益可忽略）；
2. `infra/dagster/dagster.yaml` 开启 `run_retries`（最多 2 次），即便再有瞬时崩溃，失败的 run 也会自动重试，不让当天分区缺数据；
3. 已重建 dagster 镜像使重试配置生效。

**验证：** 关并行后，此前崩溃的 `tests/db` + 手工录入测试（75 个）无崩溃；更大批量的压力验证结果见下方"压力验证"。**4.4 的无人值守计时仍从 2026-09-30 起算，但若再出现崩溃，需重新评估。**

## 产业指标手工录入（2026-09-30，用户选定方案 2）

47 个无供应商映射的指标中：3 个 `derived` 由代码计算；约 25 个 `filing`/`announcement`/`ir_page` 走文档抽取；约 19 个 `vendor_api`（第三方行业报告）改为**手工录入**。

- `ragdemo metrics manual-template --out x.csv` 生成表头；`ragdemo metrics import-manual --file x.csv [--yes]` 校验（默认只校验）与写入。
- 每行必须有 `source`、`source_ref`；`published_on` 即 `known_at`（日精度取当日 23:59:59.999999 北京时间）；单位必须等于指标定义；推导指标拒绝；整批一次报全、有错不写。
- 整个 CSV 作为一条 `provider='manual'` 快照留底，事实行盖 `snapshot_id`；`metric_source_map` 自动登记 `manual`（字段名=metric_id，不换算）；重复导入跳过，改值走更正追加。
- **行业汇总类指标**（全球出货量、渗透率等，不属于任何一家公司）：迁移 026 新建时点表 `core.industry_metric_point`（键 = metric_id + scope + period，`asof.industry_metric_point` 视图，登记双时态）。CSV 里 `entity_id` 留空、填 `scope`（GLOBAL / CN 等），与公司口径恰填其一；规则同公司口径（来源出处必填、同值跳过、更正追加、快照留底）。
- **未做：** DataApi 读取这张表的接口（契约升 v1.3），待有使用方时再加；目前只能 SQL 查 `asof.industry_metric_point`。

## 压力验证（关并行后，2026-09-30）

`tests/ingest tests/retrieval tests/metrics tests/db tests/ops` 共 **521 个测试全部通过**（5 分 01 秒），其间每个测试都新建并迁移一个带 bm25 索引的临时库；数据库日志 30 分钟内**崩溃 0 次**（关并行前同类负载下 1 次测试就能触发）。结论：并行 worker 是触发条件，处置有效；仍需靠 4.4 的 5 天实际运行继续观察。

## 负责人决定（2026-09-30）

| # | 事项 | 决定 | 落地 |
|---|---|---|---|
| 1 | 告警渠道 | 企业微信群机器人 | `.env` 已设 `ALERT_WEBHOOK_KIND=wecom`；**`ALERT_WEBHOOK_URL` 待填**（含 key，只放 `.env`）。填好后 `docker compose -f infra/docker-compose.yml --env-file .env up -d --force-recreate --no-deps dagster-daemon dagster-webserver`，再触发 `alert_selftest_job` 验证群里收到消息，即关闭 4.3 |
| 2 | Wind 月度额度 | 无上限 | 代码里本就没有 Wind 调用上限闸门，无需改动；`infra/backfill.md` §3 的"先估再跑"保留为习惯性 dry run，不再作为限额。是否补全 46 家 A 股的 Wind 财务 / 股东 / 分部，见下方待确认 |
| 3 | 研报 | 选定方案 A：入库用于检索与内部研究，**不展示原文** | [ADR-0014](../../adr/0014-research-reports-internal-retrieval-only.md)；迁移 027 登记 `eastmoney-research`（`can_show_raw=false`）；`docs ingest --kind research` 单独入库。**91 份尚未解析入库——等 TextIn 充值** |
| 4 | 人工标注 | 需要做 | 已提供 `eval label-sheet` / `eval label-import` / `eval run --author-prefix human:`（`evals/human_labels.py`，测试 5 个）；等标注员按手册填表 |
| 5 | 重排 | 不关闭 | 默认不变（`rerank_enabled=True`）；关重排时 recall@10 0.81 对 0.76 仅作为消融记录，待人工标注集复核 |
| 6 | 行业报告录入 | 有人录 CSV | 已写 [行业指标录入手册](../行业指标录入手册.md)（含 CSV 格式、校验规则、18 个 `vendor_api` 指标清单与单位），导入命令 `ragdemo metrics import-manual` |

### 研报：与产品边界冲突（已按方案 A 解决，2026-09-30）

"研报全部公开可见"与仓库里两处硬约束冲突，需要你确认如何取舍：

1. **CLAUDE.md §0（不可协商）**：禁止输出买卖建议、目标价、评级、仓位建议。研报正文就是评级 + 目标价 + 盈利预测；`can_show_raw=true` 意味着原文对所有人展示，等于把它们原样输出。
2. **来源使用限制**：`docs/rawdata/688041-hygon/README.md` 写明研报著作权归各券商、"仅供内部研究参考，不得对外分发或商用"；`local_manifest.load_documents` 里对 `kind=research` 的过滤"没有开关"（ADR-0008 后果 1）。"公开可见"即对外分发。

可行的折中（任选，需你定）：
- **A（推荐）**：研报入库用于**检索与内部研究**，`can_show_raw=false`（只展示元数据与溯源，不展示原文），输出里凡出现评级 / 目标价 / 盈利预测按"第三方观点"标注机构与日期（README 已要求）——符合 §0，也不违反来源限制；
- **B**：要公开展示原文，则需要（a）书面取得券商或东方财富的转载授权，（b）入库前剔除评级、目标价、盈利预测段落，（c）新增一篇 ADR 推翻 ADR-0008 后果 1 并说明推翻条件。
另外，解析 91 份研报需要 TextIn 额度（当前余额不足，你此前决定暂停解析），所以即使选定口径，入库也要等充值。

## 待确认
- Wind 无上限：是否现在补全 46 家 A 股的财务 / 股东 / 分部（全量约 674 次调用，数据会按首次披露日写入）？
- 企业微信机器人 Webhook 地址请填进 `.env` 的 `ALERT_WEBHOOK_URL`。


## 4.3 告警通路验收（2026-09-30）

- 企业微信机器人 Webhook 已配置并注入 Dagster 容器（`ALERT_WEBHOOK_KIND=wecom`，URL 只在 `.env`）。
- 触发 `alert_selftest_job`：run 按预期失败，`alert_on_run_failure` 传感器产生告警，**被 6 小时冷却拦下**（当天早些时候的自检用同一个 key），只落库未推送——冷却机制按设计工作。
- 另用独立 key 直接调用 `raise_alert` 发送一条测试消息：`{'log': 'ok', 'webhook': 'ok', 'db': 'ok'}`（`webhook: ok` 已校验企业微信返回的 `errcode == 0`）。**请确认群里收到了"告警通路自检（企业微信）"**。
- 自检作业加了 `dagster/max_retries: 0`，避免 run 自动重试（见上文）把一次故意失败放大成 3 条告警。

## 决定补充

- **不补全 46 家 A 股的 Wind 数据**：需要时直接通过 Wind MCP 查询，不批量入库（也就不产生对应的 `provider_snapshot` / 点时表行）。
