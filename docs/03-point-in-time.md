# 03 · 时点一致性规范

**这是全系统最重要的一篇文档。** 引擎的全部差异化价值——可复现的回测、可信的观点评分、
经得起追问的研究结论——都建立在「任何时刻都能准确还原当时可知的信息」之上。
本文档的任何一条被违反，回测结果就是自欺欺人。

## 1. 时间戳语义

### 1.1 五个时间戳

| 字段 | 含义 | 举例（某公司 2024 年三季报） |
|---|---|---|
| `valid_from` | **数据描述的期间起点**。回答「这条数据说的是什么时候的事」 | `2024-07-01` |
| `period_end` | 期间结束日（仅 `fin_fact`） | `2024-09-30` |
| `publish_at` | **该信息在现实中被公开发布的时刻**（仅 `document` / `event`） | `2024-10-28 18:32 CST` |
| `known_at` | **该数据在现实中最早可被获知的时刻** | `2024-10-28 18:32 CST` |
| `superseded_at` | 被更正版本取代的时刻；`NULL` 表示当前有效 | `NULL`，若三季报后来被更正则填更正公告的 `known_at` |
| `ingested_at` | 本系统把它写进数据库的时刻 | `2025-03-14 02:11 CST`（假设三月才补的历史数据） |

### 1.2 `known_at` 不是入库时间（对简报的修正）

简报 §13 把 `known_at` 描述为「系统首次获知该数据的时间」。**照字面实现会毁掉回测。**

反例：2025 年 3 月接入 Tushare 时一次性回填了 2020–2024 年全部财务数据。若
`known_at = ingested_at = 2025-03-14`，那么以 `as_of = 2024-08-01` 跑回测时，
`known_at <= as_of` 全部不成立——**2024 年 8 月变成了没有任何财务数据的世界**。
回测不会报错，只会安静地得出一堆基于空数据的结论。

正确定义：`known_at` = **该数据在现实世界中最早可被一个理性研究者获知的时刻**。
它是数据本身的属性，与本系统何时接入无关。回填十年历史数据时，每一行的 `known_at`
都应该是它当年真实的披露时刻。

`ingested_at` 保留下来只用于运维排查（「这批数据是哪次 run 写的」）。
**`ingested_at` 出现在任何 `as_of` 过滤表达式中都是缺陷**，CI 中有静态检查
（`grep` 规则 + `tests/test_no_ingested_at_in_filters.py`）。

### 1.3 各数据源的 `known_at` 计算规则

| 数据 | `known_at` 取值 | 保守性说明 |
|---|---|---|
| A 股 / 港股公告 | 供应商返回的公告发布时间戳 | 若供应商只给日期不给时间，取当日 `23:59:59` 本地时区——宁可晚知道，不可早知道 |
| A 股财务数据（Tushare） | 对应定期报告的公告日 `ann_date` 当日 `23:59:59` | **不取 `end_date`**。用 `end_date` 会让 9 月 30 日就「知道」三季报，典型前视偏差 |
| 财务数据更正 | 更正公告的公告日 | 同时给被更正的旧行打 `superseded_at` |
| 日行情 | `trade_date` 当地收盘后固定时刻（A 股 15:30 CST，美股 16:30 ET） | 不取拉取时间。盘中不得使用当日行情 |
| 复权因子回溯调整 | 除权除息公告日 | 见 `02-data-model.md` §4.2 |
| SEC EDGAR | filing 的 `acceptanceDateTime` | EDGAR 提供精确到秒的受理时间，直接用 |
| 公司 IR 页（台积电月营收等） | 页面标注的发布时间；无标注则取首次抓取日 `23:59:59` | 首次抓取日只在无法确定真实发布时间时使用，必须在 `source_ref` 中标注 `known_at_estimated=true` |
| 新闻 RSS | 条目的 `pubDate` | 若缺失取抓取时刻 |
| 政策文件 | 官网发文日期当日 `23:59:59` | |
| 用户上传材料 | 上传时刻 | 这是唯一 `known_at ≈ ingested_at` 合理的场景 |
| 手工维护数据（实体、关系、规则） | `valid_from` 当日 `00:00` | 见 §6.3 的限制 |

**通则：不确定时取更晚的时刻。** 晚知道会让回测略微保守，早知道会让回测系统性高估——
前者是可接受的误差，后者是欺骗自己。

### 1.4 `publish_at` 与 `known_at` 的关系

绝大多数情况下 `document.publish_at == document.known_at`。它们分开存是因为存在
**获知延迟**：

