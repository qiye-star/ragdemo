-- 来源：docs/02-data-model.md §5、docs/05-document-pipeline.md §2-§5，
-- docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 C。
-- 改动需同步这两份文档。

-- tokens 列名有误导性：从 004_documents.sql 起写入的从来都是
-- len(chunk.content)（字符数），不是任何分词器算出的 token 数——
-- 代码库里没有分词逻辑（ingest/documents.py 的 _insert_blocks）。
-- 数据底座方案 §2.3 的块长验收线按中文字符计，沿用这个名字会让验收查询
-- 看起来在测一个不存在的指标。改名不改语义，存量值原样保留（本来就是
-- 字符数，不需要数据回填）。
--
-- asof.doc_block（006_asof_views_and_roles.sql）用 SELECT * 展开时把列名
-- 锁死在视图定义里，重命名基表列不会自动跟着改视图输出列名——PG 直接拒绝
-- ALTER TABLE ... RENAME COLUMN，报错提示要先改视图那一侧。CREATE OR
-- REPLACE VIEW 在这里帮不上忙：REPLACE 只允许在末尾追加列，不允许改名，
-- 必须用 ALTER VIEW ... RENAME COLUMN。
ALTER VIEW  asof.doc_block  RENAME COLUMN tokens TO char_len;
ALTER TABLE core.doc_block  RENAME COLUMN tokens TO char_len;

-- parse_confidence：四项加权（字符覆盖率/表格闭合率/乱码比例/页面缺失率），
-- 见 parse/confidence.py 模块 docstring。document 是整份文档的分；
-- doc_block 是块所在页的分（查不到该页的分就回退用 document 的整体分，
-- 见 ingest/documents.py）。两列都允许 NULL——路径 A（供应商结构化接口）
-- 早于这次迁移写入的存量行、以及不经过 DocumentWriter 的测试夹具都没有
-- 这个值，NULL 就是"未评分"，不是 0 分。
ALTER TABLE core.document  ADD COLUMN parse_confidence numeric;
ALTER TABLE core.doc_block ADD COLUMN parse_confidence numeric;

-- table_html：表格块的结构化形态（双形态之二，content/content_desc 是
-- 自然语言那一半）。只有 block_type = 'table' 且供应商给出了 cells[] 时
-- 才非空，供第 7 层做加总校验，不参与向量检索。
ALTER TABLE core.doc_block ADD COLUMN table_html text;

-- chunking_version：切块参数的指纹（parse/config.py 的 ChunkConfig.version），
-- 写入时刻固定。允许 NULL：不经过 DocumentWriter._insert_blocks 的测试
-- 夹具（比如 embed/batch.py 的测试，直接手写 INSERT 验证嵌入逻辑，与切块
-- 参数无关）没有理由被要求补一个跟测试目的无关的值。
--
-- 重切同一份文档时，走既有的 reparse_document 机制——新 doc_id，
-- supersedes_doc_id 指向旧文档，旧块永远不被删除或修改（008_document_
-- dedup_partial.sql 的部分唯一索引已经支持这个模式）。chunking_version
-- 只是让"这批块用的是哪版参数切出来的"可查，不需要额外调整
-- doc_block_position_uk 的范围——(doc_id, ordinal) 天然不会跨 doc_id 冲突。
ALTER TABLE core.doc_block ADD COLUMN chunking_version text;

-- embedding_version：写入切块结果时不知道——嵌入是切块之后才发生的独立
-- 步骤，由 embed_pending_blocks 算出向量那一刻回填为 embedder.model
-- （embed/batch.py）。允许 NULL：父块（is_leaf = false）永远不被嵌入，
-- 恒为 NULL；叶子块在轮到嵌入之前也是 NULL，这是正常的"还没处理"状态，
-- 不是数据缺陷。
ALTER TABLE core.doc_block ADD COLUMN embedding_version text;

-- 视图必须重建（007/009 已经写明这个陷阱：CREATE OR REPLACE VIEW 只允许
-- 在末尾追加列，ALTER TABLE ... ADD COLUMN 天然满足；RENAME COLUMN 不改变
-- 列的相对顺序，同样不受这条限制影响）。
CREATE OR REPLACE VIEW asof.document AS
SELECT * FROM core.document
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE OR REPLACE VIEW asof.doc_block AS
SELECT * FROM core.doc_block
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());
