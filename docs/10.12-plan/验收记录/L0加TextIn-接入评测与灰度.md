# L0 + TextIn 分页接入：灰度、回滚与观察说明（T14）

日期：2026-10-13。本文是**操作说明与事先登记的停止条件**，不是灰度结果记录：截至今天没有按本文做过任何一批灰度，结果将来逐批追加在文末"运行记录"。

## 1. 现状（先读这一节）

- **L0 v2 未启用。** 留出集 G3 = 0.9891 < 0.99；用户事后放宽 G3 后，G9 阅读顺序抽查未过（6 页被判错，见 `L0v2-结论.md`）。`PARSE_TIER_L0_ENABLED=true` 在配置加载时会被拒绝（报错文字 `L0 v2 not validated (G9)`），`TieredParser` 里根本没有 L0 参数。因此生产现实是 **L3（TextIn）一条路**。
- **三条减页规则全部只做影子记录**（`blank_page`、`signature_tail`、`research_disclaimer_tail` 均未过 `减页规则测量.md` 的事先门槛，总节省不超过约 55 页、约 7.5 元）。`PARSE_SKIP_RULES` 默认为空，保持为空。命中只写进台账的 `shadow_skip_rules` 与旁路 JSON，不跳页。
- **接入分页路由的实际价值有限**：整份 L3 + 逐页台账（每页去向、P1–P8 验收、计费页与 TextIn 子调用快照一一对应），以及将来开 L0 / 跳页规则的开关位置。它**不降低**单页成本；成本是 0.1356 元/页（257 页实测估算，待发票复核）。
- **测试阶段的解析口径（用户裁决）：** 付费解析目前只做了一部分（已解析 288 页）；**不付费的工作**（本地文字层、页型分类、台账、影子规则、缓存命中）可以全部做完；付费解析按页预算分批，不一次烧完积压（积压全送 TextIn 约 869 元，见 `解析成本估算.md`）。
- **图表：** 只存裁剪图与元数据（`figure_meta`），不提取图内数据，不送模型。**表格：** 照旧由 TextIn 逐格解析入库。
- **迁移 034 已于 2026-10-13 在开发库应用**（备份 `data/export/ragdemo-pre-034-20261013T092306Z.dump`；页账本表当前 0 行，历史文档走旧路径未写账本）。
- **已知未接线的告警：** `ledger.emit_ledger_alerts`（近 24 小时占比、整份退回、预算 30%）目前**没有调用方**，`docs parse-cost --alert` 也已去掉。已接线的只有入库时每份文档的 `parse_ledger:l3_no_output` 告警（`ingest/assets_docs.py`）。因此"每日检查"目前靠手动查询，不要假设有告警会替你盯。是否接到定时任务，另立项。
- **一个未量化的风险：** P5 对跨页表格只在首页计表，续页可能 P5 不过而使整份文档记为 `rejected`；在真实文档上未测。第一次付费灰度前，先在已缓存的文档上只读测一遍（见 §3 步骤 2）。

## 2. 开关一览（`.env`，只改本机，不提交；命令里用 `set -a && . ./.env && set +a`，不打印值）

| 变量 | 默认 | 含义 |
|---|---|---|
| `PARSE_TIERED_ENABLED` | 空（关） | 总开关。关闭时入库路径与改造前逐字节一致 |
| `PARSE_TIERED_DOC_TYPES` | 空 | 逗号分隔，只能写 `research_report`、`announcement`；定期报告类型与未知类型会被拒绝 |
| `PARSE_TIER_L0_ENABLED` | 空 | **保持为空**；写 true 会被 `load_config` 拒绝 |
| `PARSE_SKIP_RULES` | 空 | 保持为空（全部影子）；将来只填过门槛且用户批准的规则 |
| `TEXTIN_MAX_PAGES_PER_RUN` | 代码默认 500 | 单次 run 页预算闸门 |
| `RAGDEMO_MONTHLY_BUDGET_CNY` | 代码默认 5000 | 月预算；本月 TextIn 花费 ≥ 30% 触发 S-G |
| `TEXTIN_COST_PER_PAGE_CNY` | 代码默认 0.1356 | 发票复核后在此覆盖 |
| `ALERT_WEBHOOK_URL` / `ALERT_WEBHOOK_KIND` | 空 / wecom | 告警 webhook；URL 含 key，只放 `.env` |

不论开关如何：年报 / 半年报 / 季报整份 L3；用户私有材料不进分级路由；已入库文档不重解析。

## 3. 灰度步骤（每一步先 dry run，确认页数与费用，再加 `--yes`）

前置闸门（任一不满足就停）：用户确认合合信息已充值、本月预算上限已定；本文"运行记录"里已有基线 run_id 与 git sha（现有基线 B0 = `eval-b0b1b5f24248`，C1，当时库内 560 份文档，见 `解析来源评测基线.md`）。

1. **备份并迁移。** 先做数据库备份（沿用 `data/export/ragdemo-pre-033-*.dump` 的做法，生成 pre-034 转储），再 `set -a && . ./.env && set +a && uv run ragdemo db migrate`，确认输出含 `034_parse_page_ledger`。迁移由控制者（人）执行，不交给子代理。
2. **只读预检（零费用）。** 在已缓存的 TextIn 文档上跑一遍验收与台账构造，统计 P5 误拒的文档数；若续页导致整份 `rejected`，先修 P5 再谈付费灰度。
3. **开总开关，先只对研报。** `PARSE_TIERED_ENABLED=true`，`PARSE_TIERED_DOC_TYPES=research_report`；`PARSE_TIER_L0_ENABLED` 与 `PARSE_SKIP_RULES` 保持空。第一批 5 份：`docs ingest --kind research ... --max-pages 60`（先 dry run）。核对：
   - `core.document.parse_engine`：未跳页时整份送 TextIn，仍是 `textin:...`（缓存键与旧路径相同）；只有按连续段拆分或整份退回时才是 `tiered:...`。是否走了分页路由以台账为准，不看引擎前缀；
   - 每份文档在 `core.parse_page_route` 里页数齐全，`tier` 只有 `L3`（规则未启用时 `skipped` 应为 0）；
   - `core.provider_snapshot`（`provider='textin'`）的条数 = 计费的 TextIn 子调用数；
   - `core.doc_block.parse_source` 非空，研报块 `can_show_raw` 为 false；
   - `core.parse_doc_acceptance.state` 的分布，对照 `TextIn页数字审计.md` 的口径（审计复现为 20 accepted / 1 accepted_with_scan / 10 textin_unverified）。
