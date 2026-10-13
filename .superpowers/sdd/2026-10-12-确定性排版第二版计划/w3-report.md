# W3 报告：页型普查脚本 + 定期报告识别

状态：DONE。

交付
- `packages/ragdemo/src/ragdemo/parse/tiered/census.py`：`is_periodic_report`、`force_whole_document_l3`、`classify_pages`、`summarize`、`Census`（`scan_share` / `statement_share` 与路由 T7 `CostInputs` 同口径）。
- `scripts/parse_probe.py census`（4 进程，约 3 分钟）；测试 `tests/parse/tiered/test_census.py`（47 个）；盘点文档新增第 6 节。
- ruff check/format、mypy --strict（census.py、parse_probe.py）通过。

定期报告规则：半年度先于年度判；摘要按保守口径算定期报告（强制 L3）；标题含 公告/决议/制度/预告/快报/更正/问询/核查/意见/召开/述职 等的排除；仅简称命中且已知页数 < 4 的排除；页数本身不构成证据；「年度/半年度财务报告」算。
集成点：路由计划 T9 的 `TieredParser.parse_for`，在按 `doc_types` 判范围前调用 `force_whole_document_l3(doc_type, title, filename)`。本任务未改 route / config。

普查（602 份，3 分钟；跳过 0；读取失败 1 = doc 352）
- 非定期公告 4348 页：无文字层 18.2%、报表 0.9%、表格 14.2%、多栏 10.3%、纯文字 56.4%。
- 定期报告 2195 页：报表 25.4%、表格 37.5%、多栏 6.6%、纯文字 30.5%。
- 研报 596 页：无文字层 1.2%、报表 15.1%、表格 46.3%、多栏 9.2%、纯文字 28.2%。
- 路由范围：scan_share 0.1618，statement_share 0.0314，纯文字 53.0%。
- 库内识别出 30 份定期报告；219/231/213 页三份与 166–174 页半年报均找到（共 11 份整份报告约 1900 页，doc_type 都是 announcement）。

关注点
1. 普查是代理（无装饰剥离、无图表检测，不产出 chart_unresolved）；疑似表格行常量在 census.py 内近似，`l0.tablelike_run` 落地后应改为导入。
2. 路由范围 800 个无文字层页中 510 页来自 4 份整份扫描文档（doc 242/366/422/498）。
3. 未调任何常量；留出集文档含在这个描述性普查里。
