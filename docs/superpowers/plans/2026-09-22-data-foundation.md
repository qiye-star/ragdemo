# 数据底座建设：对照《数据工程师职责面 v0.1》的差距与实施计划

> **来源**：`数据底座建设方案_数据工程师职责面_v0.1.md`（2026-09-22，待评审）
> **对照物**：本仓库当前 `p0-foundation` 分支 @ `0de9997`
> **写法**：A 做完并通过验收，才开始 B；B 通过才开始 C。每阶段的验收是**可执行命令 + 通过标准**，
> 不是形容词。
>
> 差距结论全部来自读代码与迁移脚本，不是凭印象。每条差距下面都注明了核实位置，
> 复核时直接去那个文件看。

## 实施进度

- [x] **A · 文档管线接进 Dagster DAG** —— `4eadd1c`。验收 A1–A5 全过；额外发现并修复
  两个验收计划里没预料到的真实 bug：`block_embeddings` 与 `doc_blocks_loaded` 之间缺资产
  依赖边（Dagster 并发执行器会先跑 `block_embeddings`）、`embed_pending_blocks` 的并发
  去重洞（补 `FOR UPDATE OF b SKIP LOCKED`）。A1 的资产计数与实际不符（计划写“五个资产”，
  实际是 4 个文档资产 + 2 个事实资产 = 6 个），已按验收意图核实通过，不影响结论。
- [x] **B · 来源登记与四条款** —— `bd7d43b`。验收 B1/B2/B5/B6 全过，B3/B4 由新增
  契约测试覆盖并通过。只登记了 `mock-announcements`（仓库自控的 Mock）；真实来源
  （`cninfo` 等）留给运维在商务确认四条款后手动登记，不替未拍板的来源编造合规状态——
  B1 检出的 1 条"未登记"恰好是并发会话用 `cninfo` 写入的历史文档，这是设计的一部分，
  不是缺陷。额外踩坑并修复：新建的 `core.source_registry` 表需要显式转移属主给
  `app_owner`（`006_asof_views_and_roles.sql` 的属主转移只扫过当时已存在的表）。
- [x] **C · 块级元数据补齐** —— `75e1312`。C1–C8 全过。用真实 Dagster 管线跑出
  一个真 bug 并修复：`score_document()` 的缺页判断曾经是二元的，21/24 页完全没有
  产出任何块的情况被完全吞掉——已改成真实比例并加回归测试。ADR 编号从计划里写的
  0010 改成 0011（并发会话当天已占用 0010）。
- [ ] D · 质量指标底座
- [ ] E · 时点泄漏抽样重放
- [ ] F · 接入对账与新鲜度
- [ ] G · C 档二次解析路由
- [ ] H · 评测集数据侧与真实基线

---

## 零、先说三件对不上的事

方案里有三处与本仓库**已锁定的决策**冲突。不先裁决，后面按方案做会推翻已经跑通并有测试守着的东西。
裁决意见如下，若创始人 / 技术负责人不同意，需要在开工前改这份文件。

### 冲突 1 · 切块长度：300–800 字 vs 200–400 字

方案 §2.3 要求「段落 300–800 字，重叠 80–120 字」，验收线是「块长分布 P5–P95 落在 300–800 字区间」。

本仓库 `ChunkConfig` 是 `leaf_min_chars=200` / `leaf_max_chars=400` / `overlap_ratio=0.12`（约 48 字），
见 `packages/ragdemo/src/ragdemo/parse/config.py`。

这不是实现偷懒。`docs/05-document-pipeline.md:291` 明确记录过这次口径统一：

> **长度的两层含义**：父块是整个小节（不限长，只供生成），叶子块是 200–400 字（只供召回）。
> 早期版本的本文档在这两处写了互斥的数字（300–800 与 ≤200）……

也就是说 **300–800 是被父子块拆分取代的旧数字**。父块承担「300–800 字」原本想要的上下文完整性，
而且不限长；叶子块压到 200–400 是为了向量信号不被稀释。

**裁决**：保留 200–400，方案 §2.3 的验收线改为「叶子块 P5–P95 落在 200–400 字」。
照方案原文验收会 100% 失败——不是质量问题，是标尺对不上。
最终值由阶段 H 的检索评测实测确定（`ChunkConfig` 本来就是为此做成配置而非常量）。

### 冲突 2 · 待拍板决策 2 与 6 已经关闭，且结论与方案的建议不同

| 方案 §5 的待决项 | 方案的建议 | 本仓库实际决策 |
|---|---|---|
| 决策 2：全文检索用 Postgres + zhparser 还是外置 ES | 先用 zhparser，不达标再上 ES | **两个都不是**。ADR-0001 定的是 ParadeDB `pg_search`（tantivy BM25 + `chinese_lindera` 分词），索引已建在 `doc_block` 上 |
| 决策 6：pgvector 继续用还是切独立向量库 | 本阶段锁定 pgvector | ADR-0009：**Chroma 做候选生成器，PG 的 `asof` 视图仍是时点权威**。pgvector 保留为回退路径 |

**裁决**：这两项从「待拍板」移出，方案评审时按已结论汇报即可。若要推翻，按 `CLAUDE.md` §2 的规矩
新增 ADR 并写明推翻条件。