4. **逐批扩大。** 每批 `--max-pages 300`；每批之后跑 `uv run ragdemo docs parse-cost --month <当月> --doc-type research_report`（只读），输出贴进"运行记录"。
5. **公告阶段**（研报阶段无异常，且用户裁决了"默认不解析的 NONE 档公告"解析与否：A 维持不解析 / B 全部 / C 全部但不解析 4 份整份扫描件）：`PARSE_TIERED_DOC_TYPES=research_report,announcement`，用 `docs parse-pending --tier ... --max-pages N [--doc-id ...]` 分批，先 dry run。

**每批入库之后必须做的度量：重测与当前语料匹配的基线。** 每次入库都会改变检索语料，旧基线不再可比（T11 时库里多出 3 份新文档造成偏差，就是这个原因）。做法：

1. 入库并嵌入后，用同一套参数跑 `uv run ragdemo eval run --models mock --no-rerank --w-vec 0 --author-prefix auto: --label <本批标签>`（C1，免费）；
2. 与上一批（或 B0）用 `eval table-gate --before <run_id> --after <run_id>` 比；`per_engine` 按解析来源分组读 `legacy` / `local` / `textin`；
3. 看**同一批 gold 在新语料上的变化**；指标下降按 CLAUDE.md §1.4 处理：不合并，或在记录里写清为什么可以接受；
4. `eval_extraction` 运行器未补（任务 T13 未获批准），记录里写"未测"，不得写成通过；付费评测 C2（真实嵌入 + 重排）未批准则写"C2 未测"。

## 4. 停止条件（事先写死，触发即执行动作并记录）

| 编号 | 条件 | 动作 |
|---|---|---|
| S-A | 出现任何 `tier = 'L0'` 的台账行 | 视为配置被绕过：立即回滚并排查 |
| S-C | 一个阶段累计 ≥ 300 页后，实际每页成本偏离 0.1356 元 ±10% | 暂停，对账发票，修正单价 |
| S-D | 一个阶段整份退回（`fallback_reason` 非空）的文档 > 5% | 暂停扩大，修复后继续 |
| S-F | 若将来启用跳页规则：每月随机抽 10 个跳页回原 PDF，任一页有非日期数字或实质内容 | 从 `PARSE_SKIP_RULES` 删除该规则；已跳页按台账补解析 |
| S-G | 本月 TextIn 花费 ≥ 月预算 30% | 暂停批次，问用户（ADR-0008 推翻线） |
| S-H | 任一批出现 `l3_no_output` 告警，或 P5 导致 `rejected` 的文档占比 > 10% | 暂停，先查原因（见 §1 末条） |
| S-I | 检索指标相对匹配基线下降且无法解释 | 暂停，不合并，按 §1.4 处理 |

## 5. 回滚

- **全部回退：** `.env` 设 `PARSE_TIERED_ENABLED=false`（或清空）。命令行下一次运行即生效；若 Dagster 在跑入库：`docker compose -f infra/docker-compose.yml --env-file .env up -d --force-recreate --no-deps dagster-daemon dagster-webserver`。
- **只回退减页：** 清空 `PARSE_SKIP_RULES`（台账保留）。L0 无需回滚（从未开启）。
- **已入库文档不动。** 回滚只影响之后新入库的文档；已入库块不重解析（会使 `gold_block_ids` 失效）。分级路由写入的文档可用下面的查询列出，是否重解析另行评估：

```sql
SELECT d.doc_id, d.title, a.state FROM core.document d
  JOIN core.parse_doc_acceptance a ON a.routes_ref = d.parse_json_ref
 WHERE d.superseded_at IS NULL;
```

- **台账表不回滚。** `core.parse_page_route` / `core.parse_doc_acceptance` 是运营表，保留作审计。
- **演练（第一批灰度之后做一次，尚未做）：** 关总开关 → 入库 1 份尚未入库的研报 → 确认该文档在 `core.parse_doc_acceptance` 里没有行（`routes_ref = parse_json_ref`） → 记录耗时（目标 ≤ 10 分钟）→ 恢复开关。

## 6. 告警与手动检查

自动告警（已接线）：入库时 `parse_ledger:l3_no_output`（某文档有 L3 页没有 TextIn 输出）。其余告警函数（整份退回、预算 30%、L0 未溯源）代码已有、未接定时任务，现阶段每批入库后手动查：

```sql
SELECT doc_type, tier, count(*) FROM core.parse_page_route WHERE routed_at > now() - interval '1 day' GROUP BY 1, 2;
SELECT state, count(*) FROM core.parse_doc_acceptance WHERE evaluated_at > now() - interval '1 day' GROUP BY 1;
SELECT count(*) FROM core.parse_doc_acceptance WHERE evaluated_at > now() - interval '1 day' AND fallback_reason IS NOT NULL;
```

外加 `docs parse-cost --month <当月>`。

## 运行记录

（尚无。每批追加：日期、命令、页数与费用、评测 run_id 与指标、停止条件检查结果。）
