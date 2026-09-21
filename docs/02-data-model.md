# 02 · 数据模型与数据字典

本文档的 DDL 可直接执行。验证方式见 `10-roadmap.md` P0 验收项。

## 1. 总体约定

### 1.1 Schema 划分

| Schema | 内容 | 应用角色权限 |
|---|---|---|
| `core` | 实体、指标、事实、文档、观点 —— 全部基表 | **仅 `INSERT`，无 `SELECT`** |
| `asof` | 时点安全视图（每张时点表一个） | `SELECT` |
| `evals` | 三套评测集、修正记录 | `SELECT` / `INSERT` / `UPDATE` |
| `audit` | `tool_call_log` 哈希链 | `INSERT` + 受限 `SELECT` |
| `orchestration` | LangGraph 检查点 | 全权限（框架自管） |
| `dagster` | Dagster 元数据 | 全权限（框架自管） |

`core` 不授予 `SELECT` 是有意的：它在数据库层面强制「所有读取都经过 `as_of` 过滤」，
让时点泄漏从「靠自觉」变成「物理上做不到」。详见 `03-point-in-time.md` §4。

**本文档之外的表**（定义在各自的专题文档中，但同属 `core` / `asof` schema）：

| 表 | 定义位置 |
|---|---|
| `core.entity_resolution_queue` | `04-ingestion.md` §4 |
| `core.embedding_cache` | `05-document-pipeline.md` §5.3 |
| `asof.*` 七个时点视图 | `03-point-in-time.md` §4.1 |

建库脚本（`db/migrations/`）需要把这些一并包含，本文档不是 DDL 的唯一来源。

### 1.2 主键类型（相对简报的调整）

简报把 `doc_id` / `block_id` / `opinion_id` / `event_id` 写成 `PK` 未指定类型。本文档定为
`bigint GENERATED ALWAYS AS IDENTITY`，原因有二：

1. **`pg_search` 的 `key_field` 对整数类型支持最稳**，不随扩展版本变化；
2. `doc_block` 是全库最大的表，`evidence_blocks` 数组会在 `opinion` 中大量重复存储，
   `bigint` 比 `uuid` 省一半空间、索引更快。

**引用稳定性**：`block_id` 一旦写入永不复用、永不删除。重新解析同一份文档会产生
新的 `document` 版本（`version_group_id` 相同、`supersedes_doc_id` 指向旧版），
旧版的块原样保留，因此历史观点里的引用永远不会悬空。

### 1.3 时点公共字段

以下字段出现在所有**时点表**（`fin_fact`、`price_daily`、`document`、`doc_block`、
`entity_relation`、`entity_node_membership`、`event`）中，语义见 `03-point-in-time.md`：

```
valid_from    date         NOT NULL   -- 数据描述的期间起点
known_at      timestamptz  NOT NULL   -- 现实中最早可获知该数据的时刻（不是入库时间）
superseded_at timestamptz  NULL       -- 被更正版本取代的时刻；NULL 表示当前有效
source        text         NOT NULL   -- 供应商 / 渠道标识
source_ref    text         NULL       -- 供应商侧主键、URL 或 provider_snapshot 引用
ingest_run_id text         NOT NULL   -- Dagster run id，用于追溯与回滚
ingested_at   timestamptz  NOT NULL DEFAULT now()  -- 入库时间，仅供运维，禁止进入 as_of 过滤
```

一致性由 `core.bitemporal_registry` 登记 + CI 测试强制（见 §9）。

### 1.4 扩展与 Schema 初始化

```sql
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector，向量检索
CREATE EXTENSION IF NOT EXISTS pg_search;   -- ParadeDB，BM25
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- digest()，审计哈希链
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- 别名模糊匹配

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS asof;
CREATE SCHEMA IF NOT EXISTS evals;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS orchestration;
```

### 1.5 枚举类型

```sql
CREATE TYPE core.entity_type       AS ENUM ('listed','private','institution','government','index');
CREATE TYPE core.entity_status     AS ENUM ('active','suspended','delisted','merged','pre_ipo');
CREATE TYPE core.relation_type     AS ENUM ('supplies_to','customer_of','competes_with',
                                            'invests_in','depends_on','substitutes');
CREATE TYPE core.metric_role       AS ENUM ('leading','confirming');
CREATE TYPE core.block_type        AS ENUM ('paragraph','table','figure','title');
CREATE TYPE core.opinion_direction AS ENUM ('bull','bear','neutral');
CREATE TYPE core.score_horizon     AS ENUM ('1M','3M');
CREATE TYPE core.score_track       AS ENUM ('price','evidence');
CREATE TYPE core.scorer            AS ENUM ('auto','human');
CREATE TYPE core.confidence        AS ENUM ('high','medium','low','inferred');
```