### 冲突 3 · 解析引擎：MinerU 已被 TextIn 取代

方案 §2.2 的三档路由写的是「B 档 MinerU 自解析 / C 档 TextIn 或 Reducto 二次校验」。
ADR-0008 已经用 TextIn xParse 取代 MinerU，B 档现在就是 TextIn。

**裁决**：三档路由的**分档逻辑保留**（这是方案里最有价值的一条：按价值和成本分档，
而不是一个引擎打天下），但引擎对换成：

- A 档 —— 供应商已结构化，零解析
- B 档 —— TextIn xParse 标准参数
- C 档 —— TextIn 高精度参数重跑（表格增强），或第二引擎交叉校验

C 档的存在理由不变：高价值文档值得花第二次钱。详见阶段 G。

---

## 一、现状盘点

下表是方案五个模块逐条对照的结果。**核实位置**列是复核入口。

### 1.1 接入（方案 §2.1）

| 方案要求 | 现状 | 核实位置 |
|---|---|---|
| 统一适配器接口 | ✅ `Adapter` / `FactAdapter` Protocol，业务代码不 import 供应商 SDK | `adapters/base.py` |
| 原始响应永不覆盖 + 对象存储 | ✅ `core.provider_snapshot` + `BlobStore`；>1MB 落对象存储只留 key | `ingest/snapshot.py`、`ragdemo_core/blob.py` |
| 限流令牌桶 + 指数退避 | ✅ `TokenBucket` + `RetryPolicy` + `daily_quota`，配置在 `config/providers.yaml` | `adapters/http.py:70` |
| 幂等与增量 | ⚠️ 去重唯一键是 `(source, content_hash)` 的**部分索引**，缺 `external_id`；**没有 watermark 游标** | `db/migrations/008_document_dedup_partial.sql` |
| `RawRecord` 六字段 | ⚠️ `RawResponse` 缺 `external_id` / `publish_time` / `checksum` | `adapters/base.py:41` |
| `source_registry` 表 + 四条款 | ❌ **仓库里没有这张表，也没有这四个字段中的任何一个** | 全仓库 grep `source_registry` / `can_cache` / `can_show_raw` / `can_vectorize` 零命中 |
| `time_precision` 枚举 | ❌ 没有 | 同上 |
| 对账、接入延迟 P95 | ❌ 没有任何对账或延迟统计代码 | grep `对账` / `reconcil` 只命中向量索引偏移修复，与接入无关 |

### 1.2 解析（方案 §2.2）

| 方案要求 | 现状 | 核实位置 |
|---|---|---|
| 统一 IR：document → section tree → block | ✅ | `parse/tree.py`、`parse/textin.py` |
| 表格单独成块 + 自然语言描述 | ✅ `content_desc`，且是 BM25 索引字段 | `db/migrations/004_documents.sql` |
| 表格**结构化**形态（`table_html`） | ❌ 双形态只做了一半，结构化那一半没有 | grep `table_html` 零命中 |
| `parse_confidence` | ❌ 只有 `parse_warnings` jsonb，没有可比较的数值分 | `db/migrations/007_parse_artifacts.sql` |
| 三档路由 | ⚠️ 只有两条路径（A 供应商结构化 / B xParse），**没有 C 档** | `ingest/assets_docs.py:82` `prepare_documents` |
| 去重与版本链 | ✅ 换了名字：`version_group_id` / `is_correction` / `supersedes_doc_id` | `db/migrations/004_documents.sql` |
| `version_no` 版本序号 | ❌ 没有，只能靠 `known_at` 排序 | 同上 |
| 解析成功率、闭合率等指标 | ❌ 没有 | — |

### 1.3 切块（方案 §2.3）

| 方案要求 | 现状 | 核实位置 |
|---|---|---|
| 表格单独成块 | ✅ `keep_table_whole=True` | `parse/config.py` |
| 块首拼章节路径 | ✅ `section_path`，且进 embedding 输入 | `docs/05-document-pipeline.md:307` |
| 父子块 | ✅ `parent_block_id` + `is_leaf`，向量索引只建叶子块 | `db/migrations/004_documents.sql` |
| 孤儿块检查 | ✅ 整份文档回滚而非跳过坏块 | `parse/validate.py` |
| 块长 300–800 | ⚠️ 见冲突 1，裁决为保留 200–400 | — |
| `chunk_id` 形态 `{doc_id}:{ver}:{seq}` | ⚠️ 实际是 bigint identity `block_id`，唯一性靠 `(doc_id, ordinal)` | `db/migrations/004_documents.sql` |
| `chunking_version` 新旧并存 | ❌ 没有这个列，重切只能靠 `supersedes_doc_id` 整份替换 | grep `chunking_version` 零命中 |

### 1.4 元数据（方案 §2.4）

