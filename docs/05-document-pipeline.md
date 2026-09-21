# 05 · 文档解析与切块管线

## 1. 两条解析路径

```
                  ┌─ 路径 A：供应商结构化接口（A 股 / 港股公告，首选）
文档来源 ──────────┤
                  └─ 路径 B：TextIn xParse（EDGAR、IR 页 PDF、用户上传、供应商缺失时兜底）
                                      │
                        两条路径都产出 NormalizedDocument
                                      │
                                      ▼
                        切块 → 父子块 → 元数据 → 嵌入 → 入库
```

**优先级**：同一份文档若两条路径都可得，用路径 A。原因是供应商已经做过表格结构还原与
章节识别，`section_path` 与表格准确率显著高于通用解析，而这两项直接决定检索质量。

路径 B 产出的文档在 `document.parse_engine` 中记为 `textin:<版本>+<参数指纹>`
（§2.2 说明为什么参数指纹必须在里面），检索评测时按 `parse_engine` 分组统计——
两条路径的召回率差距是判断供应商是否值钱的依据。

## 2. TextIn xParse 封装

路径 B 用合合信息 TextIn xParse，取代原先的自建 MinerU，
理由与推翻条件见 [adr/0008](adr/0008-textin-xparse-document-parsing.md)。
一句话版本：`detail[]` 直接给出了我们本来要靠启发式规则推断的标题层级、
页眉页脚标记和表格合并单元格，把一类静默失效的 bug 变成了可提工单的供应商问题。

### 2.1 调用契约

```
POST https://api.textin.com/ai/service/v1/pdf_to_markdown?<参数见 §2.2>
x-ti-app-id:      <由出网代理注入>
x-ti-secret-code: <由出网代理注入>
Content-Type:     application/octet-stream      # 请求体是文件二进制流
```

也支持 `Content-Type: text/plain` + 请求体为文件 URL 的形式。
**我们只用二进制流形式**：走 URL 意味着让供应商去拉取源站，
出网记录里就少了一条我们自己的抓取轨迹，
而 `04-ingestion.md` §6 要求每次外部调用都可录制回放。

两个认证头**不出现在业务容器的环境变量里**。按 `09-compliance-security.md` §4.1，
`api.textin.com` 进出网代理白名单，密钥由代理在转发时注入，
业务代码只知道代理地址。

限制（供应商侧）：单文件 ≤ 500MB，PDF ≤ 1000 页。

### 2.2 锁定的参数

**参数集合是版本化的一部分。** 改任何一个都会改变切块结果，
而评测集的 `gold_block_ids` 绑定在具体切块结果上。因此参数表的 canonical JSON
取 `sha256` 前 8 位作为**参数指纹**，与供应商返回的 `result.version` 一起
写进 `document.parse_engine`：`textin:4.2.1+a3f19c02`。

只记供应商版本是不够的——同一版本 API 换套参数，切出来的块就不是同一批东西了。

| 参数 | 值 | 理由 |
|---|---|---|
| `parse_mode` | `auto` | 扫描件与电子版混杂，让供应商判断 |
| `markdown_details` | `1` | **必须**。`detail[]` 是切块的唯一输入 |
| `apply_document_tree` | `1` | 生成标题层级，`outline_level` 的来源 |
| `apply_merge` | `1` | 合并跨页表格与段落。财报表格跨页是常态 |
| `table_flavor` | `html` | 只影响存档的 `markdown`。HTML 能表达合并单元格，Markdown 不能——存档要无损 |
| `catalog_details` | `1` | 返回目录树，用于识别目录页（§3.3 要过滤掉） |
| `page_details` | `0` | 关掉 `pages[]`。逐行 OCR 能把响应撑大一个数量级，而我们从 `detail[]` 切块 |
| `raw_ocr` / `char_details` | `0` | 同上，字符级坐标我们不消费 |
| `get_image` | `none` | P1 没有图像管线。且图片 URL 30 天过期，存下来就是悬空引用 |
| `apply_image_analysis` | `0` | 会把图片送去大模型解读，越过「计算与生成分离」的边界（CLAUDE.md §1.2） |
| `formula_level` | `0` | 财报几乎没有公式 |
| `paratext_mode` | `annotation` | 供应商默认值；页眉页脚的识别我们用 `content == 1` |
| `dpi` | `144` | 坐标参照系 |
| `page_start` / `page_count` | `1` / `1000` | 全文，取供应商上限 |

