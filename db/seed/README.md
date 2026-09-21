# 种子数据

由创始人手工维护（工作流 W9.1，见 `docs/11-sdlc.md` §3）。
**这些 CSV 提交进仓库**——P0 验收（`docs/10-roadmap.md`）要求在干净 clone 上
`make seed` 就能复现表计数，忽略它们等于让验收不可复现。

| 文件 | 目标表 | P0 目标数量 | 当前 |
|---|---|---|---|
| `taxonomy.csv` | 无（导入时校验用） | — | 38 个 L3 环节 |
| `entity.csv` | `core.entity` + `core.entity_node_membership` | 100 | **18** |
| `entity_alias.csv` | `core.entity_alias` | 约 400 | **73** |
| `entity_relation.csv` | `core.entity_relation` | 200 | **30** |
| `node_metric.csv` | `core.node_metric` | 30 | **30 ✅** |
| `propagation_rule.csv` | `core.propagation_rule` | 15 | **15 ✅** |

## 当前这批数据是什么

以 **海光信息（`CN.688041`）** 为核心的一个完整样例切片：海光本身，加上
它在产业链上绕不开的 17 家对手方（整机客户、封测、存储、竞品、代工）。
`entity_relation` 因此能写出真实拓扑，而不是孤立的一行。

`node_metric` 与 `propagation_rule` 是**环节级**的，不随公司数量增减，
已按 P0 验收标准做满 30 条与 15 条。

实体与关系的 100 / 200 待创始人补齐。`tests/test_p0_acceptance.py` 里的
期望值**一个都没有改**，那四条断言带 `seed_full` 标记，补完数据后
`make accept-p0` 会自动转绿。

## 录入约定

- `l3_node` 用 `|` 分隔多个环节；`primary_node` 必须是其中之一。
- **所有环节名必须在 `taxonomy.csv` 里在册。** 环节名是 `entity`、
  `entity_node_membership`、`node_metric`、`propagation_rule`、`opinion`
  五张表之间的事实联结键，而数据库对它一个字都不校验——一个错字不会报错，
  只会让该环节的评分基准悄悄变成空集、让传导规则悄悄匹配不到任何实体。
  导入器会交叉校验并拒绝不在册的环节名。
- `listed_date` 必填——观点评分要用它剔除上市不足 60 个交易日的公司。
- 别名可以一对多（「长城」「中兴」天然歧义），但**不得等于另一家实体的全称**，
  那一定是录入错误，导入器会拒绝。
- 导入时 `known_at` 取 `valid_from` 当日 00:00，不取导入时间。
  这是一个已知的、有意接受的乐观偏差，见 `docs/03-point-in-time.md` §6.3。
  缓解方式是 `source` 统一标 `manual:seed`，评测报告把依赖手工关系的观点单独分组。

## 数据质量与待核对项

只录**公开可查的事实**：公司名称、证券代码、产业链归属、公开披露或行业common
knowledge 层面的供应/客户/竞争关系。不含任何采购自供应商的数据。

两处刻意的保守处理：

1. **`share_estimate` 一律留空。** 没有公开披露的份额就不编一个精确数字。
   `strength` 只用 0.3 / 0.6 / 0.9 三档表达定性强弱，配合
   `evidence_type=expert`、`confidence=low|medium`，让这些关系天然落进
   `docs/08-evaluation.md` §3.1「依赖手工关系」的单独分组。
2. **`listed_date` 中不确定的已在 `notes` 里标注「需与供应商数据核对」。**
   P1 的 Tushare 适配器（W1.3）上线后应跑一次核对：

   ```sql
   -- 以 stock_basic 为准比对种子里的上市日期
   SELECT e.entity_id, e.name_short, e.listed_date, e.notes
     FROM core.entity e
    WHERE e.notes ILIKE '%核对%'
    ORDER BY e.entity_id;
   ```

本批数据不含任何买卖建议、目标价或评级（`CLAUDE.md` §0）。
