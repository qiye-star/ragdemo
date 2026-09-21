# 06 · 检索管线

## 1. 管线总览

```
问题 + as_of + 过滤条件
   │
   ├─(可选) 查询改写：同义词扩展 + 大模型变体            §7
   │
   ▼
阶段 1  元数据过滤：entity / doc_type / known_at ≤ as_of  §2
   │
   ├────────────────┬────────────────┐
   ▼                ▼                │
BM25 top-50     向量 top-50           │  两路并行，各自在索引内完成过滤  §3
   └────────┬───────┘                │
            ▼                        │
      加权 RRF 融合 → top-50          │  §4
            ▼                        │
   bge-reranker-v2-m3 → top-10       │  §5
            ▼                        │
      父子块展开                      │  §6
            ▼                        ▼
    证据集：[{block_id, parent_content, page, doc_id, known_at, score}]
```

设计原则（简报 §2.7「实体优先检索」）：**先过滤、再检索、最后重排**。
先把候选集缩到正确的实体与时点范围内，再在其中做相关性排序——反过来做会让
「寒武纪 2024 年三季报」这类查询召回一堆其他公司的相似段落。

## 2. 阶段 1：元数据过滤

必选过滤条件：

| 条件 | 来源 | 说明 |
|---|---|---|
| `known_at <= as_of` | 强制 | 无默认值，见 `03-point-in-time.md` §2.1 |
| `superseded_at IS NULL OR superseded_at > as_of` | 强制 | 同上 |
| `is_leaf = true` | 强制 | 只召回叶子块；父块只在展开阶段出现，见 §6 |
| `owner_tenant` / `owner_user` | 强制 | 权限边界，见 `09-compliance-security.md` §3 |
| `entity_id = ANY(:entity_ids)` | 可选 | 由路由 Agent 或用户指定 |
| `doc_type = ANY(:doc_types)` | 可选 | 如只查公告不查新闻 |
| `publish_at BETWEEN ...` | 可选 | 时间窗口查询 |

## 3. 时点过滤与索引的交互（简报未涉及的关键问题）

### 3.1 问题

HNSW 与 BM25 都是**近似**索引：它们先按相似度/相关度找出候选，再由执行器套用
`WHERE` 条件。朴素写法

```sql
SELECT block_id FROM core.doc_block
 WHERE known_at <= :as_of AND entity_id = 'CN.688256'
 ORDER BY embedding <=> :qvec LIMIT 50;
```

在最坏情况下会这样执行：HNSW 返回全库最相似的若干块 → 过滤掉不属于该公司、
或 `known_at` 晚于 `as_of` 的 → **剩下 3 条**。查询不报错，只是召回率崩了。
回测场景尤其严重：`as_of` 越早，被过滤掉的比例越高，越早的时点召回越差——
而这恰好是我们最需要准确的场景。

### 3.2 BM25 一路的解法：字段进索引

`02-data-model.md` §5.5 把 `entity_id`、`doc_type`、`known_at`、`superseded_at`、
`is_leaf` 都放进了 BM25 索引并标记 `fast`。ParadeDB 能把这些谓词下推到 tantivy 内部，
过滤在打分之前完成，`LIMIT 50` 拿到的就是过滤后的前 50 条。

为确保下推真实发生（而不是退化成回表过滤），可以用显式的查询构造器：

```sql
SELECT block_id, paradedb.score(block_id) AS bm25_score
FROM core.doc_block
WHERE block_id @@@ paradedb.boolean(must => ARRAY[
        paradedb.parse(:query_text),
        paradedb.term('is_leaf', true),
        paradedb.term('entity_id', :entity_id),
        paradedb.range('known_at', tstzrange(NULL, :as_of, '(]'))
      ])
ORDER BY bm25_score DESC
LIMIT 50;
```

**默认写法仍用普通 SQL 谓词**（§4.2），构造器只在实测发现下推未生效时才启用——
它更冗长、更绑定 ParadeDB 版本，没有必要提前付这个成本。

