# PROJECT BRIEF — AI 产业链时点研究引擎

> 用途：本文件是交给 Claude Code 的项目简报与架构大纲。
> 请 Claude Code 先用 `/superpowers:brainstorm` 就本文件逐节向我确认理解，
> 再用 `/superpowers:write-plan` 生成分阶段、可验证的详细工作计划（每步可在 1–4 小时内完成并有验收标准），
> 最后用 `/superpowers:execute-plan` 按 TDD 执行。
> 不要跳过 brainstorm 直接写代码。所有数值计算走代码不走模型；所有数据查询强制时点参数。

---

## 0. 一句话目标

构建一个面向中国大陆 AI 产业链上市公司（含关键海外环节）的研究引擎：
多数据源接入 → 时点一致的数据库 → 带溯源的检索 → 多 Agent 生成研究观点 → 观点评分与回测。
产出物是**研究参考**，不是投资建议。

## 1. 团队与约束

- 团队：2–3 位金融背景创始人（负责规则、图谱、标注）；技术负责人 1 人（待招，前期外包过渡）；数据工程师 0–1 人
- 技术栈：Python 3.11+；PostgreSQL 15+ 与 pgvector；Dagster；LangGraph 或自写状态机；国内可备案模型（DeepSeek / Qwen / Kimi）API；嵌入 bge-m3 或 Qwen3-Embedding；重排 bge-reranker-v2-m3
- 部署：起步阿里云或腾讯云单机 + RDS PostgreSQL；后期支持私有化
- 预算：数据 + 模型每月 ≤ 5000 元（起步期）
- 合规硬约束：不输出买卖建议 / 目标价 / 仓位；数据境内存储；用户材料私有隔离；每个数字可溯源
- 不做：自研模型、自建爬虫（公告走供应商接口）、行情终端、全行业覆盖

## 2. 核心设计原则（写进 CLAUDE.md）

1. **时点一致**：每张数据表带 `valid_from`、`known_at`、`superseded_at`；所有查询接口强制 `as_of` 参数；回测时物理上看不到 `known_at > as_of` 的数据
2. **计算与生成分离**：同比、倍数、加权全部由代码算，模型只决定算什么
3. **强制溯源**：输出中的每个数值绑定 `[block_id | page]` 或 `[metric_id | source | known_at]`；无来源标 `推断`
4. **评测驱动**：改 prompt / 切块 / 模型 → 跑三套评测集 → 看数字 → 再合并
5. **供应商可替换**：所有外部数据源经统一适配器层；入库保留原文与结构
6. **增量触发**：只有新公告 / 新闻 / 财报进入才触发相关 Agent，不做每日全量重算
7. **实体优先检索**：先按实体 / 时间 / 文档类型过滤，再混合检索，再重排

## 3. 系统架构（十层）

```
外部数据（数据商 API/MCP · 公告接口 · EDGAR · RSS · 用户上传）
   ↓
[L1 实体解析] entity / entity_alias / entity_relation
   ↓
[L2 口径归一] node_metric 指标字典 · AI 收入拆分规则 · 财年/币种转换
   ↓
[L3 时点管理] 双时间轴 · 快照 · as_of 查询接口
   ↓
[L5 文档管线] 供应商结构化公告落库 · MinerU 解析补充 · 切块 · 元数据 · 向量化
   ↓
[L4 权限隔离] C 端：用户私有空间；B 端：多租户 / SSO / 项目隔离 / 审计
   ↓
[L6 检索路由] 意图路由 · 结构化查询翻译 · 元数据过滤 · BM25+向量混合 · 重排 · 父子块
   ↓
[Agent 层] 产业链映射 → 基本面 → 事件 → 宏观政策 → 多空辩论 → 观点入库 → 复盘
   ↓
[L7 引用验证] 输出约束 · 后置验证器 · 置信度规则
   ↓
[L8 输出模板] 公司追踪简报 · 环节周报 · 事件解读 · 一二级对标报告 · Excel 导出
   ↓
[L10 反馈评测] 检索评测集 · 抽取评测集 · 判断评测集 · 观点库评分 · 修正记录

[L9 工作流编排] Dagster（数据管线，按日分区，可回填）+ LangGraph（Agent 流程）横向贯穿
[可信执行] 网络代理 + 凭据代管 + 域名白名单；执行轨迹 SQLite + 哈希链；后期接 Codex 内核策略/沙箱
```