- 某些供应商对非订阅客户有 T+1 延迟 → `known_at = publish_at + 延迟`；
- 部分海外数据源在国内可获取的时间晚于原始发布时间。

延迟值配置在适配器中（`04-ingestion.md` §2.3），由适配器负责计算 `known_at`，
业务代码不碰。

---

## 2. 标准 `as_of` 谓词

所有对时点表的读取都必须包含：

```sql
WHERE known_at <= :as_of
  AND (superseded_at IS NULL OR superseded_at > :as_of)
```

两个条件缺一不可：

- 只有第一条 → 会看到已被更正的旧值和更正后的新值**两行**，唯一性约束在历史时点上失效；
- 只有第二条 → 会看到未来的数据。

`as_of` 类型必须是 `timestamptz`，**不允许传 `date`**。`date` 会被隐式转换为当地 00:00，
导致「当天已披露的公告」被判为未知，且行为随服务器时区变化。接口层强制类型，
传 `date` 直接报错而不是自动转换。

### 2.1 `as_of` 没有默认值

工具层的每个查询函数都把 `as_of` 作为必填参数（`07-agents.md` §3）。
**不提供 `as_of = now()` 的默认值**——默认值会让「忘记传时点」这个缺陷静默通过，
在开发期表现正常，在回测时给出错误结果。

---

## 3. 更正处理

更正是**追加**，不是更新。流程固定为一个事务：

```sql
BEGIN;

-- 1) 给被更正的旧行打上失效时刻
UPDATE core.fin_fact
   SET superseded_at = :correction_known_at
 WHERE entity_id = :entity_id
   AND metric_id  = :metric_id
   AND period     = :period
   AND superseded_at IS NULL;

-- 2) 插入更正后的新行
INSERT INTO core.fin_fact
  (entity_id, metric_id, period, period_end, value, unit, currency,
   valid_from, known_at, source, source_ref, source_block, ingest_run_id)
VALUES
  (:entity_id, :metric_id, :period, :period_end, :new_value, :unit, :currency,
   :valid_from, :correction_known_at, :source, :source_ref, :source_block, :run_id);

COMMIT;
```

`superseded_at` 必须等于新行的 `known_at`，不是 `now()`。这样任意 `as_of` 都恰好命中
一行，不留空隙也不重叠。`fin_fact_live_uk` 部分唯一索引（`02-data-model.md` §4.1）
在数据库层面保证这一点：忘记第 1 步，第 2 步会直接违反唯一约束而失败。

**这个流程只能由 `src/ingest/` 的时点写入中间件执行**——应用角色对 `core` 没有
`UPDATE` 权限，中间件用独立的写入角色连接。

### 3.1 文档更正

文档的更正通过版本组表达，而不是改写块：

```
document(doc_id=100, version_group_id=100, is_correction=false, superseded_at=<更正公告 known_at>)
document(doc_id=207, version_group_id=100, is_correction=true, supersedes_doc_id=100)
```

旧版本的 `doc_block` 同样打 `superseded_at`，但**行本身永远保留**，
因为历史观点的 `evidence_blocks` 引用着它们。以 `as_of` 在更正前的时点检索，
命中的仍然是旧块——这正是我们要的：还原当时的认知。

### 3.2 删除

**没有物理删除。** 数据错误（比如实体识别错误导致挂到了错公司）的处理方式是
`superseded_at = now()` 并在 `source_ref` 记录原因。物理删除会让历史引用悬空，
也让审计失效。

唯一例外是合规要求的用户数据删除（`09-compliance-security.md` §3.4），
走单独的、有审批记录的流程。

---

## 4. 物理防泄漏机制

「所有查询都带 `as_of`」如果只靠代码审查保证，迟早会漏。本节的机制让它**在数据库层面
无法违反**。

### 4.1 会话变量 + 安全视图

```sql
-- 取当前会话的 as_of；未设置则报错而不是回退到 now()
CREATE OR REPLACE FUNCTION asof.current_as_of() RETURNS timestamptz
LANGUAGE plpgsql STABLE AS $$
DECLARE v text;
BEGIN
  v := current_setting('app.as_of', true);
  IF v IS NULL OR v = '' THEN
    RAISE EXCEPTION 'app.as_of is not set; every read must declare a point in time'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  RETURN v::timestamptz;
END $$;
```

每张时点表对应一个视图：

