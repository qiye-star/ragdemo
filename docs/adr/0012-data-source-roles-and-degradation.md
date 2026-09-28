# ADR-0012 · 数据源分工与降级：Tushare 管行情，Wind 管财务与公告，iFind 只做降级与核对

- 状态：已接受
- 日期：2026-09-28
- 决策者：数据底座负责人
- 影响：ADR-0005（公告供应商抽象）—— 给出第一批真实实现，不改接口

## 背景

2026-09-28 接入了三家的 MCP，并逐项实测（全部经出网代理，客户端不带凭据，
记录见 `docs/9.28-plan/验收记录/phase-2.md`）：

| 服务 | 能用 | 不能用 |
|---|---|---|
| Tushare 官方 MCP `/mcp/` | `daily` | `stock_basic` `trade_cal` `adj_factor` 限频 1 次/小时；`income` `anns_d` `fx_daily` `disclosure_date` 无权限 |
| Tushare Pro REST `/`（同一账号） | `daily` | 同上三个接口限频 1 次/分钟；财务、公告、汇率无权限 |
| Wind `vserver_stock_data` | 财务 `get_stock_fundamentals`；日 K 线 `get_stock_kline` | — |
| Wind `vserver_financial_docs` | 公告 `get_company_announcements`：标题、日期、原文链接、较长正文 | 按相关度召回，会混入同名公司 |
| iFind `hexin-ifind-ds-stock-mcp` | 财务（数值四舍五入到约 1 万元） | — |
| iFind `hexin-ifind-ds-news-mcp` | 公告 `search_notice`：标题、日期、片段；可按日期区间查 | 没有公告 ID、原文链接、页码 |

另有两个事实决定了降级不能"哪个能用就用哪个"：

1. **写入层的现行值与来源无关。** `core.fin_fact` 的现行唯一键是
   `(entity_id, metric_id, period)`，`core.price_daily` 是 `(entity_id, trade_date)`。
   降级源写进去的值，主源恢复后再写，差额会被当成一次更正——时点历史里凭空多出
   一次并不存在的重述。
2. **不同源的精度不同。** 收盘价在各家是同一个交易所数字；财务值不是：Wind 给 4 位
   小数的亿元（约 ±5000 元），iFind 约 ±1 万元。

## 决定

### 分工（每个 MCP 服务只有一个主职）

| 数据 | 第 1 层（主） | 第 2 层 | 第 3 层 | 全部不可用时 |
|---|---|---|---|---|
| 日行情 | Tushare MCP `daily` | Tushare REST `daily`（同账号，另一条通道） | Wind `get_stock_kline`（降级） | 本分区失败，游标不推进，下次重拉 |
| 财务三表科目 | Wind `get_stock_fundamentals` | —（不跨源写入） | — | 记入待补清单 `core.ingest_backlog`，下次触发自动补 |
| 财务核对 | iFind `get_stock_financials` | — | — | 只告警，**永不写库** |
| 公告（A 股） | 巨潮资讯网（全文，见 ADR-0013） | Wind `get_company_announcements`（降级，只写目录） | iFind `search_notice`（降级，只写目录） | 本实体进待补清单，全文等主源恢复后自动补 |
| 公告（港股 / 美股·ADR） | 港交所披露易 / EDGAR（全文，见 ADR-0013、2026-09-28 公告双源接入计划） | — | — | 本次跳过，下次触发重查 |

一个 MCP 服务在别的数据上只能以"降级层"出现，并在路由表里显式写出。路由表是代码
（`ragdemo.sources.routing.ROUTES`），有测试守着"每个服务恰好一个主职"。

### 什么时候切到下一层

适配器抛出的错误先归类（`ragdemo.sources.failures.classify`），再决定去留：

| 类别 | 例子 | 这一层怎么办 | 冷却 |
|---|---|---|---|
| 无权限 | Tushare `40203 没有接口访问权限`、HTTP 401/403 | 切下一层 | 24 小时（改权限要人工） |
| 限频 / 额度 | Tushare `频率超限(1次/小时)`、HTTP 429、`QuotaExceeded` | 切下一层 | 按报文里的窗口（1 次/小时 → 1 小时），否则 `Retry-After`，再否则 5 分钟；额度用尽 6 小时 |
| 不可用 | 连不上、超时、5xx、协议解不出 | 切下一层 | 5 分钟 |
| 响应被拒 | 数据校验不过（如 `WindResponseRejected`） | 切下一层，只对这一次 | 无 |

冷却记在 `core.source_health`（跨进程生效：Dagster 每个 run 是新进程）。每次切层
都写一条结构化日志并记进同一张表的最近一次失败，谁在什么时候因为什么被降级都能查到。

### 降级数据的标记

降级层写入的行，`source` 是真实来源（`wind` 而不是 `tushare`），`provider_snapshot`
里是降级层的原始响应。行情的 Wind 降级只在单位能精确换算时才成立：
`VOLUME`（股）÷ 100 = Tushare `vol`（手），`TURNOVER`（元）÷ 1000 = `amount`（千元）。

## 推翻条件

- Tushare 账号升级到有财务权限：财务第 2 层可以加 Tushare `income` 等（同样要过累计 /
  单季核对，并先解决与 Wind 的精度差，否则仍会产生伪更正）。
- Wind 公告的召回被证明不完整（与交易所披露清单比对漏报率 > 1%）：公告主源改为
  按日期区间可枚举的源。
- 写入层改成按来源分列存储：跨源写入的伪更正问题消失，财务可以开放降级写入。
