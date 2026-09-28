# 种子数据

**这些 CSV 提交进仓库**——P0 验收（`docs/10-roadmap.md`）要求在干净 clone 上
`make seed` 就能复现表计数，忽略它们等于让验收不可复现。

## 从哪来：`docs/csv` → `db/seed`

2026-09-28 起，实体、别名、关系、指标、传导规则由创始人维护的底稿 **`docs/csv/`**
生成，不再手改 `db/seed/*.csv`：

```bash
ragdemo db seed-listing-dates   # 联网：从供应商取上市日期 → sources/listing_dates.csv
ragdemo db seed-build           # 离线、确定性：docs/csv + sources/ → db/seed/*.csv
ragdemo db seed                 # 同步进库（可重复执行）
```

`docs/csv` 改了只需重跑后两步。`sources/` 里是把人读底稿落成入库格式所需的整理结果：

| 文件 | 内容 |
|---|---|
| `entity_curation.csv` | 每家公司一行：实体 ID、类型、市场、代码、L1/L2/L3 环节、币种、财年截止月 |
| `relation_names.csv` | 关系表里的别名（ASML → 阿斯麦、阿里云 → 阿里巴巴）与泛称（"北美云厂"） |
| `metric_curation.csv` | 指标字典每条的 metric_id、环节、频率、来源类型、单位、方向 |
| `rule_curation.csv` | 传导规则每条的触发事件代号，以及"L3网络(光模块/交换机)"展开成的环节 |
| `kept_relations.csv` | 原种子里 docs/csv 没有表达的关系（含不在名单里的 4 家的关系），原样保留 |
| `listing_dates.csv` | 上市日期，每行带来源（tushare / wind）与取数日期 |
| `docs_csv_version.csv` | 底稿编制日期（新关系的 valid_from） |

`taxonomy.csv`（环节表）仍然手工维护；新环节先加在这里。

| 文件 | 目标表 | P0 下限 | 当前 |
|---|---|---|---|
| `entity.csv` | `core.entity` + `core.entity_node_membership` | 100 | 118 |
| `entity_alias.csv` | `core.entity_alias` | — | 528 |
| `entity_relation.csv` | `core.entity_relation`（现行） | 150 | 193 |
| `node_metric.csv` | `core.node_metric_version`（现行） | 30 | 43 |
| `propagation_rule.csv` | `core.propagation_rule`（启用） | 15 | 44 |

实体 118 = docs/csv 名单 100 + 关系表补录的名单外公司 14 + 原种子保留 4（龙芯中科、
长川科技、澜起科技、沪电股份，已有 Wind 财务数据）。指标 43 = 13 个财务报表科目
（Wind 财务数据挂在上面）+ 指标字典 30 个。

## 构建时写死的规则

- **名字解析是严格的。** 关系表里的名字必须是名单简称、`relation_names.csv` 登记的别名或
  泛称；都不是就构建失败，不静默丢关系。
- **泛称不是实体。** "工业富联 供货 北美云厂"不建关系，写进工业富联的 `notes`，信息不丢。
- **关系不回溯。** 底稿里的关系没有日期；新关系 `valid_from` = 底稿编制日期
  （2026-09-28），`known_at` = 当日 00:00。把"行业共识"记成多年前就已知是前视。
  与原种子同一 (from, to, 类型) 的关系沿用原行。
- **上市日期取供应商数据**（A 股 Tushare `stock_basic`，限频时退到 Wind；境外 Wind），
  两家对 59 只 A 股一致。原种子手填的 7 个上市日期有误（浪潮信息差两年），已按供应商更正。
- 底稿与供应商数据冲突时按供应商：长鑫存储已于 2026-07-27 上市（688825.SH）、MiniMax
  （0100.HK）与智谱（2513.HK）已于 2026 年 1 月上市，底稿仍写非上市 / 未收录。
- 传导规则的时滞取下限（"1-2个季度" → 90 天）；可观测验证点与失效条件并进 `mechanism`；
  `author` = `docs/csv#<序号>` 指回底稿。底稿第 2、11 条正文含裸逗号导致错列，按规则还原。

## 同步语义（`ragdemo db seed` 可重复执行）

库里已经有真实数据（财务、行情），不能清库重来：

- `core.entity` 是当前画像表，直接更新；CSV 里没有、库里有的实体不动。
- 环节归属、关系是时点表：去掉的行记 `superseded_at` = 导入时刻，不删行；
  **已有实体新增的环节从导入时刻起才算已知**，新实体的归属仍取 `valid_from` 当日 00:00
  （上市公司 = 上市日，非上市 = 底稿编制日）——这是一个已知的、有意接受的乐观偏差，
  见 `docs/03-point-in-time.md` §6.3。
- 指标：新指标版本 1 取固定基线时刻；口径变了发新版本（known_at = 导入时刻）；
  去掉的指标现行版本失效（还有现行供应商映射的拒绝停用）。
- 传导规则：内容相同的不重复插入，去掉的置 `enabled = false`。
- 别名只增不删。

## 数据质量与待核对项

只录**公开可查的事实**。`share_estimate`、`strength` 在底稿没有给出时一律留空，不编数字；
`ai_revenue_pct` 在底稿里全是"待核实"，留空，核实来源建议写在 `notes`。
`notes` 里带"需核对"的条目（财年截止月、法定全称等）是整理时的不确定项。

本批数据不含任何买卖建议、目标价或评级（`CLAUDE.md` §0）。
