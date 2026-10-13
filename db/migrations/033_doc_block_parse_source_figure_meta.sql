-- 迁移 033：core.doc_block.parse_source / figure_meta（L0 + TextIn 分页接入计划 T3）。
-- 编号说明：028（解析分级路由计划预留的 parse_page_route）与 029（确定性排版第二版计划预留的
-- figure_meta）从未落地；两者的设计并入 033 / 034，028 / 029 永久留空。
--
-- parse_source：块正文由谁解析。'local' = L0 确定性排版（parse/local/l0.py）；'textin' = 合合信息
-- TextIn；'mixed' = 父块的成员来自两种来源。NULL = 本迁移之前写入的存量块（整份 TextIn），
-- 评测里归为 legacy。只用于评测分组与审计，不参与检索打分。
-- figure_meta：L0 页图表图片元数据（parse/figures.py::figure_to_dict）。图片只存不解读，
-- 不送模型、不 OCR；只允许出现在 figure 块上。
-- 两列随 doc_block 既有时点列走，不登记 core.bitemporal_registry。asof.doc_block 是 SELECT *，
-- 加列后必须按 010 的现行定义原样重建（010 的注释说明了这个坑）。

ALTER TABLE core.doc_block ADD COLUMN parse_source text;
ALTER TABLE core.doc_block ADD CONSTRAINT doc_block_parse_source_known
  CHECK (parse_source IS NULL OR parse_source IN ('local', 'textin', 'mixed'));

ALTER TABLE core.doc_block ADD COLUMN figure_meta jsonb;
ALTER TABLE core.doc_block ADD CONSTRAINT doc_block_figure_meta_only_figure
  CHECK (figure_meta IS NULL OR block_type = 'figure');

COMMENT ON COLUMN core.doc_block.parse_source IS
  '块正文的解析来源：local（L0）/ textin / mixed（父块）；NULL = 迁移 033 之前的整份 TextIn 存量块';
COMMENT ON COLUMN core.doc_block.figure_meta IS
  '图表图片元数据（parse/figures.py FigureRef）；图片只存不解读，不送模型、不 OCR';

CREATE OR REPLACE VIEW asof.doc_block AS
SELECT * FROM core.doc_block
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());