| 组 | 字段 | 现状 |
|---|---|---|
| A 定位溯源 | `chunk_id` `doc_id` `parent_chunk_id` `page_no` `bbox` `section_path` `chunk_type` | ✅ 全有 |
| A | `raw_uri` | ⚠️ 在 `document.raw_ref`，块上没有（要 JOIN） |
| B 检索过滤 | `doc_type` `publish_time` | ✅ 已反规范化到块上，为的是 BM25 / HNSW 过滤下推 |
| B | `entity_ids`（数组） | ⚠️ 当前是**单值** `entity_id`。改数组会影响 BM25 下推，需要 ADR，见阶段 C |
| B | `report_period` `language` | ⚠️ 只在 `document` 上，块上没有 |
| B | `chunk_char_len` | ❌ 只有 `tokens` |
| C 时点版本 | `valid_from` `known_at` `superseded_at` `supersedes_id` `doc_group_id` | ✅ 全有（后两个换了名） |
| C | `version_no` `chunking_version` `embedding_version` | ❌ 三个都没有 |
| D 权限质量 | `tenant_id` | ✅ 是 `owner_tenant` / `owner_user`，NULL/NULL = 公共空间 |
| D | `visibility` 三值 | ⚠️ `public` / `private` 用 NULL 语义表达得了，**`licensed` 表达不了** |
| D | `can_show_raw` | ❌ 没有。这是 `licensed` 真正缺的那一半 |
| D | `parse_engine` `parse_confidence` | ⚠️ `parse_engine` 只在 `document` 上；`parse_confidence` 完全没有 |

### 1.5 质量监控（方案 §2.5）—— 差距最大的一块

| 方案要求 | 现状 |
|---|---|
| Dagster asset checks 失败即**阻断下游物化** | ❌ **全仓库零个 `@asset_check`**（grep 零命中） |
| `quality_metric` 表 + 看板 SQL 视图 | ❌ 没有 |
| 数据契约（Pandera / GE）在 bronze→silver 边界 | ❌ 没有 |
| 每日抽 200 条、三个历史 `as_of` 重放比哈希 | ❌ 有 5 条 SQL 不变量自检（`check_point_in_time_leaks`），但那是**全表扫描查违规**，不是抽样重放 |
| `known_at < publish_time` 计数恒为 0 | ❌ 现有不变量是 `known_at >= period_end`，**没有** `known_at >= publish_at` 这条 |
| 每周 recall@10 跌破 80% 阻断发版 | ⚠️ 脚本和 CI 门禁都在，**但 `evals/` 是空目录（只有 `.gitkeep`）** |
| 每周 50 块人工抽检 | ❌ 没有工具 |

### 1.6 编排（方案 §3.1）—— 一个「已写代码但没通电」的洞

方案要求 bronze → silver → gold 四层资产按日分区跑。

本仓库 `ingest/definitions.py` 的 `Definitions(assets=[fact_normalized, fin_fact_loaded])`
**只注册了两个事实资产**。文档管线的三个资产 `doc_normalized` / `doc_blocks_loaded` /
`block_embeddings` 写完了、有测试、但**没有注册进 DAG**，生产环境里根本不会跑。

`assets_docs.py` 的模块 docstring 自己承认了这件事：

> 本任务不改 `definitions.py`，真正接入生产 DAG 时这三个资产该怎么串起来……是留给接线任务的设计决定

这就是阶段 A。所有监控都要挂在资产上，资产没通电，后面全是空转。

---

## 二、实施顺序

```
A 管线通电 ──→ B 来源登记与四条款 ──→ C 块级元数据补齐 ──→ D 质量指标底座
                                                              │
                                              ┌───────────────┴───────────────┐
                                              ▼                               ▼
                                     E 时点泄漏抽样重放              F 接入对账与新鲜度
                                              │                               │
                                              └───────────────┬───────────────┘
                                                              ▼
                                                   G C 档二次解析路由
                                                              ▼
                                                   H 评测集数据侧与真实基线
```

顺序不是按重要性排的，是按**依赖**排的：

- D 的 asset check 必须挂在资产上 → 所以 A 先
- D 要有指标可算 → 所以 C 先（`parse_confidence` 等指标列）
- C 的 `can_show_raw` 继承自来源合同 → 所以 B 先
- G 的触发条件是 `parse_confidence < 0.7` → 所以 C 先
- H 要从真实语料分层抽样 → 所以管线得先真的在跑

---

# A · 文档管线接进 Dagster DAG

**为什么是第一件事**：三个文档资产没注册进 `Definitions`，生产环境从不执行它们。
在这个前提下谈「每日解析质量监控」「接入延迟 P95」都是空话——没有 run，就没有可监控的东西。

## 要改什么

**文件**
- 改：`packages/ragdemo/src/ragdemo/ingest/definitions.py`
- 改：`packages/ragdemo/src/ragdemo/ingest/assets_docs.py`
- 改：`packages/ragdemo/src/ragdemo/embed/batch.py`
- 测：`tests/ingest/test_assets_docs.py`（已存在，扩充）

**具体动作**

1. `prepare_documents` 目前是纯函数，靠 `ResourceParam[Sequence[PreparedDocument]]` 这个
   **测试期 hack** 喂给 `doc_blocks_loaded`。改成真正的资产依赖：新增 `doc_prepared` 资产，
   依赖 `doc_normalized`，产出 `list[PreparedDocument]`；`doc_blocks_loaded` 依赖 `doc_prepared`。

2. `Definitions` 注册五个资产（两个事实 + 三个文档），补齐 resources：
   `announcements` / `writer` / `embedder` / `blob` / `parser` / `conn`。
   资源一律走 `@resource` 惰性构造——`writer_resource` 已经是这个写法，照抄，
   理由也一样：模块导入时不该连库。