`table_flavor=html` 只作用于**存档**的 `markdown` 字段。
检索用的表格块 `content` 不取自 `markdown`，而是由我们从 `cells[]` 自行拼
Markdown 表格（§2.5）。BM25 索引里不能混 HTML 标签——`chinese_lindera` 分词器
会把标签切成词元，污染词频统计（`02-data-model.md` §5.5）。

### 2.3 为什么从 `detail[]` 切块，而不是从 `markdown`

`markdown` 是一整个字符串，**丢失了页码与坐标**。
CLAUDE.md §0 要求「输出中出现的任意数值，要么绑定 `[block_id | page]`，
要么显式标注为推断」——从 Markdown 切出来的块拿不到 `page`，这条硬约束就无法满足。

所以两者分工固定：

| 产物 | 角色 | 谁消费 |
|---|---|---|
| `result.detail[]` | 切块的**唯一**输入 | 切块器（`parse/textin.py` → `parse/chunker.py`） |
| `result.markdown` | 人读的档案 | 人工核对、跨版本 diff |
| 完整响应 JSON | **重切块的数据源** | 改切块参数时从这里重切，不重新调用计费 API |

### 2.4 解析产物落盘

每次成功解析写两个对象存储文件，key 由「原始字节哈希 + 参数指纹」决定：

```
parse/textin/<content_hash>/<param_fp>.json     # 完整响应，重切块的数据源
parse/textin/<content_hash>/<param_fp>.md       # result.markdown，人读档案
```

对应 `core.document` 的 `parse_json_ref` / `parse_md_ref` 两列
（迁移 `007_parse_artifacts.sql`）。**不入 jsonb 列**：一份 500 页年报的响应可达
数十 MB，TOAST 之后 `pg_dump` 会被拖垮，而这两样东西从来不参与关系查询。
这与 `provider_snapshot.response_ref` 是同一套做法（`02-data-model.md` §4）。

key 的构成方式让它同时承担三个职责，这是这段设计里唯一值得记住的地方：

1. **成本闸门**。xParse 按页计费，首次全量入库是数十万页。调用前先查
   `parse_json_ref` 对应的对象是否存在，命中就不调 API。
   同一份文档在同一套参数下永远只解析一次。
2. **重切块的数据源**。改切块长度、改过滤规则要重跑全量——从 JSON 重切，零 API 成本。
3. **契约测试的录制-回放夹具**。落盘的 JSON 就是 `04-ingestion.md` §6 要的录制结果，
   不需要另建一套夹具机制。脱敏在写盘前做：响应里不含密钥，但请求参数要剥离认证头。

改参数时 `param_fp` 变化，于是这是一个**新的缓存条目**，旧产物保留——
这正好对上 §7.2「重新解析是新增版本，不是原地替换」。

### 2.5 `detail[]` → `NormalizedBlock`

```python
# packages/ragdemo/src/ragdemo/parse/textin.py
@dataclass(frozen=True)
class ParseResult:
    blocks: list[NormalizedBlock]
    page_count: int
    engine_version: str        # textin:<result.version>+<param_fp>
    markdown: str
    raw_json: bytes
    warnings: list[str]

class DocumentParser(Protocol):
    def parse(self, file_bytes: bytes, *, lang: str = "zh") -> ParseResult: ...
```

**块类型**（`core.block_type` 四值，`02-data-model.md` §2）：

| `detail[].type` | `outline_level` | 我们的 `block_type` |
|---|---|---|
| `paragraph` | `>= 0` | `title` |
| `paragraph` | `-1` | `paragraph` |
| `table` | 任意 | `table` |
| `image` | 任意 | `figure` |