## 4. 数据源与接入方式

| 数据 | 来源 | 接入 | 阶段 |
| --- | --- | --- | --- |
| A股/港股财务、行情、股东、分部 | Tushare Pro | REST，每日批量同步 | P1 |
| A股/港股公告（已结构化：段落、表格、页码） | 庖丁 PDFlux / 通联 / iFinD 公告接口（待选定） | REST 批量同步；MCP 仅按需补查 | P1 |
| 美股/台股/韩股财务、电话会 | FMP / Financial Datasets + SEC EDGAR | REST；EDGAR 免费 | P1 |
| 台积电月营收、法说会 | 公司 IR 页 | 抓取 | P2 |
| 云厂商 Capex 指引 | 从电话会文稿抽取 | Agent 抽取 + 人工校验 | P2 |
| 政策（BIS 出口管制、工信部/发改委） | 官网 | 抓取 + 结构化 | P3 |
| 新闻 | 财联社 / 新浪财经 / 海外 RSS | RSS 拉取 | P3 |
| 模型与技术进展 | Hugging Face / arXiv / 公司博客 | RSS / API | P3 |
| 用户上传材料 | 用户 | 上传 → MinerU 解析 → 私有空间 | P4 |

接入原则：批量同步走 REST/数据文件；MCP 只做长尾按需查询；供应商 MCP 一律包在自有工具外壳层内（参数校验、日志、缓存、限流、结果规范化）。

## 5. 数据模型（核心表，PostgreSQL）

### 5.1 实体层
- `entity(entity_id PK, name_full, name_short, name_en, entity_type, market, tushare_code, ifind_code, wind_code, edgar_cik, l1_layer, l2_segment, l3_node text[], primary_node, ai_revenue_pct numeric, ai_revenue_src, hq_country, status, created_at, updated_at, notes)`
- `entity_alias(alias, entity_id FK, alias_type, source, confidence, PK(alias, entity_id))`
- `entity_relation(relation_id PK, from_entity FK, to_entity FK, relation_type ∈ {supplies_to, customer_of, competes_with, invests_in, depends_on, substitutes}, strength, share_estimate, direction_note, evidence_block text[], evidence_type, valid_from, valid_to, known_at, confidence, updated_at)`

### 5.2 指标与规则层
- `node_metric(metric_id PK, l3_node, metric_name, metric_role ∈ {leading, confirming}, frequency, source_type, source_entity text[], extraction_hint, direction, definition, unit)`
- `metric_source_map(metric_id FK, provider, provider_field, priority, notes)`
- `ai_revenue_rule(entity_id FK, rule_text, source_type, confidence, author, effective_from)`
- `propagation_rule(rule_id PK, trigger_node, trigger_event, affected_node, direction, lag, mechanism, weight_field, confidence, evidence_block text[], author, created_at)`

### 5.3 时点数据层（所有事实表共用字段）
- 公共字段：`valid_from date, known_at timestamptz NOT NULL, superseded_at timestamptz NULL, source, source_ref, ingest_run_id`
- `fin_fact(entity_id, metric_id, period, value, unit, currency, + 公共字段)`
- `price_daily(entity_id, trade_date, open, high, low, close, volume, + 公共字段)`
- `provider_snapshot(snapshot_id PK, provider, endpoint, params jsonb, response jsonb, fetched_at)`

### 5.4 文档层
- `document(doc_id PK, entity_id FK, doc_type, title, period, publish_at timestamptz, source, source_url, version_group_id, is_correction bool, supersedes_doc_id, raw_ref, + 公共字段)`
- `doc_block(block_id PK, doc_id FK, parent_block_id NULL, block_type ∈ {paragraph, table, figure, title}, section_path text, page int, bbox numeric[4], content text, content_desc text, embedding vector(1024), tokens int, + 公共字段)`
- 索引：`doc_block(embedding)` HNSW；`doc_block(doc_id, section_path)`；全文索引（BM25 用 pg_trgm / 或外置 tantivy）

