-- 来源：docs/02-data-model.md §5、docs/05-document-pipeline.md §5.3。改动需同步这两份文档。

-- §5.1 owner_tenant / owner_user 同时为 NULL 表示公共空间。
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
  parse_engine      text,                   -- vendor:<name> / mineru:<version>
  page_count        int,
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
CREATE INDEX document_owner    ON core.document (owner_tenant, owner_user)
  WHERE owner_tenant IS NOT NULL OR owner_user IS NOT NULL;

-- §5.2 / §5.3 entity_id / doc_type / publish_at / owner_* 从 document 反规范化而来：
-- BM25 与 HNSW 索引都只能看见自己所在表的列，过滤条件若需 JOIN 才能求值就无法下推，
-- 时点过滤后召回会塌陷（docs/06-retrieval.md §3）。
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

-- §5.4 向量索引。维度固定 1024 见 adr/0004；vector_cosine_ops 要求写入前 L2 归一化。
CREATE INDEX doc_block_embedding_hnsw
  ON core.doc_block USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE superseded_at IS NULL;

-- §5.5 BM25 索引。
-- entity_id / doc_type 必须用 keyword 而非 raw：raw 默认小写化 token，
-- 会让 paradedb.term('entity_id','CN.688256') 静默匹配 0 行。
-- 不要加 datetime_fields：pg_search v0.24.1 起该选项已废弃且完全无效。
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

-- docs/05-document-pipeline.md §5.3
-- 主键三列缺一不可：去掉 model，换模型后会静默读到旧向量；去掉 owner_user，
-- 私有文档的向量会漏进公共缓存（哈希命中探测攻击，docs/09 §3.3）。
-- 用 '' 而不是 NULL 是因为主键列不能为 NULL。
CREATE TABLE core.embedding_cache (
  content_hash text NOT NULL,
  model        text NOT NULL,
  owner_user   text NOT NULL DEFAULT '',   -- '' = 公共内容；其余为用户私有分区
  embedding    vector(1024) NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (content_hash, model, owner_user)
);