3. `block_embeddings` 现在查的是**全局** `embedding IS NULL` 队列，不按分区收窄
   （`assets_docs.py` 的注释点名把这件事留给接线任务）。改成只处理本分区
   `ingested_at` 落在分区日的块。

4. 并发去重：`embed_pending_blocks` 的 `ORDER BY b.block_id LIMIT 5000` 是确定性的，
   两个并发分区会选中**完全相同**的一批行，重复调用计费 API。
   加 `FOR UPDATE SKIP LOCKED`——这也是 `assets_docs.py` 注释里明确留下的缺口。

## 验收计划 A

| # | 怎么验 | 通过标准 |
|---|---|---|
| A1 | `uv run dagster asset materialize -m ragdemo.ingest.definitions --select 'doc_normalized+' --partition 2026-09-01` | 五个资产全部 MATERIALIZED，退出码 0 |
| A2 | 同一分区连跑两次，比对 `core.document` 的行数与 `content_hash` 聚合哈希 | 两次完全一致（幂等） |
| A3 | 两个分区并发跑，统计 `MockEmbedder` 的调用次数 | = 去重后待办块数，**不是它的两倍**（`SKIP LOCKED` 生效） |
| A4 | 跑完当日分区后 `SELECT count(*) FROM core.doc_block WHERE embedding IS NULL AND is_leaf` | = 0 |
| A5 | `uv run pytest tests/ingest tests/embed -q` | 全绿 |

**A 全部通过才开始 B。** A 不通过时 B 的迁移可以先写，但不要合并——`asof` 视图重建那一步
需要真实跑过的管线来验证列没漏。

---

# B · 来源登记与四条款

**为什么现在做**：方案 §2.1 里最有价值的一句是「把合规约束变成代码约束」。
四条款（能否缓存 / 能否展示原文 / 能否向量化 / 发布时间精度）现在只存在于合同 PDF 里，
代码完全不知道它们的存在。`can_show_raw` 的继承链是 D 组元数据的前置；
`time_precision` 直接决定 `known_at` 的保守边界，属于 `CLAUDE.md` §1.1 最高优先级那条。

## 要改什么

**文件**
- 建：`db/migrations/009_source_registry.sql`
- 改：`packages/ragdemo/src/ragdemo/adapters/base.py`
- 改：`packages/ragdemo/src/ragdemo/ingest/documents.py`（`DocumentWriter`）
- 建：`tests/contracts/test_time_precision.py`
- 改：`docs/04-ingestion.md`（新增来源登记一节）、`docs/02-data-model.md`

**具体动作**

1. `core.source_registry` 表：

```sql
CREATE TABLE core.source_registry (
  source_id          text PRIMARY KEY,
  vendor             text NOT NULL,
  layer              text NOT NULL,      -- structured/filing/industry/policy/news/user
  can_cache          boolean NOT NULL,
  can_show_raw       boolean NOT NULL,
  can_vectorize      boolean NOT NULL,
  time_precision     text NOT NULL CHECK (time_precision IN ('second','minute','day')),
  contract_expire_at date,
  rate_limit_qps     numeric
);
```

2. `core.document` 增列 `can_show_raw boolean NOT NULL DEFAULT true`、`time_precision text`；
   `core.doc_block` 增列 `can_show_raw boolean NOT NULL DEFAULT true`。
   继承链：`source_registry → document → doc_block`，第 4 层权限中间件只读块上这一列，
   不回溯合同。

3. **重建 `asof` 视图**。`007_parse_artifacts.sql` 里已经写明这个陷阱：
   `asof.document` 用 `SELECT *`，列清单在 `CREATE VIEW` 时就固定了，加列不会自动出现。
   而应用层与 Agent 只允许查 `asof` 视图（`CLAUDE.md` §1.1），不重建的话新列在整个应用侧
   **永远不可见，还不报错**。

4. `time_precision` 进 `known_at` 计算：`day` 精度的源，`known_at` 一律取当日收盘后
   （交易所所在时区 23:59:59），不取拿到数据的那一刻。这是把「精度不足」显式化为保守边界，
   避免隐性泄漏。

5. `DocumentWriter.write_document()` **从 registry 读 `can_show_raw`，不接受调用方传参**。
   调用方能传，这条继承链就形同虚设。

## 验收计划 B

| # | 怎么验 | 通过标准 |
|---|---|---|
| B1 | `core.document` 左连 `core.source_registry` 后 `source_id IS NULL` 的计数 | = 0（每份文档的来源都登记过） |
| B2 | `core.source_registry` 中四条款任一为 NULL 的行数 | = 0 |
| B3 | 契约测试：给 Mock 源设 `time_precision='day'`，拉一条当日数据 | `known_at` ≥ 当日 23:59:59，**不等于** `fetched_at` |
| B4 | 继承链测试：registry 里把某源 `can_show_raw` 置 false，跑一遍管线 | 该源的 `doc_block.can_show_raw` 全为 false |
| B5 | 带 `app.as_of` GUC 查 `asof.document` 的两个新列 | 不报 `column does not exist`（视图确实重建了） |
| B6 | 尝试 `writer.write_document(..., can_show_raw=True)` 绕过 registry | 抛 `TypeError`（参数根本不存在） |

**B 全部通过才开始 C。**

---