```sql
CREATE VIEW asof.fin_fact AS
SELECT * FROM core.fin_fact
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.doc_block AS
SELECT * FROM core.doc_block
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.price_daily AS
SELECT * FROM core.price_daily
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.document AS
SELECT * FROM core.document
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.entity_relation AS
SELECT * FROM core.entity_relation
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.entity_node_membership AS
SELECT * FROM core.entity_node_membership
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.event AS
SELECT * FROM core.event
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());
```

七张时点表对应七个视图，与 `core.bitemporal_registry` 的登记一一对应，
由 Schema 不变量测试强制（`02-data-model.md` §9 第 3 条）。

### 4.2 权限

```sql
-- app_owner 是第三个角色，NOLOGIN，只用来持有 core / asof / evals / audit 的对象。
-- 它必须是**非超级用户**：超级用户无条件绕过 RLS，属主是超级用户时
-- 09-compliance-security.md §3.2 的行级隔离对经由 asof.* 视图的读取完全不生效。
CREATE ROLE app_owner NOLOGIN;
CREATE ROLE app_read;
CREATE ROLE app_write;

-- 应用角色看不见时点基表
REVOKE ALL ON ALL TABLES IN SCHEMA core FROM app_read;
GRANT USAGE ON SCHEMA core, asof TO app_read;
GRANT SELECT ON ALL TABLES IN SCHEMA asof TO app_read;

-- §4.4 的三张非时点表 + 三张纯配置表，直接授予 SELECT
GRANT SELECT ON core.entity, core.entity_alias, core.node_metric,
                core.metric_source_map, core.ai_revenue_rule, core.propagation_rule
             TO app_read;

-- 写入中间件用独立角色，且只能 INSERT + 受控 UPDATE。
-- USAGE 不能漏：没有它，下面的 GRANT INSERT 形同虚设。
GRANT USAGE ON SCHEMA core TO app_write;
GRANT INSERT ON ALL TABLES IN SCHEMA core TO app_write;
GRANT UPDATE (superseded_at) ON core.fin_fact, core.price_daily,
                                core.document, core.doc_block,
                                core.entity_relation, core.entity_node_membership,
                                core.event TO app_write;

-- 审计层只能追加
GRANT USAGE ON SCHEMA audit TO app_read, app_write;
GRANT INSERT, SELECT ON audit.tool_call_log TO app_write;
GRANT SELECT ON audit.tool_call_log TO app_read;
REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA audit
  FROM app_read, app_write, PUBLIC;
```

创建角色要写成幂等的（`DO $$ ... IF NOT EXISTS ... $$`）：角色是集群级对象，
迁移完全可能在一个已经有这些角色的集群上执行。完整实现见
`db/migrations/006_asof_views_and_roles.sql`。

`GRANT UPDATE (superseded_at)` 是列级权限：写入中间件能打失效标记，
但**改不了任何一个值字段**。这从根本上杜绝了「就地修数据」。

### 4.3 应用侧的用法

```python
@contextmanager
def as_of_session(conn, as_of: datetime) -> Iterator[Connection]:
    """开启一个锁定时点的事务。退出后设置自动失效。"""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    with conn.transaction():
        conn.execute("SET LOCAL app.as_of = %s", (as_of.isoformat(),))
        yield conn
```

用 `SET LOCAL` 而不是 `SET`：时点绑定在事务上，事务结束自动清除，
不会因为连接池复用把上一个请求的 `as_of` 泄漏给下一个请求。这是连接池场景下
最容易出的一类 bug，必须有针对性的单测。

### 4.4 边界：`entity` 与 `node_metric`

`core.entity`、`core.node_metric`、`core.propagation_rule` 不是时点表，
对 `app_read` 直接授予 `SELECT`（具体语句在 §4.2）。这是刻意的取舍：
它们是描述性/配置性数据，每条都做双时间轴会让种子数据维护成本翻倍。

> 这条授权与 `02-data-model.md` §9 不变量 5 原先的措辞「应用角色对 `core` schema
> **没有** `SELECT` 权限」直接冲突。按 `10-roadmap.md` P0 验收的字面要求
> （以 `app_read` 查 `core.fin_fact` 被拒）化解：不变量 5 已收窄为
> 「对 `core.bitemporal_registry` 登记的**七张时点表**没有 `SELECT`」。

代价是：**回溯修改这些表会影响历史结果**。缓解措施有三条：

1. 会直接进入数值计算的部分已经拆出去了——`ai_revenue_pct` 进 `fin_fact`，
   环节归属进 `entity_node_membership`（`02-data-model.md` §2.1、§2.4）；
