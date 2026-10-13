# EDGAR 8-K（doc_id=8）htm 转 PDF 再解析：准备工作与方案

日期：2026-10-13。本次只做免费准备：开发库只读 SELECT、本地离线渲染、读代码。没有调用 TextIn，没有付费调用，没有做嵌入，没有写库，没有改代码。

## 1. 结论

- 这份 8-K 转成 PDF 一共 **6 页**（不是估算的 12 页）。按单价 0.1356 元/页，付费解析约 0.81 元。
- `entity_id` 为空的原因：入库时没有对上实体，是数据补丁问题，不是代码缺陷。库里 `US.NVDA` 这个实体现在已经有 `edgar_cik=1045810`，补一条 UPDATE 就能修好。
- 推荐方案：新增一个免费的离线"渲染"步骤，把 PDF 存进 blob 并记一条来源快照；`parse-pending` 解析时优先用这份 PDF，新文档版本的 `raw_ref` 指向 PDF，`content_hash` 仍保持原 htm 的哈希。

## 2. doc 8 现状与 entity_id 为空的原因

只读查询结果：

| 字段 | 值 |
|---|---|
| doc_id / source / doc_type | 8 / edgar / 8-K |
| source_ref | 0001045810-26-000078 |
| entity_id | 空 |
| publish_at = known_at | 2026-09-03 16:03:56+00 |
| ingested_at | 2026-09-22 05:12:18+00（ingest_run_id = cli:docs-ingest-edgar） |
| raw_ref | edgar/0001045810/0001045810-26-000078/nvda-20260902.htm |
| content_hash | dc6f4e43…662d0（与磁盘上 htm 的 sha256 一致） |
| snapshot_id | 空（`record_blob_snapshot` 没有写成功或当时没有关联） |
| doc_block | 0 块 |
| announcement_index | 0 行 |

- 库里 edgar 来源只有这 1 份文档，所以没有"别的 EDGAR 文档是怎么拿到 entity_id 的"可对照。cninfo 的 596 份文档全部有 entity_id。
- `core.entity` 里 `US.NVDA` 存在，`edgar_cik='1045810'`（7 位，不补零），`wind_code='NVDA.O'`，实体创建于 2026-09-22 03:10，`updated_at` 为 2026-09-28 12:29。
- `ingest_edgar_filings` 传给写入器的是 `entity_ref=str(cik)`，也就是 `'1045810'`，与实体表的写法一致，所以匹配规则本身没问题。
- 写入器（`DocumentWriter._resolve_entity_id`）的设计是：对不上实体就只写一条警告日志，仍然入库，`entity_id` 留空（见该函数的注释，这是有意为之，为了不挡住新上市实体的公告）。
- 推断：入库时 `core.entity.edgar_cik` 这一列还没有回填（`updated_at` 晚于入库 6 天，符合"事后回填 cik"）。这一点是推断，数据库里没有当时的日志可以证实。
- 后果链：`entity_id` 为空，`parse_pending._select_pending` 仍会选中它，但 `_entity_ref_for(conn, None 的 entity_id)` 会抛 `UnknownEntityRef`。
- 同一时点还有一个更深的问题：`core.entity_resolution_queue` 是 0 行，写入器没有把这次未命中放进队列，所以没有人会被提醒。这条不在本次范围，只记录。

### 最小正确修法（本次没有执行）

一次性数据补丁，限定 doc_id=8、仅在 `entity_id` 仍为空时更新：

```sql
UPDATE core.document SET entity_id = 'US.NVDA'
 WHERE doc_id = 8 AND entity_id IS NULL AND source = 'edgar';
```