**验证方式**：`EXPLAIN (ANALYZE, VERBOSE)` 检查过滤条件出现在索引扫描节点内部，
而不是上层的 `Filter`。这是 P1 的验收项之一。

> **实测记录（PostgreSQL 18.6 / `pg_search` 0.25.9）**：上述构造器全部可用，
> 但踩到一个陷阱——`paradedb.term('entity_id', 'CN.688256')` 匹配 0 行，
> 因为 `raw` 分词器**默认小写化** token。`02-data-model.md` §5.5 已把
> `entity_id` / `doc_type` 改为 `keyword` 分词器（精确、大小写敏感）修复此问题。
>
> **注意这类错误不会报错，只会静默返回空结果集。** 启用构造器写法时，
> 必须为每个 `term` 条件写一条断言它确实返回预期行数的测试，
> 不能只看「查询没报错」。普通 SQL 谓词路径（§4.2）不受该问题影响。

### 3.3 向量一路的解法：迭代扫描

pgvector 0.8 起支持迭代索引扫描：当过滤后候选不足时自动继续扫描索引，
直到凑够 `LIMIT` 或达到上限。

```sql
SET LOCAL hnsw.ef_search       = 200;              -- 默认 40，提高召回
SET LOCAL hnsw.iterative_scan  = 'relaxed_order';  -- 候选不足时继续扫
SET LOCAL hnsw.max_scan_tuples = 200000;           -- 扫描上限，防止退化成全表
```

`relaxed_order` 允许结果不严格按距离排序，换取显著更好的过滤召回。
本管线后面还有 RRF 融合与重排，对严格顺序不敏感，因此 `relaxed_order` 是正确选择。

### 3.4 安全视图与下推的冲突（必须同时满足两者）

`03-point-in-time.md` §4 的 `asof.doc_block` 视图把时点条件写成
`known_at <= asof.current_as_of()`。`current_as_of()` 是一个 PL/pgSQL 的 `STABLE`
函数——它对正确性是好事（无法遗漏），但对性能是坏事：**函数调用很可能阻止
优化器把该谓词下推进 tantivy 索引扫描或 HNSW 的过滤**，于是 §3.2、§3.3
的全部努力落空。

解法是**冗余谓词**：查询走 `asof.doc_block` 视图（保证正确性），
**同时在 SQL 里再写一遍显式的 `known_at <= :as_of` 字面参数**（保证可下推）。

```sql
FROM asof.doc_block            -- 视图：正确性兜底，写漏也不会泄漏
WHERE content @@@ :query_text
  AND known_at <= :as_of       -- 冗余但必要：字面参数才能下推进索引
  AND (superseded_at IS NULL OR superseded_at > :as_of)
  ...
```

两个条件逻辑上等价（`:as_of` 与 `app.as_of` 由 `as_of_session` 保证是同一个值），
因此冗余不改变结果集，只影响执行计划。这与 `09-compliance-security.md` §3.2
中「检索 SQL 里写 owner 条件 + RLS 兜底」是同一个模式：
**显式条件负责跑得快，声明式机制负责不出错。**

一致性由 `as_of_session` 保证——它是唯一设置 `app.as_of` 的地方，
同时也是唯一向查询注入 `:as_of` 参数的地方，两者不可能不一致。

> P0 执行 DDL 时一并用 `EXPLAIN (ANALYZE, VERBOSE)` 验证：加冗余谓词前后，
> 过滤条件是否从上层 `Filter` 移进了索引扫描节点。若视图本身就不阻碍下推
> （不同 PostgreSQL / ParadeDB 版本行为可能不同），冗余谓词无害，保留即可。

### 3.5 兜底：按年份的部分索引

如果回测频繁使用固定的历史时点，且 §3.3 仍不够，可以按 `known_at` 年份建部分索引：

```sql
CREATE INDEX doc_block_emb_2024 ON core.doc_block
  USING hnsw (embedding vector_cosine_ops)
  WHERE known_at < '2025-01-01' AND is_leaf;
```