---

## 2. 实体层

### 2.1 `core.entity`

```sql
CREATE TABLE core.entity (
  entity_id       text PRIMARY KEY
                  CHECK (entity_id ~ '^[A-Z]{2}[.][A-Za-z0-9._-]{1,32}$'),
  name_full       text NOT NULL,
  name_short      text,
  name_en         text,
  entity_type     core.entity_type NOT NULL,
  market          text,                       -- SSE/SZSE/BSE/HKEX/NASDAQ/NYSE/TWSE/KRX
  tushare_code    text UNIQUE,
  ifind_code      text,
  wind_code       text,
  edgar_cik       text,
  l1_layer        text NOT NULL,              -- 算力 / 模型 / 应用 / 数据 / 能源 ...
  l2_segment      text NOT NULL,
  l3_node         text[] NOT NULL DEFAULT '{}',
  primary_node    text NOT NULL,
  ai_revenue_pct  numeric(5,4) CHECK (ai_revenue_pct BETWEEN 0 AND 1),
  ai_revenue_src  text,
  hq_country      text,
  status          core.entity_status NOT NULL DEFAULT 'active',
  listed_date     date,
  currency        char(3),                    -- 主要报告币种
  fiscal_year_end smallint CHECK (fiscal_year_end BETWEEN 1 AND 12),
  notes           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT entity_primary_node_in_l3 CHECK (primary_node = ANY (l3_node))
);

CREATE INDEX entity_primary_node_idx ON core.entity (primary_node);
CREATE INDEX entity_l3_node_gin      ON core.entity USING gin (l3_node);
CREATE INDEX entity_status_idx       ON core.entity (status) WHERE status = 'active';
```

`entity_id` 格式为 `<两位市场国别码>.<代码>`，例如 `CN.688256`、`US.NVDA`、`TW.2330`。

**`entity` 不是时点表**，它是缓变描述表，存「当前」画像。这带来两个必须注意的地方：

- **`ai_revenue_pct` 与 `l3_node` 只用于人读与筛选，禁止进入任何加权计算或回测基准构造。**
  用今天的 AI 收入占比去给去年的观点加权是前视偏差。计算必须走 §2.4 的历史表与
  `fin_fact` 中 `metric_id = 'ai_revenue_pct'` 的时点记录。
- `listed_date` / `fiscal_year_end` / `currency` 是简报未列但必需的字段：
  `listed_date` 用于观点评分中「剔除上市不足 60 交易日」，后两者用于口径归一。

### 2.2 `core.entity_alias`

```sql
CREATE TABLE core.entity_alias (
  alias       text NOT NULL,
  entity_id   text NOT NULL REFERENCES core.entity ON DELETE CASCADE,
  alias_type  text NOT NULL,     -- full / short / en / ticker / product / former / nickname
  source      text NOT NULL,
  confidence  core.confidence NOT NULL DEFAULT 'high',
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (alias, entity_id)
);

CREATE INDEX entity_alias_entity_idx ON core.entity_alias (entity_id);
CREATE INDEX entity_alias_trgm       ON core.entity_alias USING gin (alias gin_trgm_ops);
```

`alias` 不设全局唯一约束——「中兴」「长城」这类别名天然一对多，消歧由
`src/entities/` 的上下文规则处理，见 `04-ingestion.md` §4。

### 2.3 `core.entity_relation`（时点表）

```sql
CREATE TABLE core.entity_relation (
  relation_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  from_entity     text NOT NULL REFERENCES core.entity,
  to_entity       text NOT NULL REFERENCES core.entity,
  relation_type   core.relation_type NOT NULL,
  strength        numeric(3,2) CHECK (strength BETWEEN 0 AND 1),
  share_estimate  numeric(5,4) CHECK (share_estimate BETWEEN 0 AND 1),
  direction_note  text,
  evidence_block  bigint[] NOT NULL DEFAULT '{}',
  evidence_type   text,           -- announcement / filing / news / expert / inferred
  valid_from      date NOT NULL,
  valid_to        date,
  known_at        timestamptz NOT NULL,
  superseded_at   timestamptz,
  source          text NOT NULL,
  source_ref      text,
  ingest_run_id   text NOT NULL,
  ingested_at     timestamptz NOT NULL DEFAULT now(),
  confidence      core.confidence NOT NULL DEFAULT 'medium',
  CONSTRAINT relation_no_self CHECK (from_entity <> to_entity),
  CONSTRAINT relation_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);

CREATE UNIQUE INDEX entity_relation_live_uk
  ON core.entity_relation (from_entity, to_entity, relation_type, valid_from)
  WHERE superseded_at IS NULL;

CREATE INDEX entity_relation_from ON core.entity_relation (from_entity, relation_type, known_at DESC);
CREATE INDEX entity_relation_to   ON core.entity_relation (to_entity,   relation_type, known_at DESC);
```