`outline_level` 是**供应商给的确定值**，不是我们推断的：`-1` 表示正文，
`0` 及以上是标题层级。这一条消掉了原 MinerU 方案里整套「判断这行是不是标题」的启发式。

**`section_path`** 用一个按 `outline_level` 索引的标题栈维护：
遇到层级 L 的标题就把栈截断到 L 再压入，非标题块取当前栈的
`' > '.join(...)`，得到 `'第三节 主营业务 > 3.2 分部收入'`。

**表格块的 `content`** 从 `detail[].cells[]` 拼 Markdown 表格，
`row_span` / `col_span` 覆盖到的每个格子都填上重复值：

```
cells: [{row:0, col:0, row_span:1, col_span:2, text:"2024H1"}, ...]
  ↓ 展开合并单元格
| 2024H1 | 2024H1 |
```

重复而不是留空，是因为 BM25 与嵌入都按块的整体文本工作，
留空的格子会让「智能计算 2024H1 收入」这类查询在跨列表头上匹配不到。
第一行作表头。

**`page`**：取 `detail[].page_id`。供应商的 `page_id` 是否从 0 起算需要在
首次接入时用真实响应确认——`06` 元数据校验第 6 条要求 `page ∈ [1, page_count]`，
差一就会让整份文档回滚。封装里做防御式归一：若整份文档出现 `page_id == 0`，
则整体按 0 基处理统一加一。**首次接入时把实测结论回写本节。**

**`bbox`**：`position` 是四角八个整数，转成轴对齐的 `[x0,y0,x1,y1]`
再用页面宽高归一化到 `[0,1]`，这样换 `dpi` 不会让存量 bbox 失效。
页面宽高取响应顶层的 `metrics[]`（`page_image_width` / `page_image_height`）；
取不到就写 `None`——`bbox` 可空，P1 没有依赖它的功能，
为了它让整份文档失败不值得。

### 2.6 错误码处理

供应商的错误码决定这份文档是「以后重试」还是「永远别再试」，
分错了要么无限重试烧钱，要么静默丢文档。

| 错误码 | 含义 | 处理 |
|---|---|---|
| `40303` / `40301` / `40425` | 文件类型不支持 | **永久跳过**，`parse_engine='textin:unsupported'`，不重试 |
| `40302` | 超过 500MB | **永久跳过**，`textin:too_large` |
| `40422` | 文件损坏 | **永久跳过**，`textin:corrupt` |
| `40423` | PDF 需要密码 | **永久跳过**，`textin:encrypted`，告警要人处理 |
| `40424` | 页码超出范围 | 参数错误，**失败整个资产**（是我们的 bug，不是数据问题） |
| `40004` / `40427` | 参数错误 | 同上，**失败整个资产** |
| `40101` / `40102` / `40103` | 认证失败 | **失败整个资产**并告警。代理配置问题，重试没有意义 |
| `40003` | 账户余额不足 | **立刻失败整个资产**并告警。这条单独列出来是因为重试会在没钱的时候把分区反复跑满 |
| `50207` | 部分页面解析失败 | **成功**，但把失败页写进 `parse_warnings` |
| `30203` / `500` | 服务故障 | **退避重试**（指数退避，上限 3 次） |
| 超时 | — | `textin:skipped` + 告警，**不阻塞分区** |

「永久跳过」的文档仍然写 `document` 行——只是没有块。
否则下一次分区重跑会再拉一遍、再失败一遍。
`parse_engine` 里的标记就是「别再试了」的记号。

`parse_warnings` 落在 `document.parse_warnings`（jsonb）。消费方是检索降权与
抽取置信度降档（`07-agents.md` §5）：表格结构不确定的块，Agent 从中取数时置信度降一档。

### 2.7 成本闸门与私有材料闸门

