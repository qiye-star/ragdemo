# 08 · 评测与观点评分

「改 prompt / 切块 / 模型 → 跑三套评测集 → 看数字 → 再合并」是 `CLAUDE.md` §1.4 的原则。
本文档定义这三套评测集、它们的指标，以及观点评分的完整计算口径。

## 1. 三套评测集

| 评测集 | 表 | 测什么 | P1/P2 规模 |
|---|---|---|---|
| 检索 | `evals.eval_retrieval` | 给定问题与时点，能否召回正确的块 | 100 条 |
| 抽取 | `evals.eval_extraction` | 给定文档与字段，能否抽出正确的值 | 50 条 |
| 判断 | `evals.eval_judgement` | 给定情境，方向判断与推理是否合理 | 30 条 |

前两套是**客观**的（有唯一正确答案），第三套是**半主观**的
（`acceptable_alternatives` 允许多个合理答案）。三套的可信度依次递减，
决策权重也应如此——不要因为判断评测集的分数好看就合并一个检索指标下降的 PR。

### 1.1 检索评测集

详见 `06-retrieval.md` §9。要点重述：至少 30 条的 `as_of` 必须是历史时点，
专门检验时点过滤后召回是否塌陷。

### 1.2 抽取评测集

```
case_id | doc_id | field              | gold_value | unit | gold_block_id
--------|--------|--------------------|-----------|------|---------------
1       | 1042   | revenue_total      | 12340.5   | 百万元 | 88213
2       | 1042   | ai_revenue_pct     | 0.412     | ratio | 88219
3       | 1042   | rd_expense         | 1890.2    | 百万元 | 88240
```

指标：

| 指标 | 定义 | P2 阈值 |
|---|---|---|
| `field_accuracy` | 抽出值与 `gold_value` 在容差内一致的比例 | **> 0.95** |
| `locate_accuracy` | 引用的 `block_id` 与 `gold_block_id` 一致的比例 | > 0.90 |
| `hallucination_rate` | 抽出了文档中不存在的值的比例 | **= 0** |

容差：数值型 ±0.5% 相对误差（与 `07-agents.md` §5.2 的验证器一致）；
比率型 ±0.005 绝对误差。

`locate_accuracy` 与 `field_accuracy` 分开测很重要：两者都高说明系统健康；
`field_accuracy` 高但 `locate_accuracy` 低说明**模型在猜**——它碰巧猜对了数，
但引用的位置是错的，这种"对"不可持续。

`hallucination_rate` 的阈值是 0，不是「尽量低」。抽出文档中不存在的数字
是不可接受的失败，出现即阻断合并。

### 1.3 判断评测集

```python
{
  "scenario": "台积电宣布 2025 年 CoWoS 产能较 2024 年翻倍",
  "as_of": "2024-10-17T20:00:00+08:00",
  "input_ref": {"event_id": 331, "entity_ids": ["TW.2330"]},
  "gold_direction": "bull",
  "gold_reasoning": "CoWoS 是 AI 加速卡封装的主要瓶颈，产能扩张直接利好上游"
                    "封装设备、测试设备与载板环节，A 股对应 ... 时滞约 1-2 个季度",
  "acceptable_alternatives": ["neutral（若论证扩产已被市场充分预期）"]
}
```

指标：

| 指标 | 定义 | P3 阈值 |
|---|---|---|
| `direction_match` | 方向与 `gold_direction` 或 `acceptable_alternatives` 一致 | > 0.75 |
| `mechanism_coverage` | 输出的传导机制覆盖 `gold_reasoning` 关键要点的比例（人工评分 0–1） | > 0.70 |
| `citation_validity` | 引用全部通过验证器检查的比例 | **= 1.0** |

`citation_validity` 必须是 1.0：判断可以有分歧，引用不能有错。

## 2. 观点评分

见 [adr/0007](adr/0007-opinion-scoring-dual-track.md)。两轨**分开存、分开报、不加权合并**。

### 2.1 价格分（`track = 'price'`，全自动）

#### 交易日与区间

```
t0 = 第一个满足 known_at > opinion.as_of 的交易日
     （即观点生成后第一个可获得收盘价的交易日）
t1 = t0 之后的第 N 个交易日
     N = 21（horizon = '1M'）或 63（horizon = '3M'）
```