`share_estimate` 是产业链映射 Agent 的加权因子（`01-architecture.md` §2.3）。
`evidence_block` 为空数组且 `confidence = 'inferred'` 的关系不得用于对外输出的加权，
只能作为线索。

### 2.4 `core.entity_node_membership`（时点表）

简报未包含此表，但观点评分的基准是「L3 环节等权组合」
（[adr/0007](adr/0007-opinion-scoring-dual-track.md)）。如果环节成分只存当前值，
回溯修改某公司的环节归属会**静默改变所有历史观点的基准收益**，评分不可复现。

```sql
CREATE TABLE core.entity_node_membership (
  membership_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id     text NOT NULL REFERENCES core.entity,
  l3_node       text NOT NULL,
  is_primary    boolean NOT NULL DEFAULT false,
  valid_from    date NOT NULL,
  known_at      timestamptz NOT NULL,
  superseded_at timestamptz,
  source        text NOT NULL,          -- 通常是 'manual:<author>'
  source_ref    text,
  ingest_run_id text NOT NULL,
  ingested_at   timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX entity_node_membership_live_uk
  ON core.entity_node_membership (entity_id, l3_node)
  WHERE superseded_at IS NULL;

CREATE INDEX entity_node_membership_node ON core.entity_node_membership (l3_node, known_at DESC);
```

`core.entity.l3_node` 与本表的关系：前者是后者的「当前值」物化缓存，只用于展示与筛选。

---

## 3. 指标与规则层

```sql
CREATE TABLE core.node_metric (
  metric_id       text PRIMARY KEY CHECK (metric_id ~ '^[a-z][a-z0-9_]{2,63}$'),
  l3_node         text,
  metric_name     text NOT NULL,
  metric_role     core.metric_role NOT NULL,
  frequency       text NOT NULL,        -- daily / monthly / quarterly / annual / irregular
  source_type     text NOT NULL,        -- filing / announcement / vendor_api / ir_page / derived
  source_entity   text[] NOT NULL DEFAULT '{}',
  extraction_hint text,                 -- 给抽取 Agent 的定位提示
  direction       smallint NOT NULL DEFAULT 1 CHECK (direction IN (-1, 1)),
  definition      text NOT NULL,        -- 口径定义，必填
  unit            text NOT NULL,        -- CNY / USD / pcs / pct / ratio / x
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.metric_source_map (
  metric_id      text NOT NULL REFERENCES core.node_metric,
  provider       text NOT NULL,
  provider_field text NOT NULL,
  priority       smallint NOT NULL DEFAULT 100,
  scale_factor   numeric NOT NULL DEFAULT 1,   -- 供应商单位 -> 本系统单位
  notes          text,
  PRIMARY KEY (metric_id, provider, provider_field)
);

CREATE TABLE core.ai_revenue_rule (
  rule_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      text NOT NULL REFERENCES core.entity,
  rule_text      text NOT NULL,
  source_type    text NOT NULL,     -- segment_report / management_guidance / analyst / estimate
  confidence     core.confidence NOT NULL,
  author         text NOT NULL,
  effective_from date NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.propagation_rule (
  rule_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  trigger_node   text NOT NULL,
  trigger_event  text NOT NULL,
  affected_node  text NOT NULL,
  direction      smallint NOT NULL CHECK (direction IN (-1, 0, 1)),
  lag_days       int NOT NULL DEFAULT 0 CHECK (lag_days >= 0),
  mechanism      text NOT NULL,     -- 机制说明，必填，进入输出的解释部分
  weight_field   text,              -- 加权依据字段名，如 'share_estimate'
  confidence     core.confidence NOT NULL,
  evidence_block bigint[] NOT NULL DEFAULT '{}',
  author         text NOT NULL,
  enabled        boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX propagation_rule_trigger
  ON core.propagation_rule (trigger_node, trigger_event) WHERE enabled;
```

`direction` 在 `node_metric` 中表示「该指标上升是利好(1)还是利空(-1)」，
在 `propagation_rule` 中表示传导方向。`mechanism` 必填是刻意的：没有机制说明的规则
无法进入输出，因为输出必须能解释「为什么」。

---

## 4. 时点事实层

### 4.1 `core.fin_fact`