**成本**：除了 §2.4 的缓存，每次 Dagster run 有页数预算上限
（`TEXTIN_MAX_PAGES_PER_RUN`）。超预算停止并告警，不静默烧钱。
月度解析支出占预算比例是 adr/0008 的推翻条件之一，要持续记录。

**私有材料**：这是换到托管 API 后新增的风险，MinerU 时代不存在。
TextIn 是境内服务，满足 CLAUDE.md §0 的「数据境内存储」；
但「境内」不等于「什么都可以发」。

```python
if document.owner_user is not None and not settings.textin_allow_private:
    raise PrivateDocumentEgressBlocked(...)
```

`TEXTIN_ALLOW_PRIVATE` 默认 `false`。P4 上线用户上传前，
必须先有签署的数据处理协议 + 明确的用户告知，或者为私有材料保留一条本地解析路径。
把它写成代码里的闸门而不是文档里的提醒，是因为提醒会被忘记，
而 `test_private_document_is_not_sent_upstream` 不会。

## 3. 切块规范

### 3.1 基本规则

| 规则 | 值 | 理由 |
|---|---|---|
| 切分边界 | 章节 / 小节 / 表格边界优先，其次段落 | 跨小节的块会混入不相关内容，稀释 BM25 与向量信号 |
| 叶子块长度 | **200–400 字**（中文字符），可配置 | 短于 200 会切断「数字 + 归因」的因果链；长于 400 向量信号被稀释。最终值由检索评测集实测确定，见 `ChunkConfig` |
| 重叠 | 12%（约 48 字） | 防止关键句被切在边界上 |
| 表格 | **单独成块，不与正文合并，不因超长而切分** | 切开的表格无法理解 |
| 块首 | 拼接 `section_path` | 让块自带上下文，对 BM25 与嵌入都有明显增益 |

### 3.2 表格块

表格块的 `content` 用 Markdown 表格文本，`content_desc` 存一句自然语言描述：

```
content:
| 业务分部 | 2024H1 收入(百万元) | 同比 |
|---|---|---|
| 智能计算 | 12,340 | +58.2% |
| 通信设备 | 8,120  | -3.1%  |

content_desc:
2024 年上半年分部收入表，含智能计算、通信设备等 5 个分部的收入与同比增速。
```

`content_desc` 由小模型生成，生成 prompt 版本化存放，**内容必须只描述表格里有什么，
不做任何解读或数值推断**。它的作用是让「分部收入」这类查询能在语义上命中表格块——
纯数字表格的向量表示很弱，描述句是主要的召回来源。

`content_desc` 是 `doc_block` 的 BM25 索引字段之一（`02-data-model.md` §5.5），
也参与嵌入（§5.1）。

### 3.3 不切分的内容

以下内容不进入检索索引，但保留在文档中：

| 要过滤的 | 依据 | 性质 |
|---|---|---|
| 页眉页脚、页码 | `detail[].content == 1` | **供应商标注**，确定值 |
| 目录页 | `catalog.toc` 覆盖的页 + `outline_level` 全为标题的连续页 | 供应商标注 + 一条规则 |
| 会计政策模板文本 | 关键词 + 长度 + 位置的启发式 | **启发式**，唯一剩下的 |

前两项在 MinerU 方案里是正则加位置规则，换到 xParse 后变成读一个字段
（[adr/0008](adr/0008-textin-xparse-document-parsing.md)）。
这不只是省代码：**启发式规则会在换一种排版的文档上静默失效**——不报错，
只是少召回或多噪声，而且要等评测掉分才发现。

第三项没法靠供应商解决（各家附注高度雷同是内容问题不是版面问题），
规则写在 `packages/ragdemo/src/ragdemo/parse/filters.py`，配置化、可按 `doc_type` 覆盖。
**过滤规则的变更要跑检索评测**——过滤过度会丢召回。

## 4. 父子块

```
父块（parent_block_id IS NULL）：整个小节，可长达数千字，供生成使用
   └── 子块（parent_block_id = 父块）：200–400 字，供检索使用
```

