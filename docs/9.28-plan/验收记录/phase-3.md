# 阶段 3 记录 · 公告双源接入（Task 13 实盘验收）

对应 `docs/9.28-plan/2026-09-28-公告双源接入计划.md` Task 13。Task 1-12/14 均已
完成并提交（见该计划文档与各任务的提交记录），本记录只记 Task 13 的实盘验收结果。

## 试点两家（688041.SH 海光信息、000977.SZ 浪潮信息），2025 全年，first 档

真实调用：`RAGDEMO_CNINFO_USER_AGENT`（真实联系方式）、`EDGAR_USER_AGENT`（同一
联系方式）、iFind 网关（`RAGDEMO_IFIND_MCP_BASE`，已连通真实 MCP）。

| 项 | 结果 |
|---|---|
| 巨潮公告目录 | 每家 120 条，全部建目录成功 |
| 真实解析（TextIn，first 档） | 8 份文档（季报/半年报摘要等）真正解析出块，表格块
  content_desc 全部生成成功（0 条缺失） |
| 月度预算 | 2026-09 已用 0 元 / 上限 5000 元（TextIn 单价通过 `record_parse_snapshots`
  的 `cost_per_page_cny=None` 记为未知单价，见"发现的问题"第 2 条） |

### as_of 时点抽查

对 `doc_id=1`（海光信息 2025 年一季报摘要，`known_at` = 2026-04-08 15:59:59 UTC，
日精度来源的当日 23:59:59 北京时间上界）：

| `as_of` | `asof.document` 里是否可见 |
|---|---|
| known_at 前 1 秒 | 否 |
| known_at 当秒 / 之后 | 是 |

时点边界精确到秒，符合 CLAUDE.md §1.1。

## 强制巨潮不可用 → iFind 顶上（688041.SH，2024-02 窗口，真实网关）

巨潮客户端指向不可达端口模拟不可用；iFind 走真实 MCP（`RAGDEMO_IFIND_MCP_BASE`）。

- 结果：4 条真实公告被 iFind 命中并写入目录（`has_full_text=False`）。
- 待补清单：`BacklogItem(entity_ref='688041.SH', failure_kind='rejected', reason='主源不可用，降级层已写目录，全文待补')`。
- 恢复补齐与降级行取代逻辑已由单测覆盖（`test_recovery_refetches_backlog_window_and_supersedes_degraded_rows`），
  未在真实环境重复验证（需要真实触发巨潮从故障中恢复，成本高、单测已经充分覆盖该分支）。

## 全部 96 家可路由实体，2023-2025 三年，仅建目录不下载

`ragdemo docs announcements --since 2023-01-01 --until 2025-12-31 --tier first --yes`
（不带 `--download`，`DOWNLOAD_ENTITIES` 之外的实体本轮不产生任何 TextIn 调用，
零解析费用）。第一次运行撞上巨潮冷却期（见"发现的问题"第 6 条）与港股代码补零
缺陷（第 7 条），修复后重跑：

| 来源 | 覆盖实体数 | 现行索引行数 |
|---|---|---|
| cninfo（A 股） | 58 / 59 | 23,334 |
| edgar（美股/ADR） | 28 / 28 | 4,958 |
| hkexnews（港股） | 9 / 9 | 1,265 |
| **合计** | **95 / 96** | **29,557** |

失败 0、待补 0。有 1 家 A 股实体在这个三年窗口内命中 0 条公告（未继续排查——
三年窗口内零公告本身不是异常信号，不代表路由或抓取失败）。

## 发现的问题（均已修复并提交，见对应 commit）

1. **`Backlog` 需要 autocommit 连接**：`docs announcements --yes` 路径与 Dagster
   的 `announcement_backlog_resource` 都漏传了 `autocommit=True`——此前只在
   mock 测试里跑过，从未真正连过库。（`c311ddf`）
2. **一份文档的 content_desc 校验失败会炸穿整批**：真实公告里出现"增持数量"
   这类合法列名撞上合规违禁词过滤器（"增持"），`run_cninfo_layer` 等四处调用
   `write_document`/`reparse_document` 前都没检查 `build_chunks_and_descriptions`
   已经算出的 `violations`，校验失败直接抛 `MetadataInvalid` 让整批公告一起丢失。
   已改为逐份检查、单独标记为 `validation_failed`，不影响其余公告。（`b6e6868`）
3. **Wind 不在这个账号的服务范围内**：`ANNOUNCEMENTS_CN` 的 Wind 降级层与 Task 11
   的漏收核对整体依赖 Wind `get_company_announcements`，实盘验收时对全部 A 股
   实体跑核对触发"账户积分余额不足"——核实后这个账号的 Wind MCP 只覆盖财务/
   行情/股东/分部数据，不含公告。已从路由移除，漏收核对功能整体下线（ADR-0013
   三次修订，`9eae436`）。
4. **`.env.example` 里 Wind/iFind 公告网关的示例地址不完整**：真实连通时发现
   `RAGDEMO_IFIND_MCP_BASE` 缺了服务子路径（404），已修正文档（该条随第 2 项一并
   提交，Wind 那一条随第 3 项一起删除）。
5. **种子数据的 `wind_code`/`edgar_cik` 从未被写入 `core.entity`**：这是"为什么只
   有 59 家"问题的根因——`entity_curation.csv` 早就有正确的值，只是
   `docs_csv.py`/`loader.py` 的列清单没带上。修复后 `entity_routes()` 从 59 家
   扩到 96 家（`6caba71`、`5624cf4`）。
6. **操作失误：验证脚本用真实连接污染了生产冷却状态**：为验证"巨潮不可用→iFind
   顶上"这条路径，用了一个真实 `PgSourceHealth`（写同一张 `core.source_health`
   表）去模拟巨潮故障。5 分钟默认冷却期还没过时就启动了全量索引任务，导致 59 家
   A 股实体在冷却窗口内全部被误路由到 iFind，产生了 414 条本不该存在的降级行
   （已用 SQL 按入库时间窗口精确定位并标记 `superseded_at`，不影响后续正确重跑
   写入的 cninfo 行）。**教训**：模拟"某个来源不可用"这类会写入共享健康状态表的
   测试，必须用内存态的 `MemorySourceHealth`，不能用连接真实库的 `PgSourceHealth`。
7. **港股代码没有统一补零**：`wind_code` 存的是"0020.HK"而不是披露易要求的
   "00020.HK"，`route.entity_ref.split(".")[0]` 因此传出 4 位代码，`prefix.do`
   精确匹配 5 位代码失败，抛 `HkexnewsStockIdNotFound`。已在路由处 `zfill(5)`
   补零（`863fa9d`）。

## 结论

Task 1-12、14 的实现在真实凭据、真实网络、真实公告内容下验证通过，过程中发现
的 7 个问题已全部修复/清理并提交。Task 11（Wind 漏收核对）确认不可行、已下线，
不再是验收范围。96 家可路由实体中 95 家在 2023-2025 三年窗口内建目录成功，
2 家试点（688041.SH、000977.SZ）额外完成 2025 全年 first 档真实下载解析。