用 `t0` 而不是 `as_of` 当天，是因为观点往往在收盘后生成，当天的价格已经不可交易。
从下一个交易日的收盘算起是保守且可实现的口径。

交易日历从 `core.price_daily` 的实际数据推导（`03-point-in-time.md` §6.4）。

#### 个股收益

```
复权价 P(t) = close(t) × adj_factor(t)
r_i = P(t1) / P(t0) − 1
```

`price_daily` 的读取走 `asof.price_daily` 视图，`as_of` 取 `scored_at`。
这保证用的是**评分时点已知的复权因子**，而不是未来可能被再次回溯调整的版本。
`opinion_score` 一旦写入不再重算——即使日后复权因子变化，历史分数保持不变。

#### 基准

```
基准 = opinion 目标实体在 opinion.as_of 时点所属 primary_node 的等权组合
```

成分构造（全部在 `as_of` 时点求值，代码实现于 `src/metrics/benchmark.py`）：

```sql
SET LOCAL app.as_of = :opinion_as_of;

SELECT m.entity_id
  FROM asof.entity_node_membership m
  JOIN core.entity e USING (entity_id)
 WHERE m.l3_node = :target_node
   AND e.entity_type = 'listed'
   AND e.listed_date <= :as_of_minus_60_trading_days;
```

再逐一剔除：

1. `t0` 或 `t1` 缺失收盘价的（停牌、退市）；
2. `t0` 当日 `is_suspended = true` 的；
3. 目标实体自身**不剔除**——它是组合成分之一。保留它会让超额收益略微低估，
   这个偏差是保守方向，可接受，且口径简单不易出错。

```
b = mean(r_j)  for j in 成分
α = r_i − b
```

**成分不足的回退**（必须显式定义，否则小环节的评分会静默出错）：

| 成分数 | 处理 |
|---|---|
| ≥ 5 | 正常计算 |
| 3–4 | 正常计算，`benchmark_def.warning = 'thin_node'` |
| < 3 | 退到 `l2_segment` 层等权组合，`benchmark_def.fallback = 'l2_segment'` |
| `l2_segment` 层仍 < 3 | 退到全覆盖池等权，`benchmark_def.fallback = 'universe'` |

`benchmark_def` 是 `opinion_score` 的 jsonb 列，完整记录当次用的成分清单、
`as_of`、回退级别与规则版本号。这样即使日后环节归属调整，历史分数依然可复现
（`02-data-model.md` §6）。

#### 分档

| 观点方向 | 条件 | 分数 |
|---|---|---|
| `bull` | α > +8% | +2 |
| `bull` | +3% < α ≤ +8% | +1 |
| `bull` | −3% ≤ α ≤ +3% | 0 |
| `bull` | −8% ≤ α < −3% | −1 |
| `bull` | α < −8% | −2 |
| `bear` | 同上，符号全部反转 | |
| `neutral` | \|α\| < 3% | +1 |
| `neutral` | 3% ≤ \|α\| < 8% | 0 |
| `neutral` | \|α\| ≥ 8% | −1 |

`neutral` 的分档上限是 +1 而非 +2：判断「没什么变化」的信息量本就低于
判断对了一个方向，分值上限低是合理的。

#### 不评分的情形

以下情形**不写 `opinion_score`**，而不是记 0 分：

- 目标实体在 `[t0, t1]` 区间内停牌超过 1/3 的交易日；
- 目标实体在区间内退市或被合并；
- 观点的目标是 `l3_node` 而非具体实体（环节级观点用该环节等权组合相对
  全覆盖池等权组合计算，规则同上，基准替换为 universe）；
- `confirmed_by IS NULL`（未经人工确认的观点不进入统计）。

记 0 分会污染平均分——「没法评」和「评了是中性」是两回事。

### 2.2 论据分（`track = 'evidence'`，人工为主）

价格分回答「市场是否认同」，论据分回答「事实是否印证」。二者经常不一致，
这正是分开存的价值。

| 分数 | 含义 |
|---|---|
| +2 | 后续披露的事实**明确印证**核心论据（如预判的订单/产能/收入确实出现） |
| +1 | 部分印证，或方向对但幅度显著小于预期 |
| 0 | 区间内无相关新信息，无法判断 |
| −1 | 部分证伪 |
| −2 | 明确证伪 |

