# ADR-0011 · `doc_block.entity_id` 暂维持单值，不改数组

- 状态：已接受
- 日期：2026-09-22
- 决策者：工程侧
- 影响：ADR-0001（ParadeDB `pg_search` BM25）

## 背景

《数据底座建设方案》§2.4 指出一个真实缺口：一篇公告可能同时涉及多家实体
（例如集团财报同时提及上市主体与被收购标的），而 `core.doc_block.entity_id`
目前是单值列，只能记一个实体。方案建议改成 `entity_ids`（数组）。

这条列不是普通字段。`004_documents.sql` 的注释写明了它存在的唯一理由：
`entity_id` / `doc_type` / `publish_at` / `known_at` 从 `document` 反规范化到
`doc_block` 上，是为了让检索的过滤条件能**下推进 BM25 与 HNSW 索引**——
过滤条件如果需要 JOIN `document` 才能求值，就无法下推，退化成「先取
top-k、再过滤」，时点过滤后召回会塌陷（`06-retrieval.md` §3）。`004_documents.sql`
的 BM25 索引定义里，`entity_id` 用的是 `"tokenizer": {"type": "keyword"}`
（而非 `raw`）——这个选择本身就是一次踩坑后的修正：`raw` 默认小写化 token，
会让 `paradedb.term('entity_id','CN.688256')` 静默匹配 0 行。

## 备选方案

**A. 改成 `entity_ids text[]`，重建 BM25 索引，用 `paradedb.term_set` 或
`@@@` 数组匹配语法做过滤**

**B. 保持单值 `entity_id`，多实体公告的"次要实体"关联关系走别处解决
（例如 `core.entity_relation`，或检索时按公告标题/正文做二次实体识别）**

**C. 维持现状，把"改数组"列为待验证项，不在没有实测的情况下动索引**

## 决策

**选 C。**

`pg_search`（tantivy 封装）对数组字段的下推行为，在本仓库里**从未被验证过**。
`keyword` 分词器 + `fast` 字段的组合已经被证明会在意料之外的地方产生静默错误
（上面提到的 `raw` 小写化陷阱就是先例）——数组字段是否也有类似的、只有跑起来
才会暴露的行为（例如 `@@@` 数组匹配是否真的对每个数组元素单独建 fast field、
是否影响 `paradedb.score()` 的可用性、是否与现有的 `owner_tenant`/`owner_user`
NULL 安全谓词共存），在没有先写一个隔离的实测（建一张临时表 + 数组字段 +
BM25 索引，跑 `EXPLAIN` 确认过滤确实下推、且 `paradedb.score()` 仍可用）之前
无法回答。

方案 A 直接在生产表上改索引，一旦数组下推不成立，后果是**静默的召回塌陷**——
不是报错，是查询仍然返回结果，只是不准确、不完整，且很可能不会被现有测试
覆盖到（现有测试都是单实体场景）。这类退化成本远高于"先维持单值，晚一版
支持多实体公告"。

否决方案 B：多实体公告的场景是真实存在的（方案 §2.4 举的例子成立），
把它路由到 `entity_relation` 或二次识别，等于在没有先验证"数组方案不可行"
之前就放弃了更直接的解法，属于过早收窄选项。

## 后果

1. **多实体公告目前只能挂到 `entity_ref` 解析出的第一个/主实体上**——这是
   已知的、有意接受的召回缺口，不是被忽略的 bug。`entities/resolver.py` 的
   `EntityResolver`（三层降级）目前的返回值就是单个 `entity_id`，这个缺口
   在实体解析层同样存在，不是 `doc_block` 独有的问题。
2. **实测任务**：写一个独立的隔离测试（临时 schema，不动 `core.doc_block`），
   建 `entity_ids text[]` + 对应的 BM25 索引，跑：
   - `paradedb.term_set` 或等价数组谓词的 `EXPLAIN`，确认过滤条件确实下推
     进索引扫描（不是先取全表再在 PG 里过滤数组包含）；
   - 同一张表上 `paradedb.score()` 仍可用（不能重复 `IS NOT DISTINCT FROM`
     那个"与 `paradedb.score()` 同时出现直接被拒绝"的坑——见
     `retrieval/filters.py` 的既有注释）；
   - 时点谓词（`known_at`/`superseded_at`）与数组谓词组合时的性能，
     不能只测数组谓词单独工作。
   结论写成这份 ADR 的更新，或新开一篇 ADR 正式改索引。
3. **数据库变更 stays 冻结**：本次数据底座建设的其余阶段（C–H）不改
   `entity_id` 这一列的类型，避免在没有实测结论前引入一次不可逆的迁移。

## 推翻条件

- 上述隔离实测证明数组字段确实能下推、`paradedb.score()` 不受影响、且
  与现有 NULL 安全谓词兼容 → 批准方案 A，新开一条迁移把 `entity_id` 改成
  `entity_ids text[]`，同时更新 `retrieval/filters.py`、`entities/resolver.py`
  与所有下游消费 `entity_id` 单值的代码；
- 若实测证明数组下推不成立或代价过高（例如需要放弃 `keyword` 分词器、
  或与时点谓词组合时性能显著劣化）→ 转向方案 B，把多实体关联建模为
  `core.entity_relation` 的一种关系类型；
- 若多实体公告在实际语料中的占比长期低于某个可忽略阈值（需要 P1c 检索
  评测集或人工抽检给出真实比例）→ 这个决策可以无限期搁置，不必强行推进。
