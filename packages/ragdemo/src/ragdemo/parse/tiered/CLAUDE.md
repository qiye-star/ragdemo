# packages/ragdemo/src/ragdemo/parse/tiered — 在 TextIn 之外加一层「逐页台账 + 逐页验收」的文档解析器

## 这个包做什么
`TieredParser` 包住 `TextInParser`：把整份 PDF（或去掉被跳过页后的连续段）送 TextIn，再按页拆回 detail[]，
为每一页写路由记录（tier / 原因 / 计费 / P1–P6 检查）和文档验收结论（P8）。位置：`ingest` 取到 PDF → **tiered** → 块入库。
**生产现状（2026-10-13 裁决）**：只有 L3（TextIn）一条路；L0 v2 未通过 G9，休眠；L1/L2（自托管引擎）只有试点代码；
减页规则只做"影子"记录，不真的跳页。

## 关键模块
| 文件 | 职责 |
|---|---|
| `parser.py` | `TieredParser`、`tiered_fingerprint`；整份 L3 路径；任何版面/规则/装配/页码基异常整份退回 TextIn，TextIn 自身异常原样抛 |
| `factory.py` | `with_tiered_routing(textin, blob, cfg)`：开关关闭时原样返回 TextIn；`parse_tier_l0_enabled` 为真则抛 `ConfigError(L0_REFUSED_MESSAGE)` |
| `census.py` | `force_whole_document_l3`（年报/半年报/季报整份 L3）、页普查 `classify_pages` |
| `skip.py` | 减页规则 `blank_page` / `signature_tail` / `research_disclaimer_tail`，`SKIP_RULES_VERSION = "skip-v1"`；`enforced_skips` 只放行显式启用的 |
| `textin_pages.py` | 按连续段切子 PDF 送 TextIn，页码映射回原文档 |
| `acceptance_inputs.py` | 把 L3 页装成 `parse.acceptance.PageInput`；`l0_page_input` 休眠 |
| `records.py` / `types.py` | `PageRecord` / `DocRecord` / `ParseLedger`、`PageRoute`、`TextInCall`（不 import parse 内其他模块，防循环） |
| `ledger.py` | 写 `core.parse_page_route` / `core.parse_doc_acceptance`（迁移 034）、成本估算、告警 |
| `page_offset_guard.py` | TextIn 页码基 0/1 核对，错一页则 `PageOffsetMismatch` |
| `cost_model.py` | 积压/预算估算（`docs parse-cost` 用）；`skip_audit.py`、`probe.py` 仅脚本用 |
| `verify.py`、`engines.py`、`http_engine.py`、`markup_normalize.py`、`pdfpages.py` | L1/L2 试点基础设施（只被 `scripts/l1_pilot.py`、`tables/page_engines.py`、`tables/html_grid.py` 引用）；`pdfpages.py` 被 `parser.py` 用于切子 PDF |

## 对外入口
- `with_tiered_routing`：`ingest/cli.py`、`ingest/definitions.py`、`ingest/announcement_sources.py` 构造解析器时调用。
- `ingest/reparse_cached.py` 直接构造 `TieredParser`（整份 L3、不启用减页）。
- 配置：`PARSE_TIERED_ENABLED`（默认 false）、`PARSE_TIERED_DOC_TYPES`（只允许 `announcement` / `research_report`；`annual_report` / `quarterly` 永远整份 TextIn，放进去会 `ConfigError`）、`PARSE_SKIP_RULES`（默认空，未知名报错）、`PARSE_TIER_L0_ENABLED`（硬拒绝）。
- 依赖：`parse.textin`、`parse.acceptance`、`parse.local.layout/l0/compare`、`tables.grid`、`ops.alerts`；禁止依赖 `ingest`。

## 本包的硬规则
- **L0 不得重开**：`config.py` 加载时拒绝、`factory.py` 再拒一次、`TieredParser` 构造不接受 L0 开关。重开需要新 ADR / 重开记录（ADR-0015 与 `docs/10.12-plan/验收记录/L0v2-结论.md`），不是改常量。
- **减页规则默认不生效**：未列入 `PARSE_SKIP_RULES` 的命中只写影子台账。要真启用，先看 `docs/10.12-plan/验收记录/减页规则测量.md`（门槛在测量前已登记、不得事后改）并跑评测（§1.4）。
- 指纹：`tiered_fingerprint` 含算法版本、skip 规则集、`ACCEPTANCE_VERSION`、pypdf 版本、TextIn 指纹；任何一项变 = 缓存键变 = 存量文档需重解析。
- 未启用减页时整份原 PDF 送 TextIn，与旧路径**同一缓存键**——这是 `reparse-cached` 能零调用重放的前提，别破坏。
- 台账表的 `routed_at` / `evaluated_at` 是运维时间，**不得进 `as_of` 过滤**（§1.1）。
- 费用单价 `TEXTIN_COST_PER_PAGE_CNY` 默认 0.1356（实测、待发票复核），缺失时 `cost_cny` 为 NULL，不猜。

## 怎么测
`uv run --no-sync pytest tests/parse/tiered -q -m "not db"`（免费）；`test_ledger.py` 有 `db` 标记，用 `temp_db` 隔离库。
`live_engine` 标记的引擎契约测试在 `tests/contracts/`，需要本机解析引擎容器，不进常规跑。

## 常见坑
- `PageRoute.tier` 的类型含 L1/L2，但生产只出现 `L3` 与 `skipped` 语境；别据此以为 L1/L2 在线。
- TextIn 页的图片 OCR 数字不在文字层，P3 对不上会判 `textin_unverified`（见 `acceptance_inputs.image_ocr_numbers`）。
- 跨页表只有一个块、页码是首页；`parser._table_blocks` 靠单元格 `page_id` 判续页，别改成只数块。
