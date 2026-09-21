-- 来源：docs/02-data-model.md §5.1、docs/05-document-pipeline.md §7.2。
-- P1b Task 6 首次跑通重解析路径时抓到的缺口：改动需同步这两份文档。
--
-- document_dedup_uk 原本是普通唯一索引 UNIQUE (source, content_hash)，
-- 覆盖全部行，不分是否已被取代。重解析（05 §7.2：新增一行、supersedes_doc_id
-- 指向旧行，不原地替换）写入的新行与旧行 content_hash 完全相同——原始字节
-- 没变，变的只是切块/解析结果——于是新行的 INSERT 直接撞上旧行，报
-- UniqueViolation，重解析这条路径从一开始就不可能走通。
--
-- 与 003_facts.sql 的 fin_fact_live_uk 是同一个模式：把普通唯一索引改成
-- 只约束「活着」的行（WHERE superseded_at IS NULL），历史行不参与唯一性
-- 判断。任意时刻至多一行「活」着仍然成立——去重的本意就是防止同一份
-- 原始文档被反复正常入库，而不是禁止它被合法地重新解析。

DROP INDEX core.document_dedup_uk;

CREATE UNIQUE INDEX document_dedup_uk ON core.document (source, content_hash)
  WHERE superseded_at IS NULL;