- doc 8 没有 `doc_block`、没有 `announcement_index`，所以不需要同步其他表的反规范化列。
- 解析之后写入新版本时，`reparse_document` 会用 `entity_ref` 重新解析 `entity_id`。修补后 `_entity_ref_for` 会返回 `NVDA.O`（`wind_code` 排在 `edgar_cik` 前面，都能命中同一个实体），新版本自然带上 `US.NVDA`。
- 不建议改代码去"容忍空 entity_id"：空 entity_id 的文档会在检索里丢失归属，应该补数据，而不是绕开。
- 可选加固（另开任务，不在本次）：`docs ingest-edgar` 入库后，如果写入器警告了未命中，就在实体表补齐 cik 后提供一个 `docs backfill-entity` 命令，按 `raw_ref` 里的 `edgar/<cik>/` 路径反查，批量补 `entity_id`。

## 3. 离线转 PDF（已做）

本机有 Edge 155，使用无头模式离线打印，没有任何网络上传：

```
msedge --headless=new --disable-gpu --no-pdf-header-footer --user-data-dir=<临时目录>
       --print-to-pdf=<输出.pdf> file:///<htm 路径>
```

htm 里没有外链资源（没有 `<img>`，除 XML 命名空间外没有 http 链接），所以渲染不会触发任何外部请求。wkhtmltopdf 没安装，也不需要。

结果（文件放在本次会话的 scratchpad 目录，没有提交进仓库）：

| 项 | 值 |
|---|---|
| 页数 | 6（Letter，612x792） |
| 文件大小 | 98,681 字节 |
| sha256（本次生成的那一份） | 9e3d22637395e0b2dd9789160323f4f2262c5e9e634b3305a8a9fe44cfc861df |
| 原 htm sha256 | dc6f4e4305f3beb1025dfe22c5e4938c92044132c979cccd00ddf96d748662d0（与库里 content_hash 一致） |
| 渲染器 | Skia/PDF，HeadlessChrome 155 / Edg 155 |

检查结果：

- 文字可抽取（pdfplumber）：第 1、3、4、5、6 页分别有 1995 / 3292 / 687 / 1696 / 331 个字符，封面表格被识别出 1 个。
- **第 2 页是空白页**（0 字符、0 图片），是 htm 里分页样式造成的。保留它只多花约 0.14 元，且保持页码与渲染结果一一对应，所以建议不删。
- 数字一致性：把 htm 正文（去掉 `ix:header` 隐藏区）与 PDF 抽取文本里的数字全集比较，两边各 45 个，**缺失 0 个、多出 0 个**。
- 文中关键数字（例如约 119 亿美元收购价、最多约 10 亿美元留任激励）在 PDF 第 3 页，和 htm 原文一致。
- **渲染结果不是逐字节可复现的**：同一份 htm 连渲两次，sha256 不同（PDF 里带创建时间戳）。所以必须"渲染一次、存起来、以存下来的那份为准"，不能每次解析前重新渲染，也不能指望用 htm 重新算出同一个 PDF 哈希。本次第二次渲染的哈希为 aaf5bd7f…6e9e（仅用于验证这一点，已丢弃）。

## 4. 存储与溯源设计（只设计，不实现）

### 4.1 核心原则

1. 原始 htm 永远保留，不改、不删。`core.document.source_url` 仍指向 SEC 原文。
2. PDF 是"派生物"，不是原文；它的来源（htm 的哈希、渲染器版本、PDF 自己的哈希）必须可查。
3. 解析产生的页码（`doc_block.page`、`[block_id | page]` 引用）指的是**派生 PDF 的页码**。htm 本身没有页的概念，不存在"原文页码"。引用展示处需要标明"页码以转换后的 PDF 为准"。

### 4.2 blob 键

- 原文：`edgar/0001045810/0001045810-26-000078/nvda-20260902.htm`（不动）。
- 派生 PDF：`edgar/0001045810/0001045810-26-000078/nvda-20260902.rendered.pdf`，与原文同目录，换扩展名并加 `.rendered` 标记，一眼能看出是派生物，也不会被将来重新下载的原文覆盖。
- 一旦写入就不覆盖。如果要换渲染器重做，用新的键（例如 `.rendered.v2.pdf`），旧版保留。

