# db/seed/ — 种子数据 CSV（提交进仓库，保证干净 clone 上 `make seed` 可复现）

## 这个目录里有什么

| 文件 | 内容 |
|---|---|
| `entity.csv` `entity_alias.csv` `entity_relation.csv` `node_metric.csv` `propagation_rule.csv` `metric_source_map.csv` | 入库格式的种子表，**由 `seed-build` 生成** |
| `taxonomy.csv` | 环节表，**手工维护**（新环节先加这里） |
| `sources/` | 把创始人底稿 `docs/csv/` 落成入库格式所需的整理结果：`entity_curation` `relation_names` `metric_curation` `rule_curation` `kept_relations` `listing_dates` `docs_csv_version`（逐个说明见 `README.md`） |
| `README.md` | 来源与流水线说明 |

## 怎么用

```bash
set -a; . ./.env; set +a
uv run ragdemo db seed-listing-dates   # 联网取上市日期 → sources/listing_dates.csv（调供应商，经出网代理）
uv run ragdemo db seed-build           # 离线、确定性：docs/csv + sources/ → db/seed/*.csv
uv run ragdemo db seed                 # 同步进库，可重复执行
```

`docs/csv` 变了只需重跑后两步。

## 硬规则

- 这些 CSV **要提交**（根 `.gitignore` 对 `data/` 忽略，但 `db/seed/` 不忽略）；不要把它们加进忽略。
- 实体/别名/关系/指标/规则不要手改 `db/seed/*.csv`，改 `docs/csv/` 或 `sources/` 后重新 `seed-build`；只有 `taxonomy.csv` 手改。
- `seed-build` 必须确定性：同输入同输出，diff 才有意义。
- 隔离演示数据（`seed-isolation-demo`）不是这里的 CSV，只在回环地址库生效，用 `clear-isolation-demo` 清除。

## 常见坑

- 完整种子目标（实体 100、关系 200 等）曾未录满，导致 `seed_full` 标记的两条测试失败；以 `docs/12-dev-flow.md` 与实跑为准。
- `listing_dates.csv` 每行带来源与取数日期，别丢字段。