代价是索引数量与磁盘占用随年份线性增长。**这是优化手段，不是默认方案**——
只在 §3.3 被实测证明不足时才引入，并在 ADR 中记录。

### 3.6 召回率验证

不能假设上述手段有效，必须测。验证方法：

1. 从 `eval_retrieval` 取一批带 `as_of` 的问题；
2. 对每个问题跑两次：一次用索引路径，一次强制全表精确扫描
   （`SET LOCAL enable_indexscan = off` + 暴力计算距离）；
3. 计算索引路径相对精确路径的召回率；
4. **阈值：≥ 0.95**。低于此值说明过滤下推没生效或参数不足。

这个测试跑得慢（精确扫描是全表），因此只在 CI 的 nightly 任务中跑，不进 PR 门禁。

## 4. 混合检索与加权 RRF

### 4.1 对简报的修正

简报 §7 写「BM25 权重 0.6，向量 0.4（可配置），RRF 合并取前 50」。
这是两种互斥的融合方式：

- **加权线性融合**用分数。但 BM25 分数无上界且随语料变化，向量余弦相似度在 [-1,1]，
  两者不可直接加权——必须先做分数归一化，而归一化本身对异常值敏感、不稳定。
- **RRF（Reciprocal Rank Fusion）**只用排名，天然无量纲，对分数尺度不敏感。

统一为**加权 RRF**：权重作用在 RRF 项上，既保留「BM25 更重要」的意图，
又避免分数尺度问题。

```
score(d) = w_bm25 / (k + rank_bm25(d)) + w_vec / (k + rank_vec(d))

默认 w_bm25 = 0.6, w_vec = 0.4, k = 60
某一路未召回该文档时，该项贡献 0
```

`k = 60` 是 RRF 的经验默认值，可配置。**权重与 k 的调整必须跑检索评测**。

### 4.2 完整 SQL

> **P0 实测（`paradedb/paradedb:0.25.9-pg18`）：下面这段按原文跑不起来，三处要注意。**
> 逐条钉在 `tests/db/test_retrieval_sql.py` 里，改动本节必须同步那份测试。
>
> 1. **`owner_tenant IS NOT DISTINCT FROM :tenant` 与 `paradedb.score()` 不能共存。**
>    同时出现时 ParadeDB 直接报 `Unsupported query shape`。单独用
>    `IS NOT DISTINCT FROM`（不取 score）是可以的，所以只读文档发现不了。
>    可用的等价写法：
>    `(owner_tenant = :tenant OR (owner_tenant IS NULL AND :tenant::text IS NULL))`。
>    下面已按这个写法修正。
> 2. **`SET LOCAL app.as_of = :as_of` 不能参数化。** `SET` 不接受占位符，
>    参数化执行必然语法错。用 `SELECT set_config('app.as_of', :as_of, true)`，
>    `ragdemo_core.db.session.as_of_session` 就是这么做的。下面已改。
> 3. **公共读取时 `:tenant` / `:user` 必须绑 SQL `NULL`，不能绑空串。**
>    而 `as_of_session` 把 `app.tenant` / `app.user` 设成空串（GUC 不能存 NULL）。
>    两边约定不一致时检索会**一条公共文档都召不回，且不报任何错**。
>    `RetrievalService` 必须在一个地方统一这两处，并有针对性的单测。

