# ADR-0008 · 通用文档解析用合合信息 TextIn xParse，取代 MinerU

- 状态：已接受
- 日期：2026-09-21
- 决策者：创始团队
- 取代：无（MinerU 此前只是 `05-document-pipeline.md` 的默认选择，未成文为 ADR）

## 背景

`05-document-pipeline.md` §1 有两条解析路径。路径 A 走公告供应商的结构化接口，
路径 B 处理供应商覆盖不到的文档：EDGAR 的 10-K/10-Q、IR 页上的 PDF、
用户上传材料、以及路径 A 临时失效时的兜底。路径 B 原定用 MinerU（自建容器）。

路径 B 的产出质量直接决定两件事：

1. **表格结构还原**。研究引擎的数值几乎全部来自财务报表与分部数据表，
   表格还原错了，后面的抽取、评测、观点全部建立在错数上。
2. **章节层级识别**。`section_path` 是切块边界与块首上下文的来源
   （`05-document-pipeline.md` §3.1），层级错了会让块跨小节，稀释检索信号。

MinerU 在实际使用中的问题：中文财报的跨页合并表、合并单元格、
带底纹的三线表还原不稳定；章节层级靠版面启发式推断，
长文档中途容易错位；自建容器占 8G 内存且偶发崩溃，需要队列与超时兜底
（原 §2 的一半篇幅在处理这件事）。

## 备选方案

**A. 继续自建 MinerU**
数据不出网，无按页计费。但表格与层级质量需要自己调，
运维成本（内存、崩溃、版本升级导致切块结果变化）由我们承担。

**B. 合合信息 TextIn xParse（托管 API）**
`POST https://api.textin.com/ai/service/v1/pdf_to_markdown`。
一次调用同时返回 Markdown 全文与 `detail[]` 结构化数组，
后者带 `outline_level`（-1 正文 / 0+ 标题层级）、`content`（0 正文 / 1 页眉页脚）、
表格的 `cells[]`（含 `row_span` / `col_span`）、逐元素 `position` 四角坐标。
境内服务，按页计费。

**C. 自建 MinerU + TextIn 兜底**
两套都维护。

## 决策

**选 B，TextIn xParse 全量取代路径 B 的 MinerU。**

决定性的理由不是「解析更准」——那是可以调的——而是 **`detail[]` 直接给出了
我们本来要自己推断的三样东西**：

| 我们需要的 | MinerU 路径 | xParse 路径 |
|---|---|---|
| 标题层级 | 版面启发式推断 | `outline_level`，-1 为正文，0+ 为层级 |
| 页眉页脚过滤（§3.3） | 正则 + 位置规则 | `content == 1` 直接标出非正文 |
| 表格合并单元格 | 需从版面重建 | `cells[].row_span` / `col_span` |

前两项在原设计里是 `src/parse/filters.py` 的启发式规则，**启发式规则会在
换一种排版的文档上静默失效**——不报错，只是少召回或多噪声。换成供应商标注的
字段后，这两类 bug 从「调参」变成「供应商标错了，可以提工单」。

方案 C 被否决的理由是它要求两套切块逻辑长期保持行为一致，
而评测集的 `gold_block_ids` 绑定在具体切块结果上，两套逻辑意味着两套评测基线。
起步期团队养不起。

### 为什么从 `detail[]` 切块，而不是从 `markdown` 切块

`markdown` 字段是一整个字符串，**丢失了页码与坐标**。
CLAUDE.md §0 要求「输出中出现的任意数值，要么绑定 `[block_id | page]`，
要么显式标注为推断」——从 Markdown 切出来的块拿不到 `page`，
这条硬约束就无法满足。因此：

- **切块的输入是 `detail[]`**（结构化数组，逐元素带 `page_id` / `position`）；
- **`markdown` 与完整响应 JSON 双双落盘存档**，用途是人工核对、
  跨版本 diff、以及不重新调用 API 就能重切块。

用户要求的「存 JSON + 存 Markdown」由此落位：JSON 是重切块的数据源，
Markdown 是人读的档案，两者都不是运行时的检索输入。

### 锁定的调用参数

参数变化会改变切块结果，因此参数集合是**版本化的一部分**，
其指纹进入 `document.parse_engine`（见「后果」第 3 条）：

