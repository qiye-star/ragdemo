# packages/ragdemo/src/ragdemo/parse/local — 本地确定性排版（L0 v2），已休眠；仅少数工具函数仍在生产路径

## 这个包做什么
用 pdfplumber 取每页字符与图形，不调任何模型：去装饰（页眉页号）、识别图表、分栏、表格坐标聚类还原、按字号排版成 markdown，再用数字/字符闭合校验证明"一字未多"。
目标是替代部分 TextIn 页以省钱。**2026-10-13 裁决：L0 v2 未通过 G9（阅读顺序人工抽查），不启用**，生产不产生 L0 页。
结论与留出集结果见 `docs/10.12-plan/验收记录/L0v2-结论.md`、`L0v2-留出集结果.md`、`L0v2-调参记录.md`；排版计划见 `docs/10.12-plan/2026-10-12-确定性排版第二版计划.md`。

## 关键模块
| 文件 | 职责 | 生产是否引用 |
|---|---|---|
| `layout.py` | `extract_pages` → `PageLayout`（词/图形/文字层） | 是（tiered 解析器、`tables/verify_cells.py`） |
| `number_closure.py` | `numbers_in`、`normalize_number`、`closure_violations` 数字分词口径 | 是（`tables/numbers.py`、`verify_cells.py`、tiered 都复用，是全仓库唯一的数字口径） |
| `compare.py` | `plain_text_from_markdown`、`text_coverage` | 是（`parse/acceptance.py`、`tiered/verify.py`、`acceptance_inputs.py`） |
| `l0.py` | `classify_page` / `page_kinds` / `parse_pdf_l0` | 仅 `page_kinds`（P5 页型） |
| `columns.py`、`tables.py`、`typeset.py`（`body_font_size`）、`pipeline.py`（`needs_handback`） | 分栏、表格区域、字号 | 仅 `tiered/census.py` 引用 |
| `chart.py`、`decor.py`、`headings.py`、`structure.py`、`char_closure.py` | 图表剥离、装饰剥离、标题编号校验、LLM 排版（`LlmStructurer`）、字符闭合 | 否（L0 v2 流水线内部） |
| `report.py`、`det_report.py`、`l0_eval.py`、`recall_diag.py`、`samples.py` | TSV 报告、留出集评测、漏召诊断、调参/留出集划分 | 否（`scripts/l0v2.py` 等离线实验用） |

## 对外入口
`parse.tiered.*`、`tables.*`、`parse.acceptance` 引用上表"是"的几个文件；`scripts/l0v2.py`、`scripts/audit_typeset.py`、`scripts/parse_compare.py` 做离线实验。
本包不得 import `ingest`、`retrieval`、`evals`；也不该为了 L0 去改 `number_closure` 的分词口径（会同时改变表格校验与 P3 审计的判定）。

## 本包的硬规则
- 闭合校验**零容忍**：structured 里出现 source 没有的数字或内容字符就报；虚警可接受，漏报不可接受（`number_closure`、`char_closure` 文档头）。
- 没有任何排版结果可以改写、补全或"修正"数字；L0 只允许移动/断行/加 `#`，不得删字符。
- 不要把休眠代码接回生产：`PARSE_TIER_L0_ENABLED` 被配置层硬拒绝，见 `../tiered/CLAUDE.md`。
- `samples.py`：留出集（`holdout*`）调参类命令不得读取（`require_tune_set` 直接抛错）；拆分清单入库后不得重生成；研报原文不入 git（ADR-0014），测试夹具只留几何、文字已换占位符。

## 怎么测
`uv run --no-sync pytest tests/parse/local -q`（全部免费：无数据库、无外部调用）。

## 常见坑
- `number_closure` 把"字母/CJK 后的 `-`"当负号，`EPS-0.5`、`A-1` 会虚警——有意保守。
- 引用面小但牵一发动全身：改 `layout.py` / `number_closure.py` 前先 grep `tables/` 与 `parse/tiered/`，并重跑 `tests/tables`、`tests/parse/tiered`。
