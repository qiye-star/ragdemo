# 阶段 2 记录 · A 股结构化数据

## 入口核查（2026-09-28）

阶段 1 已通过。按负责人决定：Tushare 改为官方 MCP 直连；新增 iFind MCP，大部分公告从 iFind 取。
两类凭据已写入被 git 忽略的 `.env`（`TUSHARE_TOKEN`、`IFIND_MCP_TOKEN`），只给出网代理注入用。
两者都在对话中明文出现过，接通后应轮换。

以下全部是只读核查（`initialize` + `tools/list` + 每个接口一次小查询），没有写库。

### 连接

| 服务 | 结果 |
|---|---|
| Tushare 官方 MCP（`api.tushare.pro/mcp/`，token 在 URL 参数） | ✅ 连通，协议 2025-06-18，无会话 ID，254 个工具 |
| iFind 11 个 MCP 服务（`api-mcp.51ifind.com:8643/ds-mcp-servers/<名称>`，`Authorization` 头直接放 token，不带 Bearer） | ✅ 全部连通，协议 2025-06-18，**需要会话 ID**（`Mcp-Session-Id`），共 55 个工具 |

### Tushare 账号权限：只有基础数据与行情

| 接口 | 结果 |
|---|---|
| `stock_basic` `trade_cal` `daily` `adj_factor` | ✅ 可用 |
| `income` `balancesheet` `cashflow` `fina_indicator` `fina_mainbz` `top10_holders` `forecast` `express` `disclosure_date` `fx_daily` `anns_d` `us_income` `hk_income` | ❌ 全部 `40203 没有接口访问权限` |

**阶段 2 的核心（财务三表、主营构成、前十大股东、汇率）在当前 Tushare 账号下拿不到。**

### iFind：自然语言查询型工具，不是结构化数据接口

iFind 的工具入参基本都是一句自然语言 `query`，返回 Markdown 表格或文本片段。

**公告（`search_notice`）**：只返回与查询相关的**段落片段**——`公告标题`、`公告片段内容`、
`日期`（日精度）。没有公告 ID、没有原文 / PDF 链接、没有页码与坐标；工具自己的说明是
"仅输出与查询内容强相关的公告片段文本，而非公告全文"。片段里的表格已被打散成乱序文本。

**财务（`get_stock_financials` / `get_security_indicators`）**：能取到数字，也能单独取到
"定期报告实际披露日期"（海光 2025 三季报 = 20251016，可作 `known_at`），但：

- 数值是四舍五入后的显示值（`94.8997亿`），即使要求"原始值(元)"也一样，精度约 1 万元；
- 查询参数由服务端从自然语言推断，返回的参数说明与请求不一致
  （请求 2025-09-30，返回的参数里出现"报告期：最新一期(MRQ)"与"季度：第一季度"）；
- 看不出是首次披露值还是追溯调整后的值。

### 这对原计划意味着什么

| 需要的数据 | 原计划来源 | 现状 |
|---|---|---|
| 行情、复权因子、交易日历、股票列表 | Tushare | ✅ Tushare MCP 可用 |
| 财务三表、主营构成、前十大股东 | Tushare | ❌ Tushare 无权限；iFind 可取但精度与参数可靠性有问题 |
| 汇率（币种换算） | Tushare `fx_daily` | ❌ 无权限 |
| 公告全文（切块、页码 / bbox 溯源的前提） | iFind（负责人决定） | ❌ iFind 只给片段；Tushare `anns_d`（带 PDF 链接）无权限 |

公告全文目前唯一可用的来源仍是手工下载 + manifest 导入（`docs/rawdata/`）。CLAUDE.md §4
不允许自建公告爬虫。

**阶段 2 在负责人对数据源作出决定之前不能按原计划推进。** 可以先做、且与决定无关的部分：
Tushare MCP 行情接入（2.3）、`stock_basic` 核对上市日期（2.8）、回填文档（2.9）、
不依赖汇率的口径换算与派生值（2.5 的一部分）。

## Wind 财务入库（2026-09-28）

13 家 A 股 × 12 个季度（2023Q3–2026Q2），现行 1245 条，`source = 'wind'`。适配器同时要累计值与
单季度值、按算术判定哪列是累计（Wind 列名不可信，见 `adapters/wind.py` 模块说明）；全量回填
暴露出的三种响应形态（两张表、宽表、列名 `_` 代替 `、`）都已处理。688041.SH 2024Q3 营收
6,136,540,000、2024FY 9,162,150,000（known_at 2025-03-01）与年报一致。

## Tushare 复测与数据源分工 / 降级（2026-09-28 下午，ADR-0012）

复测（经出网代理，客户端不带凭据）：

| 通道 | 可用 | 不可用 |
|---|---|---|
| Tushare MCP | `daily` | `stock_basic` `trade_cal` `adj_factor` **限频 1 次/小时**（上午还能用）；`income` `anns_d` `fx_daily` `disclosure_date` 无权限 |
| Tushare REST（同账号，新增一层） | `daily` | 同三个接口限频 1 次/分钟；财务、公告、汇率无权限 |
| Wind `stock_data` | 财务、日 K 线 | — |
| Wind `financial_docs` | 公告：标题、日期、链接、摘录 | 摘录随问句变（同一份半年报 25412 / 22098 字），不是全文；无日期参数；有重复、混入他司 |
| iFind `stock` / `news`（带会话） | 财务；公告片段，可按日期区间查 | 公告无 ID、无链接 |

分工与降级（`ragdemo.sources.routing.ROUTES`，测试守着"每个 MCP 服务至多一个主职"）：

| 数据 | 层 | 全部不可用 | 状态 |
|---|---|---|---|
| 日行情 | Tushare MCP → Tushare REST → Wind 日 K 线（降级） | 分区失败、游标不推进 | ✅ 已接入 Dagster；三层对 688041.SH 2026-09-24 实测逐位一致 |
| 财务 | 仅 Wind | 记入 `core.ingest_backlog`，下次触发自动补 | ✅ 实测：Wind 宕 → 0 写入、2 家进待补；恢复 → 自动补齐、待补解决 |
| 财务核对 | iFind，只告警不写库 | — | ⏳ 未实现（iFind 会话支持也未做） |
| 公告 | Wind financial_docs → iFind notice（降级） | 下次按日期区间重查 | ⏳ 待定：两家都给不了全文，见下 |

失败归类（无权限 24 小时 / 限频按报文窗口 / 不可用 5 分钟 / 响应被拒不冷却）与冷却状态存
`core.source_health`（迁移 019）。

Wind K 线的一个时点陷阱：默认前复权（2025-05-06 返回 149.71，不复权 150.01），前复权价随以后的
分红变，是前视数据；降级层逐日取、`afdate` = 当天，拿到的是不复权价。

**公告待负责人定：** 两家 MCP 都只给摘录 / 片段，进不了要求全文 + 页码溯源的文档管线。