### 4.3 来源记录

两个层次，先做第一个，不需要改表：

- **A. provider_snapshot（推荐，零迁移）**：`record_blob_snapshot` 写一条，`provider='local-html-render'`，`endpoint='msedge --print-to-pdf'`，`params` 里放 `source_raw_ref`、`source_sha256`（即 htm 的 content_hash）、`pdf_sha256`、`renderer`（Edge 版本号）、`page_count`，`response_ref` 指向派生 PDF 的 blob 键，`cost_cents=0`。这符合 CLAUDE.md 1.5"入库同时保留原始响应与结构化结果"，而且快照只增不改。
- **B. 之后再考虑**：在 `core.document` 加 `derived_from_ref` 和 `derived_from_sha256` 两列（需要迁移脚本）。只有当出现第二个需要派生的来源、或要在界面上直接展示"此文档由 htm 转换"时才值得做。

### 4.4 解析后新文档版本的字段

| 字段 | 取值 | 理由 |
|---|---|---|
| raw_ref | 派生 PDF 的键 | 页码和块位置都对着这份 PDF，读取原文展示时也要读它 |
| content_hash | 仍为 htm 的哈希 `dc6f4e43…` | 文档去重索引是 `(source, content_hash)`，保持原文哈希，`docs ingest-edgar` 以后重跑才不会把同一份申报当新文档再入一遍 |
| source_url | SEC 的 htm 地址（不变） | 溯源回原文 |
| snapshot_id | 指向 4.3 A 的渲染快照；TextIn 调用自己的快照由 `record_parse_snapshots` 另记 | 一次付费调用一条快照的既有约定不变 |
| parse_warnings | 追加一条 `derived_from_html:<htm sha 前 12 位>` | 不用迁移，查询时也能看出来 |
| known_at | 沿用被取代版本（既有逻辑） | 晚解析不改变可知时刻，不引入 `ingested_at` |

注意 `content_hash` 与 `raw_ref` 所指字节的哈希在这类文档上**不相等**，这是有意的，要在代码注释和文档里写清楚，并且在校验脚本里不要用"raw_ref 的字节哈希等于 content_hash"当完整性检查——这类文档改查快照里的 `pdf_sha256`。

### 4.5 要扩展的代码路径

- 新增适配器接口（CLAUDE.md 1.5）：`HtmlRenderer` 协议（输入 htm 字节，输出 PDF 字节与渲染器标识），实现 `EdgeHeadlessRenderer`（调用 `msedge`，路径从环境变量取）和 `MockHtmlRenderer`（返回固定的最小 PDF）。放在 `packages/ragdemo/src/ragdemo/adapters/`，契约测试放 `tests/contracts/`。
- 新增 CLI 命令 `ragdemo docs render-html --doc-id N [--yes]`：免费、离线，默认 dry run。真执行时读 htm、渲染、写 blob、写快照；已经存在 `.rendered.pdf` 就跳过（不覆盖）。
- 改 `ingest/parse_pending.py`：
  - `_PendingDocument` 增加"解析输入键"概念：若存在派生 PDF（按 4.2 命名约定找，或从快照里查），就用它做 `raw_bytes_ref` 与 dry run 页数，否则用 `raw_ref`。
  - 如果 `raw_ref` 不是 PDF（扩展名或 `%PDF` 头判断）且没有派生 PDF，**直接报错并提示先跑 render-html**，不要把 htm 发给 TextIn（既浪费钱，又解析不出页码）。
  - 构造 `NormalizedDocument` 时 `raw_bytes_ref`=派生 PDF 键，`content_hash`=原 htm 哈希，`snapshot_id`=渲染快照。