流程：

1. 到期（1M / 3M）时，复盘评估 Agent 检索 `[opinion.as_of, scored_at]` 区间内
   与该实体/环节相关的新文档，产出**预标注**（含候选 `evidence_blocks`）；
2. 创始人在 CLI / Web 中复核，确认或修改分数；
3. 写入 `opinion_score`，`scorer = 'human'`，`scorer_name` 记录复核人。

**Agent 的预标注不能直接入库。** 让模型给自己的观点打分是循环论证。
Agent 在这里的作用是「找出相关的新证据」，判断由人做。

预标注与人工最终分的一致率是一个值得跟踪的元指标：一致率高到一定程度后，
可以考虑把明显情形（如 `0` 分，即区间内确实无新信息）自动化。

## 3. 报告口径

### 3.1 必须分组

观点准确率**不能只报一个总数**。每次报告必须给出以下分组：

| 分组维度 | 为什么必须分 |
|---|---|
| `track`（价格 / 论据） | 两个完全不同的问题，合并没有意义 |
| `horizon`（1M / 3M） | 短期噪声大，长期才有信号 |
| `direction`（bull / bear / neutral） | 牛市中 bull 观点天然得分高 |
| `confidence`（high / medium / low） | 高置信观点如果不比低置信准，置信度就是假的 |
| **依赖手工关系 vs 仅依赖自动抽取** | `03-point-in-time.md` §6.3 的乐观偏差检验 |
| `agent_name` + `prompt_version` | 定位是哪个改动带来的变化 |

倒数第二项最关键：如果「依赖手工关系」的观点显著优于「仅自动抽取」的观点，
说明结论主要来自创始人的事后知识而非引擎能力，这个数字不能对外宣称。

### 3.2 样本量下限

**单个分组样本数 < 30 时不报比例，只报原始计数。** 10 个观点中 7 个得正分
不是 70% 准确率，是噪声。P5 的「观点准确率公开页」在任一分组样本不足时
必须显示样本数而非百分比。

### 3.3 指标定义

```
命中率     = count(score > 0) / count(*)
平均分     = mean(score)
反向率     = count(score < 0) / count(*)      -- 比命中率更值得盯
```

反向率单独看，因为「判断反了」的成本远高于「判断中性」。

## 4. CI 门禁

### 4.1 PR 门禁（每次 PR 必跑）

| 检查 | 阈值 | 失败动作 |
|---|---|---|
| `eval_retrieval` `recall@10` | 不低于主干基线 −0.02 | 阻断 |
| `eval_extraction` `field_accuracy` | 不低于主干基线 −0.02 | 阻断 |
| `eval_extraction` `hallucination_rate` | `= 0` | 阻断 |
| `eval_judgement` `citation_validity` | `= 1.0` | 阻断 |
| Schema 不变量测试（`02-data-model.md` §9） | 全通过 | 阻断 |
| 时点泄漏自检（`03-point-in-time.md` §5） | 全部返回空 | 阻断 |

**结果数字必须贴进 PR 描述**（`CLAUDE.md` §1.4）。CI 自动生成一段 Markdown
评论，含本次与基线的对比表。

### 4.2 Nightly

- `06-retrieval.md` §3.6 的索引召回率验证（`≥ 0.95`）；
- p95 延迟（`< 3s`）；
- 全量时点泄漏自检；
- 成本统计与预算告警。

### 4.3 每次运行都写 `eval_run`

`evals.eval_run` 记录 `git_sha` 与完整 `config`（检索参数、模型、prompt 版本）。
没有这张表，三个月后看到指标下降无法定位是哪个参数改的。

## 5. 修正记录的用法

`evals.correction_log` 不只是日志。`reason_category` 的分布是**发现系统性缺陷的
主要手段**：

- `wrong_number` 集中出现 → 检查数值归一化与表格解析；
- `missing_source` 集中出现 → prompt 的溯源约束不够强；
- `wrong_entity` 集中出现 → 别名库覆盖不足，看 `entity_resolution_queue`；
- `stale_data` 集中出现 → 某个数据源的管线卡了。

每月复核一次分类分布，进研发待办。这是评测集之外的第二条反馈回路，
覆盖评测集没想到的失败模式。
