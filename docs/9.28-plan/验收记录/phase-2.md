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