```sql
-- 会话参数。注意 as_of 走 set_config 而不是 SET LOCAL —— SET 不接受占位符。
SELECT set_config('app.as_of', :as_of, true);
SET LOCAL hnsw.ef_search       = 200;
SET LOCAL hnsw.iterative_scan  = 'relaxed_order';
SET LOCAL hnsw.max_scan_tuples = 200000;

WITH bm25 AS (
  SELECT block_id,
         ROW_NUMBER() OVER (ORDER BY paradedb.score(block_id) DESC) AS rnk
    FROM asof.doc_block
   WHERE content @@@ :query_text
     AND known_at <= :as_of                  -- 冗余谓词，见 §3.4
     AND (superseded_at IS NULL OR superseded_at > :as_of)
     AND is_leaf
     AND (:entity_ids::text[] IS NULL OR entity_id = ANY (:entity_ids))
     AND (:doc_types::text[]  IS NULL OR doc_type  = ANY (:doc_types))
     -- 展开成 OR 形式：IS NOT DISTINCT FROM 与 paradedb.score() 共存会被拒，见本节开头
     AND (owner_tenant = :tenant OR (owner_tenant IS NULL AND :tenant::text IS NULL))
     AND (owner_user   = :user   OR (owner_user   IS NULL AND :user::text   IS NULL))
   ORDER BY paradedb.score(block_id) DESC
   LIMIT :candidate_k                       -- 默认 50
),
vec AS (
  SELECT block_id,
         ROW_NUMBER() OVER (ORDER BY embedding <=> :qvec) AS rnk
    FROM asof.doc_block
   WHERE embedding IS NOT NULL
     AND known_at <= :as_of                  -- 冗余谓词，见 §3.4
     AND (superseded_at IS NULL OR superseded_at > :as_of)
     AND is_leaf
     AND (:entity_ids::text[] IS NULL OR entity_id = ANY (:entity_ids))
     AND (:doc_types::text[]  IS NULL OR doc_type  = ANY (:doc_types))
     AND (owner_tenant = :tenant OR (owner_tenant IS NULL AND :tenant::text IS NULL))
     AND (owner_user   = :user   OR (owner_user   IS NULL AND :user::text   IS NULL))
   ORDER BY embedding <=> :qvec
   LIMIT :candidate_k
)
SELECT block_id,
       COALESCE(:w_bm25 / (:rrf_k + bm25.rnk), 0)
     + COALESCE(:w_vec  / (:rrf_k + vec.rnk),  0) AS rrf_score
  FROM bm25 FULL OUTER JOIN vec USING (block_id)
 ORDER BY rrf_score DESC
 LIMIT :fusion_k;                            -- 默认 50
```

注意查询走的是 `asof.doc_block` 视图而非基表——时点过滤由视图保证，
调用方无法遗漏（`03-point-in-time.md` §4）。

`owner_tenant IS NOT DISTINCT FROM :tenant` 而非 `=`：公共文档的 `owner_tenant` 是 NULL，
用 `=` 会把它们全部过滤掉。

## 5. 重排

```python
class Reranker(Protocol):
    model: str
    def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        """返回 (原索引, 相关度分) 列表，按分数降序，长度 ≤ top_k。"""
```

- 模型：`bge-reranker-v2-m3`，托管 API（[adr/0004](adr/0004-hosted-embedding-reranker-api-1024d.md)）;
- 输入：融合后的 50 条，用**叶子块的 `content`**（不是父块——父块太长会超出重排模型
  上下文，且稀释相关信号）；
- 输出：top-10；
- **超时与降级**：重排 API 超时或失败时，降级为直接使用 RRF 顺序，
  并在返回结果中标记 `reranked=false`。研究场景下"慢"比"没有结果"更不可接受，
  但降级发生率要监控——持续降级说明需要本地部署重排模型。

## 6. 父子块展开

```python
@dataclass(frozen=True)
class EvidenceBlock:
    block_id: int            # 叶子块 id，用于引用标注（精确到句子级）
    parent_block_id: int | None
    content: str             # 父块内容（无父块时为自身内容），供生成使用
    matched_child_ids: list[int]   # 同一父块下全部命中的叶子块
    doc_id: int
    doc_title: str
    section_path: str
    page: int | None
    known_at: datetime
    score: float
    reranked: bool
```

展开规则：

1. 对每个命中的叶子块，取其 `parent_block_id` 对应的父块内容；
2. 父块为 NULL（表格块）时用自身内容；
3. **同一父块去重**：多个子块命中同一父块时只返回一条 `EvidenceBlock`，
   `matched_child_ids` 收集全部命中，`score` 取最高分，`block_id` 取最高分的那个子块；