```sql
CREATE TABLE core.fin_fact (
  fact_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id     text NOT NULL REFERENCES core.entity,
  metric_id     text NOT NULL REFERENCES core.node_metric,
  period        text NOT NULL,            -- '2024Q3' / '2024FY' / '2024-08'
  period_end    date NOT NULL,            -- 期间结束日，用于排序与区间查询
  value         numeric NOT NULL,
  unit          text NOT NULL,
  currency      char(3),
  valid_from    date NOT NULL,
  known_at      timestamptz NOT NULL,
  superseded_at timestamptz,
  source        text NOT NULL,
  source_ref    text,
  source_block  bigint,                   -- 若抽取自文档，指向 doc_block
  ingest_run_id text NOT NULL,
  ingested_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fin_fact_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);

CREATE UNIQUE INDEX fin_fact_live_uk
  ON core.fin_fact (entity_id, metric_id, period)
  WHERE superseded_at IS NULL;

CREATE INDEX fin_fact_lookup   ON core.fin_fact (entity_id, metric_id, period_end DESC, known_at DESC);
CREATE INDEX fin_fact_known_at ON core.fin_fact (known_at);
CREATE INDEX fin_fact_metric   ON core.fin_fact (metric_id, period_end DESC);
```

`fin_fact_live_uk` 是核心约束：同一 (实体, 指标, 期间) 在任一时刻只能有一行「当前有效」。
更正必然表现为「给旧行打 `superseded_at` + 插入新行」，写入中间件之外无法做到。

`ai_revenue_pct` 作为一个 `metric_id` 存在这里，这样它就自动具备时点语义（见 §2.1）。

### 4.2 `core.price_daily`

```sql
CREATE TABLE core.price_daily (
  entity_id     text NOT NULL REFERENCES core.entity,
  trade_date    date NOT NULL,
  open          numeric,
  high          numeric,
  low           numeric,
  close         numeric NOT NULL,
  pre_close     numeric,
  volume        numeric,
  amount        numeric,
  adj_factor    numeric NOT NULL DEFAULT 1,
  is_suspended  boolean NOT NULL DEFAULT false,
  valid_from    date NOT NULL,
  known_at      timestamptz NOT NULL,
  superseded_at timestamptz,
  source        text NOT NULL,
  source_ref    text,
  ingest_run_id text NOT NULL,
  ingested_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_id, trade_date, known_at)
);

CREATE UNIQUE INDEX price_daily_live_uk
  ON core.price_daily (entity_id, trade_date)
  WHERE superseded_at IS NULL;

CREATE INDEX price_daily_date ON core.price_daily (trade_date, entity_id);
```

**`adj_factor` 是本表需要双时间轴的主要原因**：每次除权除息，供应商都会回溯调整全部历史
复权因子。如果直接 UPDATE，三个月前算出的观点分数今天重算就会变——评分不可复现。
按更正流程写新行则历史可完整还原。

`known_at` 取 `trade_date` 当地收盘后的固定时刻（A 股 15:30 CST，美股 16:30 ET），
不取数据拉取时间。理由见 `03-point-in-time.md` §1.3。

### 4.3 `core.provider_snapshot`

```sql
CREATE TABLE core.provider_snapshot (
  snapshot_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider      text NOT NULL,
  endpoint      text NOT NULL,
  params        jsonb NOT NULL,
  response      jsonb,
  response_ref  text,                -- 超过 1MB 的响应存对象存储，这里放 key
  http_status   int,
  fetched_at    timestamptz NOT NULL DEFAULT now(),
  ingest_run_id text NOT NULL,
  cost_cents    numeric(10,4)
);

CREATE INDEX provider_snapshot_lookup ON core.provider_snapshot (provider, endpoint, fetched_at DESC);
CREATE INDEX provider_snapshot_params ON core.provider_snapshot USING gin (params);
```

原始响应永不修改、永不删除。任何「数据对不对」的争议都回到这张表重放。

---

## 5. 文档层

### 5.1 `core.document`

