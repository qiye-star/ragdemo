-- 迁移 032：core.doc_table 按块查找的非偏索引（表格处理改进计划 T9，T8 评审遗留项）。
--
-- doc_table_live_uk 是偏索引（WHERE superseded_at IS NULL）。asof.doc_table 视图的谓词是
-- "known_at <= as_of AND (superseded_at IS NULL OR superseded_at > as_of)"，它并不蕴含
-- "superseded_at IS NULL"（as_of 之后才被取代的旧版也必须可见），规划器因此不能用那个偏索引，
-- load_table(block_id) 会退化成全表扫描。这里给 (block_id, known_at) 建一个不带谓词的索引；
-- 同一块的历史版本很少，索引过滤后再用视图谓词判断 superseded_at 即可。
-- 031 保留给 core.fact_table_ref（T18），故本迁移取 032。

CREATE INDEX doc_table_block_known ON core.doc_table (block_id, known_at);
