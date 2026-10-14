# packages/ragdemo/src/ragdemo/tables — 表格网格：从 TextIn 单元格建网格、绑单位/期别、逐格校验、引用到格、与 Wind 事实对账

## 这个包做什么
把解析出的表格变成引擎无关的 `TableGrid`（每格带 page / bbox / 校验状态），推断表头、绑定表名/单位/币种，用 PDF 文字层逐格校验数字，
再存进 `core.doc_table`（读只走 `asof.doc_table`）。下游：检索结果带 `table_unit / table_caption / table_verdict`；
`[block_id | page | r{row}c{col}]` 单元格引用供验证器判定数字有无出处；`fact_check` 用已解析表格去佐证 Wind 事实。
位置：`parse.textin.blocks_from_detail` 建网格 → `ingest.documents` 写库 → `retrieval` 读 → 验证器/事实核对用。

## 关键模块
| 文件 | 职责 |
|---|---|
| `grid.py` | `TableGrid` / `GridCell` / `TableAttachment`，`GRID_VERSION`，JSON 往返，`CellCheck` 五种状态 |
| `textin_cells.py` / `html_grid.py` | TextIn detail 表格 → 网格（每格自带 `page_id` 与 8 点坐标）；L1/L2 的 HTML 表 → 网格（保守，不忠实就返回 None） |
| `headers.py` / `statements.py` / `continuation.py` | 表头推断（结果是"推断"）、报表类型、非 L3 跨页续表只链接不合并 |
| `context.py` | `bind_context`：表名 / 单位 / 币种 / 列单位 / 行单位；冲突、复合、非金额单位一律 → 单位未知（None） |
| `numbers.py` | `cell_value`：整格只陈述一个数才返回 Decimal；分词复用 `parse/local/number_closure.py` |
| `periods.py` | 列表头 → `PeriodRef(period, basis)`，含糊返回 None |
| `verify_cells.py` | 逐格文字层校验，`VERIFY_CELLS_VERSION = "cellverify:v3"`，表级 `TableVerdict`（含 `dual_agree`） |
| `store.py` | `core.doc_table` 读写；反规范化列（`known_at` 等）一律从所属块读回，调用方不能传 |
| `backfill.py` | 存量文档补网格（`ragdemo docs backfill-tables`），整份文档一个事务 |
| `citation.py` | `CELL_CITATION` 解析、`resolve_cell`（经 `asof.doc_table`）、`cell_supports_value` |
| `fact_check.py` | Wind 事实 ↔ 表格单元格逐格校验，只写 `core.fact_table_ref`（迁移 031），`ragdemo facts table-check` |
| `render.py` / `page_engines.py` | 网格 → Markdown/HTML（与库内存量逐字相等）；旁路 JSON `routes` → 每页引擎与层级 |

## 对外入口
- `parse/textin.py` 调 `grid_from_textin_item`、`bind_context`；`parse/chunker.py` 传 `TableAttachment`；`ingest/documents.py` 经 `store.insert_table` 写。
- CLI：`ragdemo docs backfill-tables [--doc-id N] [--yes] [--reverify] [--json-out F]`、`ragdemo facts table-check`（默认 dry run，只读）。
- 本包 import：`parse.local.*`（数字口径、版面）、`parse.tiered.types/verify`、`ingest.stage2_crosscheck`（`FACT_LABELS`、`period_of_title`，`fact_check` / `periods` / `statements` 用）、`dataapi.contract.require_as_of`。与 `parse` 互相引用，注意循环 import。

## 本包的硬规则
- **假通过比漏报危险**：所有规则偏向"不知道/不佐证"。单位错一个数量级就是 10^4/10^8 的错，所以单位冲突、复合单位、非金额单位都置 None；币种不在 (None, CNY) 不换算；**不默认人民币**。
- 只有 `check == "match"` 的格能佐证数值；`not_checked` / `no_text_layer` / `no_numbers` / `mismatch` 的格只能以 `推断` 引用（§0）。缺 `page` 或 `bbox` 的格不可验证。
- 校验是单页、不放宽 ±1 页；数字多重集必须与框内文字层**相等**，不是子集。扫描页、旋转页、无坐标格永不记 `match`。
- 时点：`doc_table` 行继承块的 `known_at`；回填走 supersede（旧行 `superseded_at`、插新行），**不原地改**；`now()` 早于日精度的公告 `known_at` 时 supersede 会违反 `doc_table_time_order`，回填记 `deferred` 不动。应用层只查 `asof.doc_table`，`store.live_table_meta` 是唯一读 `core` 的函数。
- 改 `GRID_VERSION`、`VERIFY_CELLS_VERSION`、单位/期别规则 = 存量网格需重算：先跑 `eval table-gate`（§1.4）再回填；`fact_check` 的容差与标签表写死，别为了过更多事实放宽。
- `fact_check` 不是事实来源，**永不修改 `core.fin_fact`**；母公司表/母公司列不佐证合并口径事实。
- 数值转换全部由代码完成（§1.2）；`numbers.token_to_decimal` / `cell_decimals` 仅供诊断，消费者必须用 `cell_value`。

## 怎么测
- 免费、无库：`uv run --no-sync pytest tests/tables -q -m "not db"`。
- `-m db`（`test_backfill.py`、`test_store.py`、`test_citation.py` 等）用 `tests/conftest.py::temp_db` 起随机临时库，不碰开发库；夹具助手 `tests/tables/conftest.py::insert_test_grid`。
- 语料测试（`test_corpus_invariants.py`、`test_textin_corpus_attach.py`）读 `data/blob/parse/textin`，目录不存在则跳过；全是本地离线，不调 TextIn。

## 常见坑
- 已知失败：`tests/tables/test_textin_corpus_attach.py` golden `KeyError`——本机缓存里有 `tests/fixtures/tables_textin_golden.json` 没登记的 JSON，不是代码回归；别为了变绿去改 golden 而不查原因。
- 跨页表只有一个块（页码=首页），续页的格靠自带 `page_id`；别按块页码判页。
- `reverify` 之外，同一 `(grid_version, verify_version)` 的现行网格会被回填跳过；`not_checked` 旧行遇到能得到真实结论才重算。
- 引用页码位可写 `—`（`CELL_CITATION` 允许），表示页未知，解析为不可验证。