```sql
CREATE TABLE core.document (
  doc_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id         text REFERENCES core.entity,        -- 政策/宏观类文档可为 NULL
  doc_type          text NOT NULL,   -- annual_report / quarterly / announcement / 10-K /
                                     -- 10-Q / 8-K / transcript / policy / news / user_upload
  title             text NOT NULL,
  period            text,
  publish_at        timestamptz NOT NULL,   -- 文档在现实中发布的时刻
  language          char(2) NOT NULL DEFAULT 'zh',
  source            text NOT NULL,
  source_url        text,
  raw_ref           text,                   -- 原始 PDF 的对象存储 key
  content_hash      text NOT NULL,          -- 原始字节 sha256，用于去重
  version_group_id  bigint NOT NULL,        -- 同一份文档的所有版本共享
  is_correction     boolean NOT NULL DEFAULT false,
  supersedes_doc_id bigint REFERENCES core.document,
  parse_engine      text,                   -- vendor:<name> / textin:<版本>+<参数指纹>
  page_count        int,
  -- 以下三列由迁移 007_parse_artifacts.sql 追加（adr/0008）。
  -- 解析产物存对象存储、表里只留 key，与 §4 的 provider_snapshot.response_ref 同理：
  -- 一份 500 页年报的解析响应可达数十 MB，进 jsonb 会 TOAST 到拖垮 pg_dump，
  -- 而它从来不参与关系查询。
  parse_json_ref    text,                   -- xParse 完整响应 JSON 的对象存储 key
  parse_md_ref      text,                   -- result.markdown 的对象存储 key
  parse_warnings    jsonb NOT NULL DEFAULT '[]'::jsonb,
  owner_tenant      text,                   -- 多租户隔离；NULL = 公共空间
  owner_user        text,                   -- 用户私有空间；NULL = 非私有
  valid_from        date NOT NULL,
  known_at          timestamptz NOT NULL,
  superseded_at     timestamptz,
  source_ref        text,
  ingest_run_id     text NOT NULL,
  ingested_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);

CREATE UNIQUE INDEX document_dedup_uk ON core.document (source, content_hash);
CREATE INDEX document_entity   ON core.document (entity_id, doc_type, publish_at DESC);
CREATE INDEX document_version  ON core.document (version_group_id, known_at DESC);
CREATE INDEX document_known_at ON core.document (known_at);
CREATE INDEX document_parse_engine ON core.document (parse_engine);   -- 007：按解析器分组评测
CREATE INDEX document_owner    ON core.document (owner_tenant, owner_user)
  WHERE owner_tenant IS NOT NULL OR owner_user IS NOT NULL;
```

`owner_tenant` / `owner_user` 同时为 NULL 表示公共空间。权限模型见
`09-compliance-security.md` §3。

`parse_json_ref` 的 key 形如 `parse/textin/<content_hash>/<param_fp>.json`，
同时是**成本缓存键**——调用按页计费的解析 API 前先查它是否存在
（`05-document-pipeline.md` §2.4）。

**给 `core.document` 加列必须同时重建 `asof.document`**：视图用 `SELECT *`，
列清单在 `CREATE VIEW` 时就固定了，新列不会自动出现。应用层只允许查 `asof` 视图
（CLAUDE.md §1.1），漏掉这一步的后果是新列在整个应用侧永远不可见——不报错，只是查不到。
迁移 007 用 `CREATE OR REPLACE VIEW` 重建（只允许在末尾追加列，正好够用，
且会保留 `GRANT`；`DROP` + 重建则不会）。

### 5.2 `core.doc_block`

```sql
CREATE TABLE core.doc_block (
  block_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  doc_id          bigint NOT NULL REFERENCES core.document,
  parent_block_id bigint REFERENCES core.doc_block,
  block_type      core.block_type NOT NULL,
  section_path    text NOT NULL DEFAULT '',   -- '第三节 主营业务 > 3.2 分部收入'
  ordinal         int NOT NULL,               -- 文档内顺序，用于还原上下文
  page            int,
  bbox            numeric[4],
  content         text NOT NULL,
  content_desc    text,                       -- 表格/图的一句自然语言描述
  embedding       vector(1024),
  tokens          int,
  is_leaf         boolean NOT NULL,           -- 叶子块（可被召回）；父块为 false
  -- 以下四列从 document 反规范化而来，用途见 §5.3
  entity_id       text REFERENCES core.entity,
  doc_type        text NOT NULL,
  publish_at      timestamptz NOT NULL,
  owner_tenant    text,
  owner_user      text,
  valid_from      date NOT NULL,
  known_at        timestamptz NOT NULL,
  superseded_at   timestamptz,
  source          text NOT NULL,
  source_ref      text,
  ingest_run_id   text NOT NULL,
  ingested_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT doc_block_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at),
  CONSTRAINT doc_block_no_self_parent CHECK (parent_block_id IS DISTINCT FROM block_id)
);

CREATE UNIQUE INDEX doc_block_position_uk ON core.doc_block (doc_id, ordinal);
CREATE INDEX doc_block_doc_section ON core.doc_block (doc_id, section_path);
CREATE INDEX doc_block_parent      ON core.doc_block (parent_block_id) WHERE parent_block_id IS NOT NULL;
CREATE INDEX doc_block_known_at    ON core.doc_block (known_at);
```

`is_leaf` 区分「可被召回的叶子块」与「只在展开阶段使用的父块」。它不能用
`parent_block_id IS NOT NULL` 代替：表格块既是父块也是叶子块
（`05-document-pipeline.md` §4.2），两种判定在表格上结论相反。检索侧依赖这一列，
见 `06-retrieval.md` §2。

