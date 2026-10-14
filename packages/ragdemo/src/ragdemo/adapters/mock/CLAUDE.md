# packages/ragdemo/src/ragdemo/adapters/mock — 适配器 Protocol 的 Mock 实现

## 这个包做什么
CLAUDE.md §3 要求每个外部适配器都有 Mock。这里放离线、确定性的替身，让测试、CI 与 P1 评测不依赖网络、不花钱、不受供应商额度限制。
Mock 通过与真实实现**同一套契约测试**，因此行为必须与 Protocol 一致，不能为了方便放宽（例如 `known_at` 仍要晚于 `period_end`）。

## 关键模块
| 文件 | 职责 |
|---|---|
| `announcements.py` | `MockAnnouncementProvider`：内置公告样本，`disclosure_lag` 可调，通过 `tests/contracts/announcement_contract.py` |
| `facts.py` | `MockFactAdapter`：数字取自真实三季报公开数；`known_at`/`period_end` 由 `partition_date` 推导，回填历史分区也合法 |
| `mcp_gateway.py` | `MockMcpGateway`：内存工具清单与返回值，可模拟 `isError`、协议错误，避免 CI 打真实 Wind/iFind/Tushare（要钱、有套餐并发限） |
| `html_render.py` | `MockHtmlRenderer`：产出 N 页空白 PDF，无需本机浏览器 |

## 对外入口
- 被 `tests/` 与 `ingest/definitions.py`、`ingest/price_sources_build.py`、`ingest/cli.py` 的离线路径引用（如 `from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway`）。
- 只可 import `ragdemo.adapters.*` 的 Protocol/模型，不得 import 真实供应商实现以外的业务层。

## 本包的硬规则
- 新增适配器 Protocol 时同步在这里加 Mock，并在 `tests/contracts/` 加 `test_mock_*.py` 套用契约基类。
- Mock 不得读环境变量、不得联网、不得写库。

## 怎么测
`pytest tests/contracts`（免费，无数据库）。

## 常见坑
- 契约里「回填两年前的分区时 known_at 仍在历史区间」是真检验：Mock 写死 2024-09-30 这类日期会被抓出来，所以 `facts.py` 才从 `partition_date` 推导。
