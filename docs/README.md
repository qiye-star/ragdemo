# 技术文档索引

AI 产业链时点研究引擎（以下简称「引擎」）的规范级技术文档。
本文档集是实现契约：`db/`、`src/`、`tests/` 下的代码以此为准，不一致时以本文档集为准并提 PR 修正。

## 阅读顺序

**第一次接触本项目**，按 00 → 01 → 03 → 02 读。先理解目标与架构，再理解时点语义（这是全系统的地基），最后看表结构——否则会看不懂为什么每张表都有三个时间戳。

**想知道「从哪开始、做完怎么算数、现在到哪了」**：直接读 [12](12-dev-flow.md)。它把 00–09 串成一条主线，每环节给出治理文档、对应工作流、验收标准与实测进度。

**要写数据接入**：04 → 05 → 02
**要写检索**：06 → 03 → 02
**要写 Agent**：07 → 09 → 08
**要做评测或回测**：08 → 03
**要排期、分工或动手写代码**：12 → 11 → 10 → `superpowers/plans/` 下当前阶段的计划

## 文档清单

| 文件 | 内容 | 谁需要读 |
|---|---|---|
| [00-overview.md](00-overview.md) | 目标、范围边界、约束、非目标、合规底线 | 所有人 |
| [01-architecture.md](01-architecture.md) | 分层架构、组件边界、数据流、部署拓扑 | 所有人 |
| [02-data-model.md](02-data-model.md) | 全量 PostgreSQL DDL 与逐字段数据字典 | 后端、数据 |
| [03-point-in-time.md](03-point-in-time.md) | **时点一致性规范**：三时间戳语义、`as_of` 查询、更正处理、防泄漏 | 所有人 |
| [04-ingestion.md](04-ingestion.md) | 数据源、适配器接口、Dagster 资产与分区、限流重试 | 数据工程 |
| [05-document-pipeline.md](05-document-pipeline.md) | TextIn xParse 解析与产物落盘、切块、父子块、元数据、向量化 | 数据工程 |
| [06-retrieval.md](06-retrieval.md) | 过滤 → 双路召回 → 加权 RRF → 重排 → 父子块展开 | 后端 |
| [07-agents.md](07-agents.md) | Agent 目录、工具外壳层、编排、prompt 版本化、验证器 | AI 工程 |
| [08-evaluation.md](08-evaluation.md) | 三套评测集、指标阈值、观点评分公式、CI 门禁 | 所有人 |
| [09-compliance-security.md](09-compliance-security.md) | 输出合规过滤、溯源强制、权限隔离、密钥、审计哈希链 | 所有人 |
| [10-roadmap.md](10-roadmap.md) | P0–P6 **交付什么**：阶段产出与阶段级量化验收 | 所有人 |
| [11-sdlc.md](11-sdlc.md) | **怎么交付**：生命周期规范、工作流分解、依赖与并行、变更控制 | 所有人，排期时必读 |
| [12-dev-flow.md](12-dev-flow.md) | **按什么顺序**：00–09 串成一条主线，每环节的验收标准与**当前进度快照** | 所有人，第一次上手必读 |
| [glossary.md](glossary.md) | 术语表 | 所有人 |
| [adr/](adr/README.md) | 架构决策记录（8 篇） | 所有人 |
| [superpowers/plans/](superpowers/plans/) | 每阶段一份可执行的 TDD 逐步计划 | 执行者 |

## 与项目简报的章节映射

本文档集覆盖项目简报（PROJECT BRIEF）的全部章节：

| 简报章节 | 对应文档 |
|---|---|
| §0 一句话目标 | `00-overview.md` §1 |
| §1 团队与约束 | `00-overview.md` §3 |
| §2 核心设计原则 | `/CLAUDE.md` §1 + 各文档正文 |
| §3 系统架构（十层） | `01-architecture.md` 全篇（含原编号映射） |
| §4 数据源与接入方式 | `04-ingestion.md` §1–§2 |
| §5 数据模型 | `02-data-model.md` 全篇 |
| §6 Agent 设计 | `07-agents.md` §1–§3 |
| §7 检索管线规范 | `06-retrieval.md` 全篇 |
| §8 切块规范 | `05-document-pipeline.md` §3 |
| §9 分阶段任务与验收 | `10-roadmap.md`（交付与验收）+ `11-sdlc.md`（流程、工作流、并行） |
| §10 仓库结构建议 | `01-architecture.md` §5 |
| §11 工程约定 | `/CLAUDE.md` §3 + `11-sdlc.md` §2（DoR/DoD）、§6（变更控制） |
| §12 开放问题 | `adr/0001`–`adr/0007`（全部已决策） |
| —（简报之外的后续决策） | `adr/0008` 文档解析供应商 |
| §13 术语表 | `glossary.md` |