# C · 块级元数据补齐

**为什么现在做**：D 的质量指标要有东西可算。`parse_confidence` 是 G 档路由的触发条件，
也是第 7 层置信度降级规则的输入——`docs/07-agents.md` §5 已经写了「解析置信度低 → 降级」，
但它读的那个字段至今不存在。

## 要改什么

**文件**
- 建：`db/migrations/010_block_metadata.sql`
- 建：`packages/ragdemo/src/ragdemo/parse/confidence.py`
- 改：`packages/ragdemo/src/ragdemo/parse/chunker.py`、`parse/textin.py`
- 改：`packages/ragdemo/src/ragdemo/ingest/documents.py`
- 建：`tests/parse/test_confidence.py`
- 建：`docs/adr/0011-entity-ids-single-value-pending-pushdown-test.md`（见下；编号从
  0010 改成 0011——并发会话已经在同一天占用了 0010，见该 ADR 的索引）

**具体动作**

1. **`parse_confidence`**：四项加权，权重写死在代码里并注释出依据：
   - 字符覆盖率（`detail[]` 提取的字符数 / 页面预估字符数）
   - 表格闭合率（行列数一致的表格 / 总表格数）
   - 乱码字符比例（非 CJK、非 ASCII、非标点的字符占比）
   - 页面缺失率（`detail[]` 覆盖的页码 / `page_count`）

   写入 `document.parse_confidence` 与 `doc_block.parse_confidence`（块级取所在页的分）。

2. **`table_html`**：表格块的结构化形态。现在只有 `content_desc`（自然语言），
   双形态缺了供第 7 层做加总校验的那一半。xParse 的 `table_flavor=html` 目前只作用于
   **存档的 markdown**（`docs/05-document-pipeline.md:78`），要把它落到块上。

3. **`char_len`**：现在只有 `tokens`。方案的块长验收线是按中文字符计的，
   用 token 数验收会得出与标尺无关的数字。

4. **`chunking_version` / `embedding_version`**：
   - `chunking_version` 让重切新旧并存。已发布观点引用的旧 `block_id` 必须继续可解析，
     而第 10 层的「改切块 → 跑评测 → 对比」要求两版同时在库。
   - `embedding_version` 让换嵌入模型可灰度。`embedding_cache` 主键里已经有 `model` 列，
     但 `doc_block.embedding` 上没有对应标记。

5. **`entity_ids` 数组化 —— 先写 ADR，不要直接改**。
   方案说「一篇公告可涉及多家」，对。但 `entity_id` 是 `doc_block` 的 BM25 `fast` 字段，
   反规范化到块上的**唯一理由**就是让过滤条件能下推（`004_documents.sql` 的注释写得很清楚：
   「过滤条件若需 JOIN 才能求值就无法下推，时点过滤后召回会塌陷」）。
   改成数组要先确认 `pg_search` 对数组字段的下推行为，否则修一个正确性问题、
   换来一个召回塌陷。**ADR-0011 的内容就是这个实测结论**，实测之前保持单值。

## 验收计划 C

| # | 怎么验 | 通过标准 |
|---|---|---|
| C1 | A 组字段非空率：`doc_id` / `section_path` / `block_type` 为 NULL 的块数 | = 0 |
| C2 | C 组字段非空率：`valid_from` / `known_at` / `chunking_version` / `embedding_version` 为 NULL 的块数 | = 0 |
| C3 | `block_type='table'` 且 `table_html` 或 `content_desc` 为 NULL 的块数 | = 0（双形态都在） |
| C4 | 同一份 parse JSON、同一 `chunking_version` 重切两次 | 块数、`ordinal`、`content` 逐块相同 |
| C5 | 换 `chunking_version` 重切 | 新旧两版**同时在库**，旧 `block_id` 仍能查出来 |
| C6 | 叶子段落块 `char_len` 的 P5 / P95 分位数 | P5 ≥ 200、P95 ≤ 400（按冲突 1 的裁决口径） |
| C7 | 人工构造一份含乱码与断表的解析结果 | `parse_confidence` < 0.7 |
| C8 | `uv run pytest tests/parse -q` + `uv run mypy --strict packages/` | 全绿 |

**C 全部通过才开始 D。**

---

# D · 质量指标底座

**为什么现在做**：方案里说得最对的一句是「没有监控的管线等于没有数据」。
更关键的是那个「失败即**阻断下游物化**，而非仅告警」——告警会被忽略，阻断不会。
本仓库现在一个 `@asset_check` 都没有。

## 要改什么

**文件**
- 建：`db/migrations/011_quality_metric.sql`
- 建：`packages/ragdemo/src/ragdemo/quality/checks.py`
- 建：`packages/ragdemo/src/ragdemo/quality/metrics.py`
- 改：`packages/ragdemo/src/ragdemo/ingest/definitions.py`
- 建：`tests/quality/test_checks.py`

**具体动作**

1. `core.quality_metric` 表：`(metric, source_id, partition_date, value, threshold, passed, note, computed_at)`，
   外加一个 `quality.dashboard` 视图，看板只读这个视图。