### 5.3 为什么 `doc_block` 要反规范化 `entity_id` / `doc_type` / `publish_at` / owner

简报把这些字段只放在 `document` 上。但检索时的过滤条件几乎全落在它们身上，而
**BM25 索引与 HNSW 索引都只能看见自己所在表的列**——过滤条件如果需要 JOIN
`document` 才能求值，就无法下推进索引扫描，退化成「先取 top-k、再过滤」，
时点过滤后召回会塌陷（见 `06-retrieval.md` §3）。

代价是这五列在 `doc_block` 中冗余。可接受，因为：文档一旦入库这些值就不再变化
（变化意味着新版本、新块），不存在更新不一致的窗口。写入中间件负责填充，
CI 中有一致性校验测试。

### 5.4 向量索引

```sql
CREATE INDEX doc_block_embedding_hnsw
  ON core.doc_block USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE superseded_at IS NULL;
```

`WHERE superseded_at IS NULL` 的部分索引服务于最常见的「查当前」场景。
回测查历史时点时另有处理，见 `06-retrieval.md` §3。

维度固定 1024 见 [adr/0004](adr/0004-hosted-embedding-reranker-api-1024d.md)。
`vector_cosine_ops` 要求写入前对向量做 L2 归一化（`05-document-pipeline.md` §5）。

### 5.5 BM25 索引

```sql
CREATE INDEX doc_block_bm25 ON core.doc_block
USING bm25 (block_id, content, content_desc, section_path,
            entity_id, doc_type, is_leaf, known_at, superseded_at, publish_at)
WITH (
  key_field = 'block_id',
  text_fields = '{
    "content":      {"tokenizer": {"type": "chinese_lindera"}, "record": "position"},
    "content_desc": {"tokenizer": {"type": "chinese_lindera"}, "record": "position"},
    "section_path": {"tokenizer": {"type": "chinese_lindera"}, "record": "position"},
    "entity_id":    {"tokenizer": {"type": "keyword"}, "fast": true},
    "doc_type":     {"tokenizer": {"type": "keyword"}, "fast": true}
  }',
  boolean_fields = '{ "is_leaf": {"fast": true} }'
);
```

把 `entity_id` / `doc_type` / `is_leaf` / `known_at` / `superseded_at` 列进索引，
是为了让过滤在 tantivy 内部完成而不是回表后过滤——这是 BM25 一路能保证
「时点过滤后仍有足够召回」的关键。

时间戳列**不需要**（也不应该）写 `datetime_fields` 选项：`pg_search` 自 v0.24.1 起
该选项已废弃且完全无效，其性能优化默认开启。写上去只会得到一条 WARNING
和一份误导后人的配置。列出现在索引字段列表里就够了。

**`entity_id` / `doc_type` 必须用 `keyword` 而不是 `raw`。** `raw` 分词器
**默认会把 token 小写化**，而 `entity_id` 的格式是大写开头的
`CN.688256` / `US.NVDA`。用 `raw` 时 `paradedb.term('entity_id', 'CN.688256')`
会匹配 0 行，而 `'cn.688256'` 匹配全部——**查询不报错，只是静默返回空结果**。
这是实测发现的陷阱（见下方验证记录）。`keyword` 分词器做精确的大小写敏感匹配，
是标识符字段的正确选择。

> **已验证**：本段 DDL 在 **ParadeDB `paradedb/paradedb:latest`（PostgreSQL 18.6,
> `pg_search` 0.25.9, `pgvector` 0.8.4）** 上实际执行通过，
> `chinese_lindera` 分词器、`boolean_fields` 选项、`keyword` 分词器均可用。
>
> 尽管如此，`infra/docker-compose.yml` 仍必须**固定镜像的具体版本号**——
> `pg_search` 的索引选项语法在版本间变动过。升级镜像时重跑 P0 的建库脚本，
> 以实测为准更新本段。中文分词器若在目标版本不可用，退到 `chinese_compatible`
> （按字切分，召回更高、精度略低）。

---

## 6. 观点与事件层