| 参数 | 值 | 理由 |
|---|---|---|
| `parse_mode` | `auto` | 扫描件与电子版混杂，让供应商判断 |
| `markdown_details` | `1` | **必须**。`detail[]` 是切块的唯一输入 |
| `apply_document_tree` | `1` | 生成标题层级，`outline_level` 的来源 |
| `apply_merge` | `1` | 合并跨页表格与段落。财报表格跨页是常态 |
| `table_flavor` | `html` | 只影响存档的 `markdown`。HTML 能表达合并单元格，Markdown 不能——存档要无损 |
| `catalog_details` | `1` | 返回目录树，用于识别目录页（§3.3 要过滤掉） |
| `page_details` | `0` | 关掉 `pages[]`。逐行 OCR 结果能把响应体撑大一个数量级，而我们从 `detail[]` 切块，用不到它 |
| `raw_ocr` / `char_details` | `0` | 同上，字符级坐标我们不消费 |
| `get_image` | `none` | P1 没有图像管线。且图片 URL 30 天过期，存下来就是悬空引用 |
| `apply_image_analysis` | `0` | 会把图片送去大模型解读，越过「计算与生成分离」的边界 |
| `formula_level` | `0` | 财报几乎没有公式 |
| `dpi` | `144` | 供应商默认值，坐标参照系 |

`table_flavor=html` 只作用于存档的 `markdown` 字段。
**检索用的表格块 `content` 不取自 `markdown`，而是由我们从 `cells[]` 自行拼 Markdown 表格**，
把 `row_span` / `col_span` 覆盖到的格子填上重复值。
理由是 BM25 索引里不能混 HTML 标签——`chinese_lindera` 分词器会把标签切成词元，
污染词频统计（`02-data-model.md` §5.5）。

## 后果

1. **文档内容离开我们的网络。** 这是与 MinerU 最实质的区别。
   TextIn 是境内服务（上海合合信息），满足 CLAUDE.md §0 的「数据境内存储」。
   但「境内」不等于「可以发任何东西」：
   - P1–P3 只送**公开文档**（公告、年报、EDGAR 备案、IR 页 PDF）——
     这些本来就是公开的，外送不增加泄露面；
   - **用户上传材料默认禁止外送**。解析器对 `owner_user IS NOT NULL` 的文档
     直接拒绝，除非显式打开 `TEXTIN_ALLOW_PRIVATE`（默认 `false`）。
     P4 上线用户上传前，必须先有签署的数据处理协议 + 明确的用户告知，
     或者为私有材料保留一条本地解析路径。这是**代码里可执行、可测试的闸门**，
     不是文档里的一句提醒。
2. **按页计费，成本进入设计约束。** 首次全量入库是数十万页。对策：
   - 解析产物按 `sha256(原始字节) + 参数指纹` 落对象存储，
     调用前先查缓存命中则不调 API。同一份文档永远只解析一次；
   - 每次 Dagster run 有页数预算上限，超预算停止并告警，不静默烧钱；
   - 这套缓存同时是契约测试的录制-回放夹具来源（`04-ingestion.md` §6）。
3. **`parse_engine` 必须同时记录供应商版本与参数指纹**，
   格式 `textin:<result.version>+<参数指纹前 8 位>`。
   只记版本不够：参数一改，同一版本 API 的切块结果也会变，
   而评测集的 `gold_block_ids` 绑定在切块结果上。
4. **密钥不进业务容器。** 按 `09-compliance-security.md` §4.1，
   `api.textin.com` 进出网代理白名单，`x-ti-app-id` / `x-ti-secret-code`
   由代理转发时注入。业务代码只知道代理地址。
5. **`infra/docker-compose.yml` 不再需要 mineru 服务**，
   整栈少一个 8G 内存的容器。原 §2 里关于队列、崩溃恢复、内存上限的设计随之删除。
6. **供应商不可用时路径 B 整体停摆**，不像自建还能降级跑。
   缓解：已解析文档的产物在对象存储里，重跑不受影响；
   新文档积压在 Dagster 分区里，供应商恢复后回填即可。
   `document.parse_engine = 'textin:skipped'` 的文档数是需要盯的指标。
7. **`core.document` 增列**：`parse_json_ref` / `parse_md_ref` / `parse_warnings`
   （迁移 `006_parse_artifacts.sql`，只加不改）。

## 推翻条件

- 表格还原或章节层级的实测准确率**不优于 MinerU**——
  用同一批标注文档双跑对比，指标不占优就没有理由付费；
- 月度解析支出占预算（5000 元/月）比例超过 30%，
  且缓存命中后的增量成本仍在上升；
- 合规要求文档不得离开本机（私有化客户的硬性要求）→
  退回自建解析，此时 `DocumentParser` 协议让切换不改业务代码；
- 供应商可用性低于 99%，或出现一次数据处理违约；
- TextIn 变更 `detail[]` 的字段语义且不提供版本化端点——
  我们对 `outline_level` / `content` / `cells` 的依赖是强依赖，
  语义漂移会静默改变切块结果。
