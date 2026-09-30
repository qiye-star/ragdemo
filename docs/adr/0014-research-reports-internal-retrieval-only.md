# ADR-0014 · 券商研报入库仅用于检索与内部研究，原文不展示

- 状态：已接受
- 日期：2026-09-30
- 决策者：数据底座负责人
- 影响：修订 ADR-0008 后果 1 之外的一条隐含约定"研报一律不入库"（`local_manifest.load_documents`
  原来对 `kind=research` 的过滤"没有开关"）；不改变任何已锁定的技术决策。

## 背景

海光信息的原始材料里有 91 份券商研报（东方财富公开列表）。两条硬约束：

- **CLAUDE.md §0**：禁止输出买卖建议、目标价、评级、仓位建议。研报正文就是评级 + 目标价 +
  盈利预测。
- **来源使用限制**（`docs/rawdata/688041-hygon/README.md`）：著作权归各发布券商，仅供内部
  研究参考，不得对外分发或商用。

负责人最初提出"研报全部公开可见"，与上面两条冲突；经对齐选定"入库但不展示原文"。

## 决定

1. 研报可以入库，用于**检索与内部研究**；来源登记为 `eastmoney-research`，
   `can_show_raw = false`、`can_vectorize = true`、`time_precision = 'day'`（迁移 027）。
2. `can_show_raw = false` 由现有权限链执行：`core.document` / `core.doc_block` 继承该值，
   DataApi 与诊断界面对这类来源只返回元数据与溯源，`content` / `preview` 为 `None`。
3. 研报的观点一律是**第三方观点**：引用时必须标注机构与日期，不得写成本系统的结论；
   评级 / 目标价 / 盈利预测不进入任何面向用户的输出（违禁词拦截仍然生效）。
4. 入库必须显式、且与公告分开：`ragdemo docs ingest --kind research`（来源固定
   `eastmoney-research`，传其他 `--source` 直接报错）；默认 `kind=announcement` 不加载研报。
   `doc_type` 记为 `research_report`。
5. 解析走 TextIn 按页计费，受既有页数预算与月度上限约束。

## 推翻条件

要对外展示研报原文，必须同时满足：(a) 取得券商或东方财富的书面转载授权；
(b) 入库前剔除评级、目标价、盈利预测段落；(c) 新增 ADR 推翻本决定。

## 后果

- 91 份研报尚未入库：TextIn 余额不足（负责人此前决定暂停解析），充值后执行
  `ragdemo docs ingest --kind research --manifest <manifest> --entity-ref 688041.SH --yes`。
- 评测集中出现研报块时，检索评测的 gold 与展示权限互不影响（检索可命中，展示被截断）。