4. 去重后若不足 `top_k`，**不补位**——宁可少给几条也不引入低相关证据。

引用标注用 `block_id`（叶子块），不是父块 id。理由：溯源要精确到段落级别，
指到整个小节等于没指。

## 7. 查询改写（可开关）

默认**关闭**。开启后：

1. **同义词扩展**：查词表 `config/synonyms.yaml`（如 `算力 ↔ 计算力 ↔ compute`、
   `CoWoS ↔ 先进封装`），扩展 BM25 查询；
2. **大模型变体**：小模型生成 2 个查询变体，三个查询各自召回后一起进 RRF
   （变体权重 0.5 倍）。

开关放在 `RetrievalConfig.rewrite_enabled`，**默认关闭是有意的**：
改写会增加延迟与成本，而它是否提升召回必须由评测证明，不能想当然。
`eval_retrieval` 上开关两次跑对比，指标提升超过 2 个百分点才值得默认开启。

## 8. 服务接口

```python
# src/retrieval/service.py
@dataclass(frozen=True)
class RetrievalConfig:
    candidate_k: int = 50
    fusion_k: int = 50
    top_k: int = 10
    w_bm25: float = 0.6
    w_vec: float = 0.4
    rrf_k: int = 60
    ef_search: int = 200
    rewrite_enabled: bool = False
    rerank_enabled: bool = True

@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    as_of: datetime                      # 必填，无默认值
    entity_ids: list[str] | None = None
    doc_types: list[str] | None = None
    published_after: datetime | None = None
    tenant: str | None = None
    user: str | None = None
    config: RetrievalConfig = RetrievalConfig()

@dataclass(frozen=True)
class RetrievalResult:
    blocks: list[EvidenceBlock]
    stats: RetrievalStats                # 各阶段耗时、候选数、是否降级

class RetrievalService(Protocol):
    def search(self, req: RetrievalRequest) -> RetrievalResult: ...
```

`RetrievalStats` 必须包含 `bm25_hits` / `vec_hits` / `after_fusion` / `after_rerank`
与各阶段耗时。这些数字是排查「为什么没找到」的唯一依据，**每次查询都记录到
结构化日志**，不是只在 debug 模式下记。

## 9. 评测

### 9.1 指标

| 指标 | 定义 | P1 阈值 |
|---|---|---|
| `recall@10` | top-10 中包含至少一个 `gold_block_ids` 的问题占比 | **> 0.80** |
| `recall@50` | 融合后 top-50 的同样指标（衡量召回天花板） | > 0.92 |
| `MRR@10` | 第一个正确块排名倒数的平均值 | 记录，无硬阈值 |
| `index_recall` | §3.6 的索引召回率 | **≥ 0.95** |
| `p95_latency` | 端到端 95 分位延迟 | < 3s |

`recall@50` 与 `recall@10` 的差距告诉你问题出在哪：差距大说明**重排不好**，
两者都低说明**召回不好**。分开看才能定位。

### 9.2 评测集

`eval_retrieval` 表（`02-data-model.md` §7），P1 录入 100 条。构成要求：

- 覆盖至少 20 家实体、5 种 `doc_type`；
- 每条必须带 `as_of`，且其中 **≥ 30 条的 `as_of` 是历史时点**（不是"现在"）——
  这些条目专门检验时点过滤后的召回是否塌陷；
- 按 `difficulty` 分 easy / medium / hard，报告分档给出指标；
- `gold_block_ids` 由创始人标注，可多个（同一事实出现在多处都算对）。

### 9.3 CI 集成

- **PR 门禁**：全量 `eval_retrieval` 跑一遍，`recall@10` 不得低于主干基线 2 个百分点。
  数字贴进 PR 描述（`CLAUDE.md` §1.4）。
- **Nightly**：额外跑 §3.6 的索引召回率验证与 p95 延迟。
- 每次运行写一行 `evals.eval_run`，含 `git_sha` 与完整 `config`，指标可追溯回参数。