### 5.5 观点与评测层
- `opinion(opinion_id PK, entity_id NULL, l3_node NULL, event_id NULL, direction ∈ {bull, bear, neutral}, confidence, thesis text, evidence_blocks text[], evidence_facts text[], agent_name, prompt_version, model, created_at, as_of)`
- `opinion_score(opinion_id FK, horizon ∈ {1M, 3M}, scored_at, outcome_desc, score numeric, scorer ∈ {auto, human})`
- `event(event_id PK, event_type, trigger_entity, trigger_node, summary, publish_at, source_block, matched_rules text[], affected_entities jsonb, + 公共字段)`
- `eval_retrieval(q_id PK, question, as_of, gold_block_ids text[], entity_filter, notes)`
- `eval_extraction(case_id PK, doc_id, field, gold_value, unit, notes)`
- `correction_log(id PK, agent_name, original_output, corrected_output, reason, corrector, created_at)`

### 5.6 审计与执行
- `tool_call_log(call_id PK, run_id, agent_name, tool_name, params jsonb, result_ref, cost, started_at, ended_at, prev_hash, hash)`

## 6. Agent 设计

| Agent | 输入 | 输出 | 模型档位 |
| --- | --- | --- | --- |
| 路由 | 用户问题 / 触发事件 | 意图类型 + 工具计划 | 小 |
| 产业链映射 | 事件、实体 | 受影响实体列表（按 ai_revenue_pct × share_estimate 加权） | 代码 + 小 |
| 基本面 | 实体、as_of | 关键指标变化、异常、带来源 | 中 |
| 事件解读 | 新公告/新闻块 | event 记录、匹配的传导规则、影响方向/力度/时滞 | 中 |
| 宏观政策 | 政策文本 | 影响环节与机制 | 中 |
| 多空辩论 | 上述输出 | 看多论据、看空论据、裁判结论、置信度 | 强（三个角色） |
| 验证器 | 生成结果 | 通过 / 标注问题 | 小 + 代码 |
| 复盘评估 | 历史 opinion + 后续数据 | opinion_score | 代码 + 中 |

工具集（对 Agent 暴露，全部经外壳层）：`query_fin_fact(entity, metric, period, as_of)`、`search_blocks(query, entity?, doc_type?, as_of, k)`、`get_relations(entity, relation_type?, as_of)`、`match_rules(event_type, trigger_node)`、`compute(expr, inputs)`、`write_opinion(...)`（需确认节点）。

## 7. 检索管线规范

1. 过滤：`entity_id ∈ ...`、`known_at ≤ as_of`、`doc_type ∈ ...`
2. 混合：BM25 权重 0.6，向量 0.4（可配置），RRF 合并取前 50
3. 重排：bge-reranker-v2-m3 取前 10
4. 父子块：命中子块返回其所属小节
5. 查询改写：同义词表扩展 + 大模型生成 2 个变体（可开关）
6. 评测：`eval_retrieval` 上 recall@10、MRR，CI 中运行

## 8. 切块规范

- 按文档结构切：小节 / 表格边界；段落 300–800 字，重叠 10–15%
- 表格单独成块，附表头与一句自然语言描述（`content_desc`）
- 块首拼章节路径；元数据齐全（见 `doc_block`）
- 父子块：子块 ≤ 200 字用于检索，父块为整小节用于生成

## 9. 分阶段任务与验收

### P0 准备（第 1–2 周）— 创始人手工 + Claude Code 搭骨架
- [ ] 仓库初始化、CLAUDE.md（写入第 2 节原则）、pre-commit、CI
- [ ] PostgreSQL + pgvector 建表脚本（第 5 节全部表）与迁移工具
- [ ] 实体表 100 家、关系 200 条、传导规则 15 条、指标 30 个的 CSV 导入工具
- 验收：`make db-init && make seed` 成功；`SELECT count(*) FROM entity` = 100

### P1 底座（第 3–7 周）
- [ ] Tushare 适配器 + Dagster 每日分区资产（财务、行情）
- [ ] 公告供应商适配器（接口待定，先写抽象接口 + Mock）
- [ ] EDGAR 适配器（10-K/10-Q/8-K 列表与全文）
- [ ] 时点写入中间件：自动 `known_at`；更正公告处理
- [ ] 切块器 + 嵌入器 + 入库；MinerU 解析器封装
- [ ] 检索服务（过滤 → 混合 → 重排 → 父子块）
- [ ] `eval_retrieval` 100 条录入工具 + 评测脚本
- 验收：管线连续 5 天无人工干预跑通；recall@10 > 0.80；任意查询带 `as_of` 可复现

