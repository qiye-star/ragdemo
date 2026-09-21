-- 来源：docs/02-data-model.md §1.4、§1.5。改动需同步该文档。
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector，向量检索
CREATE EXTENSION IF NOT EXISTS pg_search;   -- ParadeDB，BM25
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- digest()，审计哈希链
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- 别名模糊匹配

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS asof;
CREATE SCHEMA IF NOT EXISTS evals;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS orchestration;

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