2. `@asset_check(asset=..., blocking=True)` 绑定四类检查。**`blocking=True` 是这一阶段的全部意义**，
   非阻断的 check 等于没有：

   | 绑定资产 | 检查 | 阈值 | 失败动作 |
   |---|---|---|---|
   | `doc_normalized` | 每源应到 / 实到差异 | = 0 | 阻断，简报标「数据不完整」 |
   | `doc_blocks_loaded` | 解析成功率 | > 98% | 阻断 |
   | `doc_blocks_loaded` | 表格闭合率 | > 95% | 阻断 |
   | `doc_blocks_loaded` | `parse_confidence` P50 | > 0.8 | 告警，低分块转 C 档（阶段 G） |
   | `doc_blocks_loaded` | 孤儿块数 | = 0 | 阻断 |
   | `doc_blocks_loaded` | 叶子块长度合规率 | > 90% | 告警 |
   | `block_embeddings` | 向量化覆盖率 | = 100% | 阻断 |

3. 每次 check 的结果写 `quality_metric`，**通过也写**。只记失败就看不出「从 99% 滑到 96%」
   这种退化趋势——自动指标的价值在趋势，不在单点。

## 验收计划 D

| # | 怎么验 | 通过标准 |
|---|---|---|
| D1 | 人为往分区里塞一份表格闭合率 60% 的文档，跑该分区 | `block_embeddings` **不物化**（不是只报警） |
| D2 | 同上，查 `quality_metric` 里 `table_closure_rate` 那行 | `passed = false`，且 `value` 记录了实际的 0.60 |
| D3 | 正常分区跑完后统计当日 `quality_metric` 行数 | ≥ 7（七项都写了，不只失败项） |
| D4 | `SELECT * FROM quality.dashboard WHERE partition_date = current_date` | 返回当日全部指标，一条 SQL 出看板 |
| D5 | `uv run pytest tests/quality -q` | 全绿 |
| D6 | 把某项阈值调到必然失败，跑 CI | CI 红灯（门禁真的接上了） |

**D 全部通过才开始 E。**

---

# E · 时点泄漏抽样重放

**为什么现在做**：`CLAUDE.md` §1.1 是最高优先级。现有的 `check_point_in_time_leaks`
是**查违规**（扫全表找 `known_at` 早于 `period_end` 的行），方案要的是**查篡改**
（三个历史 `as_of` 重放，比对结果集哈希）。两者抓的是不同的东西：
前者抓写入时的错误，后者抓写入之后被无痕修改。

方案 §2.5 说这是「回测和复现不作弊」唯一可自动化验证的手段——同意。

## 要改什么

**文件**
- 建：`packages/ragdemo/src/ragdemo/quality/replay.py`
- 建：`db/migrations/012_asof_replay_and_publish_invariant.sql`
- 改：`packages/ragdemo-core/src/ragdemo_core/db/invariants.py`
- 建：`tests/quality/test_replay.py`

**具体动作**

1. **补上缺失的不变量**：`known_at >= publish_at`。
   现有六条不变量里有 `known_at >= period_end`，**没有** `known_at >= publish_at`。
   方案 §2.4 点名要求「`known_at` 早于 `publish_time` 的记录数恒为 0」，是对的：
   一份公告不可能在发布前就被知晓。加 CHECK 约束 + 加进 `_LEAK_QUERIES`。

2. **抽样重放**：每日随机抽 200 条历史 `doc_block`，取三个历史 `as_of`
   （该块 `known_at` 之前 / 之后 / 最近一次更正之后），经 `asof` 视图重放查询，
   对结果集算哈希，与 `quality_metric` 里首次记录的哈希比对。
   不一致 → 写 P0 级 `quality_metric` 行并阻断当日发布资产。

3. 抽样种子按 `partition_date` 派生，**不是随机种子**——重放要可复现，
   否则「今天不一致」没法复查。

## 验收计划 E

| # | 怎么验 | 通过标准 |
|---|---|---|
| E1 | `core.document` 中 `known_at < publish_at` 的行数 | = 0，且有 CHECK 约束挡住新写入 |
| E2 | 存量体检：同一条 SQL 在加约束**之前**先跑一次 | 若 > 0，先归因再加约束（迁移里不能静默丢数据） |
| E3 | 首日重放，记录 200 条基线哈希 | `quality_metric` 里有 200 行 `asof_replay_hash` |
| E4 | 人为把某条抽中块的 `known_at` 往前挪一天后重放 | 该条哈希不一致，写出 P0 行，下游阻断 |
| E5 | 正常情况连续 7 天重放 | 一致率 = 100% |
| E6 | 同一 `partition_date` 跑两次抽样 | 抽中**同一批** 200 条（种子可复现） |

**E 全部通过才开始 F。** E 和 F 之间没有代码依赖，赶进度的话可以并行开发，
但验收要分开签——混在一起签，哪条没过就说不清了。

---

# F · 接入对账与新鲜度

**为什么放在 E 之后**：这一阶段的价值依赖真实多源在跑。目前的实际情况是：

- `price_daily`（行情）**零接入代码**，只在 P0 的 schema 测试里出现过
- EDGAR 适配器只有 `FilingRef.document_url` 这个 URL 构造属性，**没有任何代码去取全文**

这两项在 `docs/10-roadmap.md` 里已经标了「部分完成」。对账的前提是「应到」有定义，
单源在跑的时候做对账，做出来的是一张只有一行的表。

## 要改什么