### 4.1 为什么要拆两层

检索和生成对块长度的要求相反：检索要短（信号集中、向量表示准确），
生成要长（上下文完整、数字不被切断）。父子块让两者各取所需。

> **长度的两层含义**：父块是整个小节（不限长，只供生成），叶子块是 200–400 字
> （只供召回）。早期版本的本文档在这两处写了互斥的数字（300–800 与 ≤200），
> 已按 `docs/superpowers/plans/2026-09-21-p1b-document-pipeline.md` 的裁定统一。

### 4.2 构造规则

1. 按 `section_path` 分组，同一小节的内容构成一个父块；
2. 父块内按 §3.1 的 `ChunkConfig` 切出 200–400 字的叶子块，`parent_block_id` 指向父块；
3. **表格块既是父块也是叶子块**（`parent_block_id IS NULL` 且无子块）——
   表格不能切，也不能只给片段；
4. 父块**不做嵌入**（`embedding IS NULL`），检索时被 `is_leaf = false` 排除——
   只有叶子块参与召回，父块只在展开阶段被取出。

因为规则 3 的存在，「是否可被召回」不能用 `parent_block_id IS NOT NULL` 判断
（表格块的 `parent_block_id` 是 NULL 但它必须可被召回）。用显式的
`doc_block.is_leaf` 列：有子块的块 `is_leaf = false`，其余为 `true`。
该列在写入时由切块器计算，是 BM25 索引字段之一（`02-data-model.md` §5.5）。

### 4.3 展开

检索命中子块后返回其父块内容用于生成，但**引用标注用子块的 `block_id`**——
溯源要精确到句子级，不能只指到小节。展开逻辑见 `06-retrieval.md` §6。

去重：同一父块下多个子块同时命中时，父块只返回一次，但记录全部命中的子块 id。

## 5. 嵌入

### 5.1 嵌入输入

嵌入的不是裸 `content`，而是拼接后的文本：

```python
def embedding_input(block: DocBlock, doc: Document) -> str:
    parts = [
        f"{doc.title}",
        f"{block.section_path}" if block.section_path else "",
        block.content_desc or "",
        block.content,
    ]
    return "\n".join(p for p in parts if p)[:MAX_EMBED_CHARS]
```

标题与章节路径提供了块本身缺失的上下文（「本期」「上述」这类指代在孤立块中无法解析）。
`MAX_EMBED_CHARS` 按模型的上下文上限设定，超长时截断 `content` 而非丢弃前面的上下文。

### 5.2 归一化与维度

- 维度固定 **1024**（[adr/0004](adr/0004-hosted-embedding-reranker-api-1024d.md)）；
- 写入前做 **L2 归一化**——`02-data-model.md` §5.4 的 HNSW 索引用 `vector_cosine_ops`，
  归一化后余弦距离与内积等价，查询端也必须归一化；
- 模型标识存在 `document.parse_engine` 之外的单独字段不必要，但**更换嵌入模型必须
  全量重建索引**，见 §7。

### 5.3 批处理与缓存

托管 API 有速率限制，而 P1 首次入库有数十万块。策略：

- **批量调用**：每批 32–64 个块，并发 4；
- **内容哈希缓存**：`sha256(embedding_input)` → 向量，存独立缓存表。
  相同内容（各公司公告中大量重复的模板段落）只算一次；
- **断点续传**：嵌入是独立的 Dagster 资产，`embedding IS NULL` 的块构成待办队列，
  失败重跑只处理剩余部分；
- **限速退避**：遇 `429` 按 `Retry-After` 退避，不放大并发。

```sql
CREATE TABLE core.embedding_cache (
  content_hash text NOT NULL,
  model        text NOT NULL,
  owner_user   text NOT NULL DEFAULT '',   -- '' = 公共内容；其余为用户私有分区
  embedding    vector(1024) NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (content_hash, model, owner_user)
);
```

主键三列缺一不可：