```sql
CREATE TABLE core.event (
  event_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_type        text NOT NULL,     -- capex_guidance / capacity_expansion / export_control /
                                       -- order_win / product_launch / earnings / policy ...
  trigger_entity    text REFERENCES core.entity,
  trigger_node      text,
  summary           text NOT NULL,
  publish_at        timestamptz NOT NULL,
  source_block      bigint REFERENCES core.doc_block,
  matched_rules     bigint[] NOT NULL DEFAULT '{}',
  affected_entities jsonb NOT NULL DEFAULT '[]',  -- [{entity_id, direction, weight, lag_days}]
  processed_at      timestamptz,
  valid_from        date NOT NULL,
  known_at          timestamptz NOT NULL,
  superseded_at     timestamptz,
  source            text NOT NULL,
  source_ref        text,
  ingest_run_id     text NOT NULL,
  ingested_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX event_type_time  ON core.event (event_type, publish_at DESC);
CREATE INDEX event_trigger    ON core.event (trigger_entity, publish_at DESC);
CREATE INDEX event_unprocessed ON core.event (known_at) WHERE processed_at IS NULL;
CREATE INDEX event_affected   ON core.event USING gin (affected_entities);

CREATE TABLE core.opinion (
  opinion_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id       text REFERENCES core.entity,
  l3_node         text,
  event_id        bigint REFERENCES core.event,
  direction       core.opinion_direction NOT NULL,
  confidence      core.confidence NOT NULL,
  thesis          text NOT NULL,
  evidence_blocks bigint[] NOT NULL DEFAULT '{}',
  evidence_facts  bigint[] NOT NULL DEFAULT '{}',
  counter_thesis  text,                -- 多空辩论中对立方的最强论据，必须保留
  agent_name      text NOT NULL,
  prompt_version  text NOT NULL,
  model           text NOT NULL,
  run_id          text NOT NULL,
  as_of           timestamptz NOT NULL,   -- 生成时假设的时点，评分起点
  confirmed_by    text,                   -- 人工确认者；NULL 表示未确认，不得对外
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT opinion_has_target CHECK (entity_id IS NOT NULL OR l3_node IS NOT NULL),
  CONSTRAINT opinion_has_evidence
    CHECK (cardinality(evidence_blocks) + cardinality(evidence_facts) > 0)
);

CREATE INDEX opinion_entity  ON core.opinion (entity_id, as_of DESC);
CREATE INDEX opinion_node    ON core.opinion (l3_node, as_of DESC);
CREATE INDEX opinion_event   ON core.opinion (event_id);
CREATE INDEX opinion_pending ON core.opinion (created_at) WHERE confirmed_by IS NULL;
```

`opinion_has_evidence` 在数据库层面强制「观点必须有依据」——这是合规硬约束
（`CLAUDE.md` §0）的最后一道防线。`counter_thesis` 是简报未列但必需的字段：
多空辩论产出的反方最强论据如果不落库，复盘时无法判断当初是否考虑过该风险。

`opinion` 不是时点表：它本身就是一个在 `as_of` 时刻产生的不可变事实，只追加不修改。

```sql
CREATE TABLE core.opinion_score (
  score_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  opinion_id   bigint NOT NULL REFERENCES core.opinion,
  track        core.score_track NOT NULL,     -- price | evidence
  horizon      core.score_horizon NOT NULL,   -- 1M | 3M
  scored_at    timestamptz NOT NULL,
  score        numeric(4,2) NOT NULL CHECK (score BETWEEN -2 AND 2),
  outcome_desc text NOT NULL,
  -- price 轨专用，全部由代码计算并留存以便复现
  stock_return    numeric,
  benchmark_return numeric,
  excess_return   numeric,
  benchmark_def   jsonb,        -- {node, constituents:[...], as_of, rule_version}
  -- evidence 轨专用
  evidence_blocks bigint[] NOT NULL DEFAULT '{}',
  scorer       core.scorer NOT NULL,
  scorer_name  text,
  UNIQUE (opinion_id, track, horizon)
);

CREATE INDEX opinion_score_lookup ON core.opinion_score (track, horizon, scored_at DESC);
```

两轨分开存、`UNIQUE (opinion_id, track, horizon)` 保证不合并——
这是 [adr/0007](adr/0007-opinion-scoring-dual-track.md) 的结构性体现。
`benchmark_def` 完整记录当次评分用的成分股清单，即使日后环节归属变化，
历史分数依然可复现。

---

## 7. 评测层