**文件**
- 建：`db/migrations/013_ingest_watermark.sql`
- 改：`packages/ragdemo/src/ragdemo/adapters/base.py`
- 建：`packages/ragdemo/src/ragdemo/ingest/reconcile.py`
- 改：`packages/ragdemo/src/ragdemo/adapters/tushare.py`（补 `daily` 行情）
- 改：`packages/ragdemo/src/ragdemo/adapters/edgar.py`（补全文抓取）
- 建：`tests/ingest/test_reconcile.py`

**具体动作**

1. `core.ingest_watermark (source_id, partition_date, cursor, updated_at)`。
   `FetchContext` 增 `watermark: str | None`。现在每次都按 `partition_date` 全量拉，
   增量语义靠去重索引兜底——能跑，但拉取量是线性增长的。

2. `RawResponse` 补 `external_id` / `publish_time` / `checksum`，
   去重唯一键改成 `(source_id, external_id, content_hash)`。
   现在的 `(source, content_hash)` 在「同一份公告供应商改了个标点重发」时会判成新文档。

3. 对账：每源每日记录「应到」（供应商返回的 total / 分页总数）与「实到」（入库行数），
   差异必须有归因（`note` 列），没有归因就是 check 失败。

4. 接入延迟：`fetched_at - publish_time` 的 P50 / P95 写 `quality_metric`。
   **按源分别设阈值**——方案的「P95 < 15 分钟」对公告源合理，对 EDGAR 这种日频源
   没有意义，一刀切会让这个指标永远红着，然后被忽略。

5. 补 Tushare `daily` 行情接入与 EDGAR 全文抓取。这两项是 P1 遗留，不是本方案新增，
   但 F 的验收（多源对账）依赖它们。

## 验收计划 F

| # | 怎么验 | 通过标准 |
|---|---|---|
| F1 | `quality_metric` 中 `reconcile_diff ≠ 0` 且 `note IS NULL` 的行数 | = 0（差异必须有归因） |
| F2 | 连跑同一分区两次 | 第二次拉取的记录数显著少于第一次（watermark 生效），入库行数不变 |
| F3 | 同一份公告改一个标点重发 | 判为**新版本**（`version_group_id` 与 `external_id` 相同、`content_hash` 不同），不是新文档 |
| F4 | 跑完一个分区后 `SELECT count(*) FROM core.price_daily` | > 0（行情真的接上了） |
| F5 | EDGAR 10-K 全文抓取 | `document.raw_ref` 指向的对象真实存在且非空 |
| F6 | 接入延迟 P95，按源分组 | 公告源 < 15 min；其余源记录但不设阻断阈值 |
| F7 | 抽 50 条 `provider_snapshot`，按 `response_ref` 取回 | 100% 可取回 |

**F 全部通过才开始 G。**

---

# G · C 档二次解析路由

**为什么现在做**：触发条件 `parse_confidence < 0.7` 依赖阶段 C，
「批量退化则暂停发布」依赖阶段 D 的阻断能力。前置不齐就做，只能做成一个没人看的开关。

## 要改什么

**文件**
- 建：`db/migrations/014_parse_tier_policy.sql`
- 建：`packages/ragdemo/src/ragdemo/parse/router.py`
- 改：`packages/ragdemo/src/ragdemo/parse/textin.py`（月度预算）
- 改：`packages/ragdemo/src/ragdemo/ingest/assets_docs.py`
- 建：`tests/parse/test_router.py`

**具体动作**

1. `core.parse_tier_policy` 表——触发规则**可配置且可审计**，不写死在代码里：

```sql
CREATE TABLE core.parse_tier_policy (
  policy_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  doc_type         text,           -- NULL = 任意
  confidence_below numeric,        -- NULL = 不按置信度触发
  closure_below    numeric,
  monthly_cap_cny  numeric NOT NULL,
  enabled          boolean NOT NULL DEFAULT true,
  known_at         timestamptz NOT NULL,
  superseded_at    timestamptz
);
```

   带时点列，因为「上个月为什么触发了这么多 C 档」要能回答。

2. 触发规则（方案 §2.2 原文，引擎按冲突 3 裁决对换）：
   `doc_type ∈ 高价值集合` 或 `parse_confidence < 0.7` 或 `块内含数值且表格闭合率 < 0.9`。

3. **月度预算闸门**。现有 `PageBudget` 是单次调用的页数预算，没有月度维度。
   超限时的行为必须是**告警 + 降级为标注低置信度**，
   而不是静默跳过——静默跳过会让下游以为这份文档解析质量正常。

## 验收计划 G

| # | 怎么验 | 通过标准 |
|---|---|---|
| G1 | 跑一个月的真实分区，统计 C 档触发率 | < 3% |
| G2 | 造一份 `parse_confidence=0.5` 的文档 | 自动进 C 档，重解析后 `parse_engine` 记录为高精度参数 |
| G3 | 把月度上限调到 0，再喂一份该进 C 档的文档 | 告警 + `parse_warnings` 含 `budget_exceeded`，**且仍然入库**（不是静默跳过） |
| G4 | 同上场景 | `parse_confidence` 保持低分，下游检索按低置信度降权 |
| G5 | C 档重解析后对比 | `parse_confidence` 高于 B 档结果（二次解析确实有用，否则这笔钱不该花） |
| G6 | 解析失败文档 | 100% 进重试队列，且队列里带 24 小时超时标记 |