- 少了 `model`，更换嵌入模型后会读到旧模型的向量。这种错误不报错，
  只会让检索质量莫名下降。
- 少了 `owner_user`，用户私有文档的向量会进入共享缓存，
  理论上可通过哈希命中探测「某段文本是否被别人上传过」
  （`09-compliance-security.md` §3.3）。用 `''` 而非 `NULL` 表示公共内容，
  因为主键列不允许 NULL。

## 6. 元数据完整性校验

块入库前必须通过校验，任何一条不满足则整份文档回滚：

1. `ordinal` 从 0 开始连续，无重复无空缺；
2. `content` 非空且去除空白后长度 > 0；
3. `block_type = 'table'` 的块必须有 `content_desc`；
4. `parent_block_id` 指向的块属于同一 `doc_id`，且父块自身 `parent_block_id IS NULL`
   （只允许两层，不允许多级嵌套）；
5. 反规范化列（`entity_id` / `doc_type` / `publish_at` / `owner_*` / `known_at`）
   与所属 `document` 完全一致（`02-data-model.md` §5.3）；
6. `page` 若非空则在 `[1, document.page_count]` 内；
7. `is_leaf` 与实际父子关系一致：`is_leaf = false` 的块必须至少有一个子块，
   `is_leaf = true` 的块必须没有子块；
8. 每个 `is_leaf = true` 的块最终都要有 `embedding`（嵌入是独立资产，
   此项在嵌入资产完成后校验，不阻塞入库）。

整份文档回滚而非跳过坏块，是因为部分入库的文档会让检索返回残缺证据，
比没有这份文档更危险。

## 7. 幂等与重建

### 7.1 重复入库

`document` 上的 `UNIQUE (source, content_hash)`（`02-data-model.md` §5.1）
让同一份文档的重复拉取直接冲突，管线捕获后跳过。

### 7.2 重新解析

升级解析器版本或改 §2.2 的调用参数、更换公告供应商、修改切块参数，
都会导致需要重新解析已有文档。
流程是**新增版本，不是原地替换**：

1. 新建 `document` 行，`version_group_id` 沿用原值，`supersedes_doc_id` 指向旧行；
2. 新文档的 `known_at` 沿用**原文档的 `known_at`**——重新解析不改变这份文档
   在现实中何时可知；
3. 给旧 `document` 与其全部 `doc_block` 打 `superseded_at = now()`；
4. 旧块行保留，历史观点的引用不断。

第 2 步是容易做错的地方：如果新行的 `known_at` 取重新解析的时刻，
这份文档在历史回测中会凭空消失。

### 7.3 更换嵌入模型

需要全量重算 `doc_block.embedding`。流程：

1. 新增一列 `embedding_v2 vector(1024)`，在新列上建 HNSW 索引；
2. 后台批量回填（受预算限制，可能持续数天）；
3. 回填完成、双跑评测确认新模型指标不低于旧模型后，切换检索查询；
4. 观察一周，稳定后删除旧列与旧索引。

**不允许原地覆盖 `embedding`**：回填过程中新旧向量混在一列里，
检索结果会在几天内处于不可解释的中间状态，评测数字也失去意义。

## 8. 用户上传（P4）

用户上传的材料走路径 B，但有额外约束：

- `document.owner_user` 必填，`owner_tenant` 按用户所属租户填；
- **外送闸门**：`TEXTIN_ALLOW_PRIVATE` 默认 `false`，解析器拒绝把
  `owner_user` 非空的文档发给 TextIn（§2.7）。P4 上线前必须先解决
  数据处理协议与用户告知，或为私有材料保留本地解析路径；
- 解析任务在**隔离的队列**中进行，不与公共管线共用并发额度与重试预算；
- 原始文件存对象存储的独立 bucket，不与公共文档混放；
- 块进入同一张 `doc_block` 表，隔离靠 `owner_*` 列 + 行级安全策略实现
  （`09-compliance-security.md` §3）——不建独立表，否则检索需要跨表 UNION，
  时点过滤与索引下推会双双失效。