```sql
CREATE TABLE evals.eval_retrieval (
  q_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question      text NOT NULL,
  as_of         timestamptz NOT NULL,
  gold_block_ids bigint[] NOT NULL CHECK (cardinality(gold_block_ids) > 0),
  entity_filter text[],
  doc_type_filter text[],
  difficulty    text,        -- easy / medium / hard
  notes         text,
  author        text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE evals.eval_extraction (
  case_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  doc_id     bigint NOT NULL REFERENCES core.document,
  field      text NOT NULL,
  gold_value text NOT NULL,
  unit       text,
  gold_block_id bigint,     -- 正确答案所在块，同时评「找对地方」与「抄对数字」
  notes      text,
  author     text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (doc_id, field)
);

CREATE TABLE evals.eval_judgement (
  case_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scenario     text NOT NULL,
  as_of        timestamptz NOT NULL,
  input_ref    jsonb NOT NULL,           -- 事件 / 实体 / 文档的引用
  gold_direction core.opinion_direction NOT NULL,
  gold_reasoning text NOT NULL,
  acceptable_alternatives text[],
  author       text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE evals.eval_run (
  run_id       text PRIMARY KEY,
  suite        text NOT NULL,          -- retrieval / extraction / judgement
  git_sha      text NOT NULL,
  config       jsonb NOT NULL,         -- 检索参数、模型、prompt 版本
  metrics      jsonb NOT NULL,         -- {recall_at_10: 0.83, mrr: 0.71, ...}
  started_at   timestamptz NOT NULL,
  ended_at     timestamptz,
  cost_cents   numeric(10,4)
);

CREATE TABLE evals.correction_log (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_name      text NOT NULL,
  opinion_id      bigint REFERENCES core.opinion,
  original_output text NOT NULL,
  corrected_output text NOT NULL,
  reason          text NOT NULL,
  reason_category text,     -- wrong_number / missing_source / wrong_entity /
                            -- compliance / logic / stale_data
  corrector       text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX correction_log_category ON evals.correction_log (reason_category, created_at DESC);
```

`eval_judgement` 是简报「三套评测集」中的第三套（简报称「判断评测集」）的具体形态。
`eval_run` 是简报未列但 CI 门禁必需的表：没有历史 run 记录就无法判断指标是升是降。
`reason_category` 让修正记录可聚合——这是发现系统性缺陷的主要手段。

---

## 8. 审计层

```sql
CREATE TABLE audit.tool_call_log (
  call_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id      text NOT NULL,
  agent_name  text NOT NULL,
  tool_name   text NOT NULL,
  params      jsonb NOT NULL,
  as_of       timestamptz,          -- 该次调用使用的时点
  result_ref  text,                 -- 结果摘要或对象存储 key
  result_hash text,
  ok          boolean NOT NULL,
  error       text,
  tokens_in   int,
  tokens_out  int,
  cost_cents  numeric(10,4),
  started_at  timestamptz NOT NULL,
  ended_at    timestamptz NOT NULL,
  prev_hash   text NOT NULL,
  hash        text NOT NULL
);

CREATE UNIQUE INDEX tool_call_log_hash_uk ON audit.tool_call_log (hash);
CREATE INDEX tool_call_log_run  ON audit.tool_call_log (run_id, started_at);
CREATE INDEX tool_call_log_cost ON audit.tool_call_log (started_at) INCLUDE (cost_cents);
```

哈希链构造与校验见 `09-compliance-security.md` §4。本表**只允许 INSERT**，
由 `REVOKE UPDATE, DELETE` 强制。

---

## 9. 一致性强制

```sql
CREATE TABLE core.bitemporal_registry (
  table_name regclass PRIMARY KEY,
  note       text
);

INSERT INTO core.bitemporal_registry (table_name) VALUES
  ('core.fin_fact'), ('core.price_daily'), ('core.document'), ('core.doc_block'),
  ('core.entity_relation'), ('core.entity_node_membership'), ('core.event');
```

CI 中的测试 `tests/test_schema_invariants.py` 断言：

1. 登记表中的每张表都具备 §1.3 的全部七个公共字段，类型正确；
2. 每张表都有 `superseded_at > known_at` 的 CHECK 约束；
3. 每张表在 `asof` schema 下有同名视图（`03-point-in-time.md` §4）；
4. `doc_block` 的反规范化列与 `document` 一致（抽样 1000 行）；
5. 应用角色对 `core` schema **没有** `SELECT` 权限。

这五条任何一条失败即 CI 红灯。它们比文档更能防止时点语义被悄悄破坏。

---

## 10. 种子数据

P0 需要导入的手工数据（`db/seed/*.csv`）：

| 文件 | 目标表 | 数量 |
|---|---|---|
| `entity.csv` | `core.entity` + `core.entity_node_membership` | 100 |
| `entity_alias.csv` | `core.entity_alias` | 约 400（每家 3–5 个别名） |
| `entity_relation.csv` | `core.entity_relation` | 200 |
| `node_metric.csv` | `core.node_metric` | 30 |
| `propagation_rule.csv` | `core.propagation_rule` | 15 |

导入工具须做的事：校验 `primary_node ∈ l3_node`、别名不与其他实体全称冲突、
关系两端实体存在、为每行时点表记录填 `known_at`（手工数据取 `valid_from` 当日 00:00）。