**G 全部通过才开始 H。**

---

# H · 评测集数据侧与真实基线

**为什么最后做**：分层抽样需要真实语料在库里。现在 `evals/` 是空目录，
`recall@10` 的两个数字跑在 5 条自造用例、6 个块的夹具语料上——
`docs/06-retrieval.md` §9.2 要的是 **100 条生产用例**。
这也是 `docs/10-roadmap.md` 里 P1「代码做完 9/11，验收不能宣布通过」的那个缺口。

**这一阶段有外部依赖**：标注由创始人做（方案 §1.2 职责边界表写明了）。
本阶段交付的是**抽样工具 + 冻结快照 + 跑分脚本**，不是标注本身。

## 要改什么

**文件**
- 建：`packages/ragdemo/src/ragdemo/evals/sampler.py`
- 建：`packages/ragdemo/src/ragdemo/evals/freeze.py`
- 改：`packages/ragdemo/src/ragdemo/evals/cli.py`
- 建：`evals/retrieval/2026-10-baseline.jsonl`（标注产物，创始人交付）
- 建：`tests/evals/test_sampler.py`

**具体动作**

1. **分层抽样工具**：按 `doc_type × parse_engine × entity` 分层，
   避免 100 条全落在最容易的年报正文上。抽样种子固定，结果可复现。

2. **冻结快照**：把 `(as_of, block_id 集合, chunking_version, embedding_version)` 固化。
   没有这个，两周后重跑评测，数字变化分不清是检索改好了还是语料变了。

3. **人工抽检工具**：每周 50 块，输出 CSV 给人核对元数据（页码、`section_path`、实体映射），
   结果回流抽取评测集。方案 §2.5 说得对——自动指标只能发现「和昨天不一样」，
   发现不了「一直是错的」。

4. 跑分脚本接进 CI 的 nightly，`recall@10` 跌破 80% 阻断发版。
   门禁代码已经有了，缺的是喂给它的真实数据。

## 验收计划 H

| # | 怎么验 | 通过标准 |
|---|---|---|
| H1 | `uv run ragdemo evals sample --n 100 --stratify doc_type,parse_engine` | 产出 100 条候选，每层至少 5 条 |
| H2 | 同一种子跑两次 | 抽中完全相同的 100 条 |
| H3 | 创始人标注完成后入库 `eval_retrieval` | ≥ 100 条，覆盖 ≥ 3 种 `doc_type` |
| H4 | `uv run ragdemo evals run --set retrieval` | `recall@10` > 80%、`recall@50` > 95%，数字贴进 PR |
| H5 | 冻结快照后重跑三次 | 三次结果完全一致 |
| H6 | 人工抽检 50 块 | 元数据全对比例 > 95% |
| H7 | 故意把 `ChunkConfig` 改坏（`leaf_max_chars=50`）跑评测 | `recall@10` 下降且 CI 红灯（门禁真的挡得住） |

**H 通过 = 方案 §1.1 那句可验收的话真正成立**：

> 给定任意 `as_of` 时点和任意实体，能够返回该时点之前已公开的、带页码与 bbox、
> 带解析置信度、带展示权限标识的文档块集合，且重放三次结果一致。

到这一步，这句话的每一个成分才都有对应的列和对应的测试：
`as_of`（已有）、页码 bbox（已有）、**解析置信度**（阶段 C）、
**展示权限标识**（阶段 B）、**重放三次一致**（阶段 E）。

---

## 三、这份计划不做的事

按 `CLAUDE.md` §4 与方案 §1.3，以下明确不在范围内，避免默默扩张：

- **不自研解析器**。C 档是换参数或换供应商，不是自己写。
- **不定义金融口径**。指标字典、AI 收入拆分规则由创始人交付，管线只消费。
- **不做检索与重排策略**。阶段 C 提供过滤字段，`RetrievalService` 怎么用是第 6 层的事。
- **不在管线里做推断**。`parse_confidence` 是测量，不是推断；管线只搬运和标注。
- **不碰研报全文**（合规红线）。
- **不做实体表 / 别名库的录入**。P0 那一项（实体 18/100、别名 73/400、关系 30/200）
  仍然是创始人侧未完成的交付，阶段 F 的实体映射命中率验收依赖它。

## 四、外部依赖与风险

| 依赖 | 卡住哪个阶段 | 现状 |
|---|---|---|
| 公告结构化接口选型 + 四条款确认 | B（registry 填不满）、F（对账无「应到」） | 未定。方案 §5 决策 1 |
| 实体表 100 家 / 别名 400 / 关系 200 | F 的实体映射命中率 > 92% | 18 / 73 / 30，未完成 |
| 指标字典 30 个 | 不卡本计划（文档管线可并行） | 已满 |
| 评测集标注（100 条） | H | 未开始 |
| TextIn 月度预算上限 | G 的 `monthly_cap_cny` | 未拍板。方案 §5 决策 3 |

**最大的单点风险**：阶段 H 的标注是人工工作，工程侧无法替代。
建议在阶段 A 完成、语料开始积累时就同步启动抽样与标注，
不要等到 G 结束才开始——否则 H 会成为一段纯等待。