## 对简报的四处修正

文档按修正后的版本编写。每处修正在对应章节都标注了原因，此处汇总便于核对：

1. **融合方式冲突**（简报 §7）——「BM25 权重 0.6 / 向量 0.4」与「RRF 合并」是两种互斥的融合方式，不能同时执行。统一为**加权 RRF**。见 `06-retrieval.md` §4。
2. **`known_at` 语义**（简报 §5.3、§13）——必须是「现实中最早可获知的时刻」，不是入库时间。见 `03-point-in-time.md` §1。
3. **时点过滤与索引的交互**（简报未涉及）——HNSW 与 BM25 索引无法把 `known_at <= as_of` 天然下推，朴素写法会让时点过滤后的召回塌陷。见 `06-retrieval.md` §3。
4. **层号顺序**（简报 §3）——架构图中 L5 排在 L4 前。文档按数据流重排，附原编号映射表。见 `01-architecture.md` §1。

此外，`02-data-model.md` 相对简报调整了主键类型（`doc_id` / `block_id` / `opinion_id` / `event_id` 改为 `bigint` 标识列），原因见该文档 §1.2。


## 验证记录

本文档集的 SQL 不是纸面设计。以下内容已在 **ParadeDB `paradedb/paradedb:latest`
（PostgreSQL 18.6 / `pg_search` 0.25.9 / `pgvector` 0.8.4）** 上实际执行验证：

| 验证项 | 结果 |
|---|---|
| `02` / `03` / `04` / `05` / `09` 的全部 DDL（25 个代码块）在空库执行 | **0 error，0 warning**；建成 19 张 `core` 表、7 个 `asof` 视图、5 张 `evals` 表、1 张 `audit` 表、72 个索引 |
| `doc_block` 的 BM25 索引（`chinese_lindera` 分词 + `boolean_fields`） | 创建成功，分词器配置确实生效 |
| `doc_block` 的 HNSW 向量索引（1024 维 / `vector_cosine_ops`） | 创建成功 |
| `asof` 视图未设 `app.as_of` 时的行为 | **报错**「app.as_of is not set」，不是静默返回空集 |
| `app_read` 直接查 `core.fin_fact` | **permission denied** |
| `app_write` 的列级 UPDATE 权限 | `superseded_at` 可写、`value` 不可写 |
| `core.document` / `core.doc_block` 的行级安全 | 已启用 |
| `03` §5 的五条时点泄漏自检查询 | 可执行 |
| `06` §4.2 的完整混合检索 SQL（BM25 + 向量 + 加权 RRF） | 端到端跑通，时点过滤正确屏蔽了 `known_at > as_of` 的文档 |
| 中文 BM25 检索（「云端训练芯片」） | 正确命中 |

**验证中发现并已修正的两处缺陷**：

1. **`raw` 分词器默认小写化 token**。原稿用 `raw` 索引 `entity_id`，
   导致 `paradedb.term('entity_id', 'CN.688256')` 匹配 0 行——而
   `entity_id` 的格式恰恰是大写开头。**查询不报错，只静默返回空结果。**
   已改为 `keyword` 分词器（`02-data-model.md` §5.5）。
2. **`datetime_fields` 自 `pg_search` v0.24.1 起已废弃且完全无效**。
   原稿写了该选项，执行时只得到一条 WARNING。已移除（同上）。

**尚未验证、留给 P0/P1 的**：过滤谓词是否真正下推进索引扫描
（`EXPLAIN` 验证需要真实数据量，3 行测试数据下优化器根本不会走索引）；
`06` §3.3 的 pgvector 迭代扫描参数效果；召回率指标。
这些都在 `10-roadmap.md` 的 P0/P1 验收项中。

## 变更记录

| 日期 | 变更 | 影响面 |
|---|---|---|
| 2026-09-21 | **路径 B 的解析器由 MinerU 换为合合信息 TextIn xParse**（[adr/0008](adr/0008-textin-xparse-document-parsing.md)）。解析产物（完整响应 JSON + Markdown）落对象存储，`core.document` 增三列，迁移 `007_parse_artifacts.sql` | `01` §1/§4/§5、`02` §5.1、`04` §1、`05` §1–2/§3.3/§7.2/§8、`09` §4.1、`10` P1/P4、`11` W2.2、`CLAUDE.md` §2、P1b Task 6/9/10 |

`007_parse_artifacts.sql` 已在真实 ParadeDB 上执行验证：迁移 001–007 全序列
0 error；三个新列确实出现在 `asof.document` 视图中（`CREATE OR REPLACE VIEW`
重建过），且 `app_read` 的 SELECT 授权在重建后保留。
P1b Task 9 的解析器纯函数与缓存路径也已按计划里的夹具实跑通过。