### P2 单公司 Agent（第 8–11 周）
- [ ] 工具外壳层（参数校验、日志、缓存、预算）
- [ ] 基本面 Agent + 验证器
- [ ] 简报模板 v1 + 渲染（Markdown/HTML）
- [ ] `eval_extraction` 50 条 + 字段准确率脚本
- 验收：任选 3 家公司生成简报，字段准确率 > 0.95，每个数字可点回原文

### P3 传导与辩论（第 12–16 周）
- [ ] 新闻 / 政策 RSS 接入；事件抽取 Agent
- [ ] 传导规则匹配 + 产业链映射
- [ ] 多空辩论（三角色）+ 观点入库（确认节点）
- [ ] 复盘评估 Agent + `opinion_score`
- [ ] 增量触发调度
- 验收：连续 2 周每日自动产出事件解读与观点；观点 100% 带依据链

### P4 产品化（第 17–20 周）
- [ ] Web 简报（阅读、追问、溯源点击）
- [ ] Excel 导出（保留公式）
- [ ] 用户上传 → 私有空间检索
- [ ] 输出合规过滤器（禁词、无来源数值拦截）
- 验收：20 位种子用户可登录使用；追问功能可用

### P5 商业化（第 21–26 周）
- [ ] 付费与账号体系
- [ ] 观点准确率公开页
- [ ] 机构版权限模型设计文档
- 验收：付费可用；观点库有 3 个月评分

### P6 机构版（第 7–12 月）
- [ ] 多租户、SSO、项目隔离、审计日志
- [ ] 一二级对标报告模板；CIM 上传交叉验证
- [ ] Word/PPT 导出与机构母版
- [ ] 可信执行：网络代理 + 凭据代管 + 哈希链审计；评估接入 Codex 内核

## 10. 仓库结构建议

```
repo/
  CLAUDE.md                 # 原则、约定、禁止事项
  pyproject.toml
  infra/                    # docker-compose、RDS 脚本
  db/                       # 迁移、建表、seed CSV
  src/
    adapters/               # tushare / announcements / edgar / rss / mcp_shell
    ingest/                 # dagster 资产、分区、known_at 中间件
    parse/                  # mineru 封装、切块、元数据
    retrieval/              # 过滤、混合、重排、父子块、同义词
    entities/               # 解析、别名、消歧
    metrics/                # 指标字典、口径转换、计算
    agents/                 # 路由、基本面、事件、政策、辩论、验证、复盘
    tools/                  # Agent 工具外壳层
    templates/              # 简报/周报/对标模板与渲染
    audit/                  # tool_call_log、哈希链
    api/                    # Web 后端
  web/                      # 前端
  evals/                    # 三套评测集与脚本
  docs/                     # ADR、数据字典、规则说明
  tests/
```

## 11. 工程约定

- TDD：先写失败测试再实现；数值与时点逻辑必须有单测
- 每个外部适配器有 Mock 与契约测试
- 所有 prompt 版本化存 `src/agents/prompts/<agent>/<version>.md`，变更需跑评测
- 每个 PR 自动跑三套评测并在描述中贴数字
- 日志结构化（JSON），含 `run_id`、`as_of`、`entity_id`
- 密钥只在网络代理 / 环境变量，代码与日志中不得出现

## 12. 开放问题（请 Claude Code 在 brainstorm 阶段逐一向我确认）

1. 公告供应商最终选谁？接口返回格式是什么？（影响 L5 适配器）
2. BM25 用 PostgreSQL 内置全文（中文分词方案？）还是外置索引？
3. Agent 编排用 LangGraph 还是自写状态机？
4. 嵌入模型本地部署还是走 API？维度定 1024？
5. 起步是否需要 Web 前端，还是先用 Markdown 邮件/飞书推送简报？
6. 观点评分的自动化规则（如何定义"事后走向"）？
7. 私有化部署的最早时点，是否影响 P1 的架构选择？

## 13. 术语表

- **as_of / 时点**：查询或分析所假设的"当前时刻"，只能看到 `known_at ≤ as_of` 的数据
- **known_at**：系统首次获知该数据的时间；**valid_from**：数据描述的期间起点；**superseded_at**：被修正的时间
- **块 / block**：文档切分后的最小检索单元
- **传导规则**：某环节事件对其他环节影响的方向、时滞、机制
- **观点 / opinion**：Agent 生成的带依据链与置信度的判断
- **外壳层**：包裹外部工具的自有层，做校验、日志、缓存、限流
