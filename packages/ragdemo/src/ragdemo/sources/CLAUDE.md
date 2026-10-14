# packages/ragdemo/src/ragdemo/sources — 数据源分工表、分层降级路由、冷却状态与待补清单（ADR-0012）

## 这个包做什么
同一项数据（日行情、财务、各市场公告）可能有多个来源。这里定义「谁是主源、谁是降级层」（`ROUTES`），
`SourceRouter` 按层尝试；某层失败时按原因归类、记冷却、切下一层。主源不可用又不允许跨源写入的数据（目前是财务）记入待补清单，下次触发先补。
位置：`adapters`（取数）之上、`ingest`（写入）之下。

## 关键模块
| 文件 | 职责 |
|---|---|
| `routing.py` | `Capability`（`price_daily`、`financials`、`financials_verify`、`announcements_cn/hk/us`）、`LayerSpec`、`ROUTES` 分工表、`layer_spec` |
| `router.py` | `Layer`、`SourceRouter`、`Routed`、`AllLayersFailed`；`Layer.run` 应含「取数 + 解析」，解析被拒也能切层 |
| `failures.py` | `classify(AdapterError)` → `Failure`（`permission` / `rate_limited` / `unavailable` / `rejected`），决定冷却多久 |
| `health.py` | `SourceHealth` 协议、`MemorySourceHealth`、`PgSourceHealth`（表 `core.source_health`，跨进程） |
| `backlog.py` | `Backlog`（表 `core.ingest_backlog`）：同一 `(capability, entity_ref)` 只保留一条未解决项，`since` 取最早缺口 |

## 对外入口
- 调用方：`ingest/prices.py`、`ingest/announcements.py`、`ingest/wind_facts.py`、`ingest/price_backfill.py`、`ingest/price_sources_build.py`、`ingest/definitions.py`、`ingest/schedules.py`、`ingest/cli.py`、`ingest/facts_cli.py`。
- 本包 import：`adapters.errors`、`adapters.mcp_gateway`（仅 `failures.py`）、`psycopg`。不得 import `ingest`。
- 分工依据：`docs/adr/0012-data-source-roles-and-degradation.md`。

## 本包的硬规则
- **改 `ROUTES` 就是改分工，必须同步改 ADR-0012**（`routing.py` docstring）。每个服务至多一个主职；换了供应商才算降级，同一供应商换通道（Tushare MCP → REST）不算。
- `failures.classify` 只归类 `AdapterError`；`KeyError`/`TypeError` 等是我们自己的 bug，不得当成「供应商不可用」去降级，必须原样抛出。
- `PgSourceHealth` 与 `Backlog` 必须用 **autocommit 连接**：入库流程回滚自己的事务时，冷却记录与「这里还缺数据」不能跟着消失。
- 跨源写入受 `LayerSpec.writes` 约束（例如 iFind 财务只做校验，`writes=False`）；`full_text=False` 的层进不了要求全文的文档管线。
- 被拒绝的原始响应照样留 `provider_snapshot`（CLAUDE.md §1.5），降级不得吞掉它。

## 怎么测
仓库根目录运行：`pytest tests/sources`。`test_router.py`、`test_routing.py`、`test_failures.py` 免费无库；`test_pg_state.py` 为 `@pytest.mark.db`（`temp_db`）。

## 常见坑
- 报文判断基于 2026-09-28 的实测原文（如 Tushare 的「没有接口访问权限」「频率超限(1次/小时)」），供应商改措辞时 `classify` 会落到错误类别，要更新并补测试。
- 冷却状态若只放内存，Dagster 每个 run 是新进程，被限频 1 次/小时的接口每次都会再被拒一次，所以生产必须用 `PgSourceHealth`。