- `ingest/documents.py` 的 `reparse_document` 不用改：它已经接受调用方给的 `raw_bytes_ref` 与 `content_hash`，只需要确认去重索引在"旧版先标记取代、再写新版"的顺序下不会因为相同的 `content_hash` 冲突（现有测试里重解析同一 PDF 时就是同一个哈希，已经覆盖）。
- `assets_docs.prepare_documents` 与 `pdf_pages.pdf_page_count` 不需要改，它们面对的已经是 PDF 字节。

### 4.6 TDD 测试计划（先写失败测试，再实现）

1. 契约测试：`HtmlRenderer` 的 Mock 与 Edge 实现输出都以 `%PDF` 开头、页数可读；Edge 实现用 `pytest.mark` 标成需要本机 Edge，默认跳过，不进 CI。
2. `render-html` 单测（用 Mock 渲染器 + 临时 blob）：写入 `.rendered.pdf`；写 1 条快照且 `params` 含 `source_sha256`、`pdf_sha256`；`cost_cents=0`；第二次运行跳过且不覆盖已有 PDF（字节不变）。
3. `parse_pending` 单测：
   - 有派生 PDF 时，dry run 页数等于 PDF 真实页数，不是 12 的估算。
   - 没有派生 PDF 且 `raw_ref` 是 htm：抛出明确的错误，并且 `MockDocumentParser` 一次都没被调用。
   - 解析后新版本 `raw_ref` 指向 PDF，`content_hash` 等于原 htm 哈希，`parse_warnings` 含派生标记，`known_at` 等于被取代版本。
   - `ingest_edgar_filings` 之后再对同一申报重跑，不会产生第二份 `source='edgar'` 的现行文档（去重仍以 htm 哈希为准）。
4. 回归：现有 cninfo PDF 路径的 `test_parse_pending.py` 全部不变且通过。
5. 数字一致性检查（脚本，不进单测）：htm 正文数字集合与 PDF 文本数字集合相等；本次 doc 8 已验证为 45 对 45。
6. 不涉及数值计算和时点逻辑的改动，但沿用 `known_at` 的单测不得被破坏；按 CLAUDE.md 1.4，改动不涉及 prompt、切块、检索参数，所以不触发三套评测，但解析后应补一次 `eval_retrieval` 看新增的块有没有拉低指标。

## 5. 付费解析要执行的命令（交给负责人）

前置（都免费）：

1. 修补 `entity_id`（见第 2 节 SQL）。
2. 渲染并存好 PDF：目前 `render-html` 命令还没有实现，完成第 4 节的实现后执行。
3. 先 dry run，确认页数为 6：

```
ragdemo docs parse-pending --doc-id 8 --tier first
```

确认后再付费（约 6 页，约 0.81 元，`--max-pages 6` 作为预算上限）：

```
ragdemo docs parse-pending --doc-id 8 --tier first --max-pages 6 --yes
```

说明：

- 不加 `--yes` 只做 dry run；加了才会调用 TextIn 并写库。
- `PARSE_TIERED_ENABLED` 等分级开关保持现状；这份 PDF 本身只有 6 页，不需要分页路由来省钱。
- 不传 `--entity-ref`，用 `--doc-id` 精确限定即可。
- 在第 4.5 节的代码改动合入之前，直接对 doc 8 执行 `--yes` 会因 `raw_ref` 是 htm 而失败或把 htm 发给 TextIn，**不要这样做**。

## 6. 未验证与风险

- entity_id 为空的根因是按时间线推断的，没有入库时的日志佐证。
- Edge 渲染结果依赖本机浏览器版本；换机器或升级后分页可能不同，所以必须存下渲染成品，并在快照里记录渲染器版本。
- 单价 0.1356 元/页是估算值（见 `config.py` 注释，需以发票复核），所以 0.81 元是估算。
- 本次只验证了一份很短的 8-K。篇幅很长的 EDGAR 文档（10-K 等）应继续按分级策略处理，不属于这份方案的范围。
- 8-K 里若带 EX-99 新闻稿附件，当前入库只取了主文档（primary_document）；附件是否要一起转，需要另行决定，本次未涉及。