2. 这三张表的变更走迁移脚本，进 git 历史，可追溯；
3. `opinion_score.benchmark_def` 把评分当次用到的成分股清单完整快照下来，
   即使日后配置变化，历史分数仍可复现。

---

## 5. 回测泄漏自检

以下查询应在 CI 中定期运行，任何一条返回非空行即为泄漏。

```sql
-- 1) known_at 早于数据所描述的期间结束日 —— 不可能提前知道
SELECT fact_id, entity_id, metric_id, period, period_end, known_at
  FROM core.fin_fact
 WHERE known_at::date < period_end;

-- 2) known_at 等于 ingested_at 且 ingested_at 远晚于 valid_from
--    —— 典型的「回填时误用入库时间」
SELECT fact_id, entity_id, metric_id, valid_from, known_at, ingested_at
  FROM core.fin_fact
 WHERE known_at = ingested_at
   AND ingested_at::date - valid_from > 400;

-- 3) 失效时刻早于生效时刻
SELECT 'fin_fact' AS t, fact_id::text AS id FROM core.fin_fact
 WHERE superseded_at IS NOT NULL AND superseded_at <= known_at
UNION ALL
SELECT 'doc_block', block_id::text FROM core.doc_block
 WHERE superseded_at IS NOT NULL AND superseded_at <= known_at;

-- 4) 同一 (实体, 指标, 期间) 在某个历史时点上有多行有效
--    （live 唯一索引只覆盖 superseded_at IS NULL，历史时点需单独查）
SELECT entity_id, metric_id, period, count(*)
  FROM core.fin_fact
 WHERE known_at <= :probe_as_of
   AND (superseded_at IS NULL OR superseded_at > :probe_as_of)
 GROUP BY 1,2,3 HAVING count(*) > 1;

-- 5) 观点引用了它生成时还不存在的块
SELECT o.opinion_id, b.block_id, o.as_of, b.known_at
  FROM core.opinion o
  JOIN core.doc_block b ON b.block_id = ANY (o.evidence_blocks)
 WHERE b.known_at > o.as_of;
```

第 5 条是最关键的一条：它直接检测「观点用了未来数据」。**这条查询返回非空行时，
相关观点的全部评分作废。**

### 5.1 双跑一致性测试

除静态检查外，P1 验收需要一个端到端测试：

1. 取一个历史时点 `T`（例如 90 天前）；
2. 以 `as_of = T` 跑完整检索 + 基本面 Agent，记录证据集与产出数值；
3. 一周后再跑一次同样的 `as_of = T`；
4. 断言两次的证据 `block_id` 集合与全部数值**完全一致**。

这期间数据库新增了一周的数据。如果结果变了，说明有地方漏了时点过滤。
这个测试比任何静态检查都有效，它是 P1 的验收项。

---

## 6. 边界情况

### 6.1 财报的多次披露

业绩预告 → 业绩快报 → 正式财报，同一期间的同一指标会有三个来源、三个精度。
它们是**三条独立的 `fin_fact` 行，不是互相更正**——`metric_id` 不同
（`revenue_forecast` / `revenue_flash` / `revenue_total`）。只有「正式财报被更正公告修改」
才走 §3 的更正流程。

### 6.2 时区

全库统一 `timestamptz`，数据库时区设为 `UTC`，应用层按市场时区解释。
A 股用 `Asia/Shanghai`，美股 `America/New_York`，台股 `Asia/Taipei`。
`known_at` 的计算必须在市场当地时区完成后再转 UTC 存储，否则跨时区的日界会错一天。

### 6.3 手工数据的 `known_at`

创始人手工维护的实体关系、传导规则，`known_at` 取 `valid_from` 当日 00:00。
这意味着「我们今天写下的一条 2023 年就存在的供应关系，在回测 2023 年时是可见的」。

**这是一个已知的、有意接受的乐观偏差**：手工知识库本身就是事后整理的，
要求它具备严格时点性不现实。缓解方式：

- 在 `entity_relation.source` 中标记 `manual:<author>`，评测报告里把
  依赖手工关系的观点单独分组统计；
- 观点评分报告必须同时给出「全部观点」和「仅依赖自动抽取关系的观点」两组数字。
  两组数字差距过大说明结论主要来自事后知识，不可信。

### 6.4 `as_of` 落在非交易日

行情相关计算（观点评分）中 `as_of` 若落在非交易日，取**之前**最近的交易日，不取之后。
交易日历从 `price_daily` 的实际数据推导（某市场有行情的日期即交易日），
不引入额外的日历数据源。
