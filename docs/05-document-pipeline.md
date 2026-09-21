# 05 · 文档解析与切块管线

## 1. 两条解析路径

```
                  ┌─ 路径 A：供应商结构化接口（A 股 / 港股公告，首选）
文档来源 ──────────┤
                  └─ 路径 B：MinerU 解析（EDGAR、IR 页 PDF、用户上传、供应商缺失时兜底）
                                      │
                        两条路径都产出 NormalizedDocument
                                      │
                                      ▼
                        切块 → 父子块 → 元数据 → 嵌入 → 入库
```

**优先级**：同一份文档若两条路径都可得，用路径 A。原因是供应商已经做过表格结构还原与
章节识别，`section_path` 与表格准确率显著高于通用解析，而这两项直接决定检索质量。

路径 B 产出的文档在 `document.parse_engine` 中记为 `mineru:<version>`，
检索评测时按 `parse_engine` 分组统计——两条路径的召回率差距是判断供应商是否值钱的依据。

## 2. MinerU 封装

```python
# src/parse/mineru.py
@dataclass(frozen=True)
class ParseResult:
    blocks: list[NormalizedBlock]
    page_count: int
    engine_version: str
    warnings: list[str]        # 如 "page 12: table structure uncertain"

class DocumentParser(Protocol):
    def parse(self, pdf_bytes: bytes, *, lang: str = "zh") -> ParseResult: ...
```

封装要点：

- **在独立容器中运行**，通过队列调用。MinerU 内存占用大且偶发崩溃，跑在主进程里
  会拖垮整个管线。
- **超时与页数上限**：单文档 10 分钟、500 页。超限的文档记入
  `document.parse_engine = 'mineru:skipped'` 并告警，不阻塞分区。
- `warnings` 必须保留。表格结构不确定的块在检索中降权，且抽取 Agent 从这些块
  取数时置信度降一档（`07-agents.md` §5）。
- 版本固定并记录。MinerU 升级会改变切块结果，必须能区分「哪些块是哪个版本解析的」，
  否则评测集的 `gold_block_ids` 会莫名失效。

## 3. 切块规范

### 3.1 基本规则

| 规则 | 值 | 理由 |
|---|---|---|
| 切分边界 | 章节 / 小节 / 表格边界优先，其次段落 | 跨小节的块会混入不相关内容，稀释 BM25 与向量信号 |
| 叶子块长度 | 300–800 字（中文字符） | 短于 300 上下文不足，长于 800 重排模型精度下降 |
| 重叠 | 10–15% | 防止关键句被切在边界上 |
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

- 纯格式化的页眉页脚、页码；
- 财务报表附注中的会计政策模板文本（各家高度雷同，是检索噪声的主要来源）；
- 目录页。

识别规则写在 `src/parse/filters.py`，配置化、可按 `doc_type` 覆盖。
**过滤规则的变更要跑检索评测**——过滤过度会丢召回。

## 4. 父子块

```
父块（parent_block_id IS NULL）：整个小节，可长达数千字，供生成使用
   └── 子块（parent_block_id = 父块）：≤ 200 字，供检索使用
```

### 4.1 为什么要拆两层

检索和生成对块长度的要求相反：检索要短（信号集中、向量表示准确），
生成要长（上下文完整、数字不被切断）。父子块让两者各取所需。

### 4.2 构造规则

1. 按 `section_path` 分组，同一小节的内容构成一个父块；
2. 父块内按 §3.1 规则切出 ≤ 200 字的子块，`parent_block_id` 指向父块；
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

升级 MinerU、更换公告供应商、修改切块参数都会导致需要重新解析已有文档。
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
- 解析在**隔离的 worker** 中进行，不与公共管线共用容器实例；
- 原始文件存对象存储的独立 bucket，不与公共文档混放；
- 块进入同一张 `doc_block` 表，隔离靠 `owner_*` 列 + 行级安全策略实现
  （`09-compliance-security.md` §3）——不建独立表，否则检索需要跨表 UNION，
  时点过滤与索引下推会双双失效。
