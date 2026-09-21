# 09 · 合规、权限与审计

合规不是产品的一个特性，是它能否存在的前提（`00-overview.md` §4）。
本文档定义四道防线：输出过滤、溯源强制、权限隔离、执行审计。

## 1. 输出合规过滤器

所有对外输出（简报、事件解读、问答回复、导出文件）在离开系统前必须通过过滤器。
过滤器在渲染层之后、传输层之前，**没有绕过路径**。

### 1.1 违禁内容

| 类别 | 示例 | 处理 |
|---|---|---|
| 交易动作 | 买入、卖出、增持、减持、加仓、减仓、建仓、清仓、抄底、止损 | 拦截 |
| 评级 | 推荐、强烈推荐、优于大市、跑赢行业、首次覆盖给予…评级 | 拦截 |
| 目标价 | 目标价、合理估值区间、上看…元、看到…元 | 拦截 |
| 仓位 | 建议配置、仓位、重仓、标配、超配、低配 | 拦截 |
| 收益承诺 | 预计涨幅、稳赚、必涨、翻倍 | 拦截 |

词表存 `config/compliance/forbidden_terms.yaml`，按类别组织，支持正则。
**词表的变更需要两人审核**（CODEOWNERS 规则），因为放宽一个词就是放宽一条合规边界。

### 1.2 允许与禁止的表述对照

过滤器要拦的是「建议」，不是「分析」。边界如下：

| 禁止 | 允许 |
|---|---|
| 「建议买入」 | 「该环节景气度指标连续两季改善」 |
| 「目标价 85 元」 | 「当前 PE 处于近三年 32% 分位」 |
| 「建议超配算力环节」 | 「算力环节 2024Q3 收入同比中位数 +41%，高于其他环节」 |
| 「这是个好机会」 | 「该事件对 X 环节的历史传导时滞约 1–2 个季度」 |

这个对照表要放进每个生成 Agent 的 prompt——**在生成端约束比在过滤端拦截更有效**，
过滤器是最后一道网，不是第一道。

### 1.3 无来源数值拦截

```python
def check_unsourced_numbers(output: RenderedOutput) -> list[Violation]:
    """输出中的每个数值必须能在 citations 覆盖的原文中找到，或被标记为推断。"""
```

算法：

1. 用正则提取输出中的全部数值（含百分比、金额、倍数、带单位的数）；
2. 排除白名单：年份、期间标识（`2024Q3`）、页码、`block_id`、条目序号；
3. 对每个剩余数值，按 `07-agents.md` §5.2 的归一化规则，在该 Claim 的
   `citations` 指向的原文中查找；
4. 找不到且 `is_inferred = False` → **违规**。

违规处理**分级**：

| 场景 | 动作 |
|---|---|
| 自动推送的简报 | 拦截整篇，不发送，告警 + 写 `correction_log` |
| 交互式问答 | 把该数值替换为「推断」标注后放行，并在回复末尾说明 |
| 导出文件 | 拦截，提示用户哪些数值无法溯源 |

自动推送场景最严格，因为没有人在环；交互场景用户能看到标注并自行判断。

### 1.4 免责声明

每个输出模板强制包含（`07-agents.md` §6）：

> 本文由 AI 研究引擎自动生成，内容为研究参考，不构成投资建议。
> 所有数据均标注来源与时点，请以原始披露文件为准。

模板渲染时若缺少该段落，渲染器直接报错——不是「建议加上」，是渲染失败。

## 2. 溯源强制

三层强制，任一层都能独立兜底：

| 层 | 机制 | 位置 |
|---|---|---|
| 数据库 | `opinion_has_evidence` CHECK 约束 | `02-data-model.md` §6 |
| 生成 | `Claim.citations` 必填的结构化输出契约 | `07-agents.md` §2 |
| 输出 | 验证器 6 项检查 | `07-agents.md` §5 |

其中**检查项 3（引用的 `known_at <= opinion.as_of`）是合规与时点的交叉点**：
引用了观点生成时尚不存在的文档，既是时点泄漏也是溯源造假。
该项失败必须告警而不只是拦截。

## 3. 权限隔离

### 3.1 三种可见性

| 可见性 | `owner_tenant` | `owner_user` | 谁能看 |
|---|---|---|---|
| 公共 | NULL | NULL | 所有登录用户 |
| 租户私有（B 端） | `<tenant_id>` | NULL | 该租户全体成员 |
| 用户私有（C 端上传） | `<tenant_id>` 或 NULL | `<user_id>` | 仅该用户 |

### 3.2 行级安全

隔离**不靠应用层拼 WHERE 条件**，靠 PostgreSQL RLS：

```sql
-- FORCE 不能少。asof.* 是普通视图，按**视图属主**的身份与 RLS 上下文执行；
-- 只 ENABLE 的话属主自己不受策略约束，经视图读取时隔离等于没开。
ALTER TABLE core.document  ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.document  FORCE  ROW LEVEL SECURITY;
ALTER TABLE core.doc_block ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.doc_block FORCE  ROW LEVEL SECURITY;

-- 策略给 PUBLIC 而不是 app_read：读取经由 asof.* 视图发生时，
-- 求值身份是视图属主而不是 app_read，只授给 app_read 的策略根本不会被执行。
CREATE POLICY doc_visibility ON core.document FOR SELECT TO PUBLIC
USING (
      (owner_tenant IS NULL AND owner_user IS NULL)                  -- 公共
   OR (owner_tenant = current_setting('app.tenant', true)
       AND owner_user IS NULL)                                       -- 租户私有
   OR (owner_user = current_setting('app.user', true))               -- 用户私有
);

-- doc_block 同样的策略（反规范化的 owner_* 列使其可独立判定，
-- 无需 JOIN document —— 这也是 02-data-model.md §5.3 反规范化的理由之一）
CREATE POLICY block_visibility ON core.doc_block FOR SELECT TO PUBLIC
USING (
      (owner_tenant IS NULL AND owner_user IS NULL)
   OR (owner_tenant = current_setting('app.tenant', true) AND owner_user IS NULL)
   OR (owner_user = current_setting('app.user', true))
);

-- RLS 一旦开启，没有写入策略就等于禁止一切写入——包括写入中间件自己。
-- 归属校验在中间件里做（只有它知道当前请求属于谁），这里不重复限制。
-- DELETE 刻意不给策略：文档只做版本化，不做物理删除。
CREATE POLICY doc_insert   ON core.document  FOR INSERT TO PUBLIC WITH CHECK (true);
CREATE POLICY doc_update   ON core.document  FOR UPDATE TO PUBLIC USING (true) WITH CHECK (true);
CREATE POLICY block_insert ON core.doc_block FOR INSERT TO PUBLIC WITH CHECK (true);
CREATE POLICY block_update ON core.doc_block FOR UPDATE TO PUBLIC USING (true) WITH CHECK (true);
```

> **超级用户会无条件绕过 RLS，`FORCE` 也拦不住。** 因此 `core` / `asof` 下的表与视图
> 必须由**非超级用户** `app_owner` 持有——迁移默认以超级用户身份运行，不转移属主的话
> 上面这一整套策略仍然是一纸空文。属主转移见 `db/migrations/006_asof_views_and_roles.sql`，
> 由 `02-data-model.md` §9 的不变量 6 守着。
>
> 这一条是 P0 实测出来的：属主是超级用户时，`test_private_document_invisible_through_asof_view`
> 直接失败（u2 读到了 u1 的私有块）。

`app.tenant` / `app.user` 与 `app.as_of` 一样用 `SET LOCAL` 在事务内设置
（`03-point-in-time.md` §4.3），事务结束自动清除。

RLS 是**第二道防线**：`06-retrieval.md` §4.2 的 SQL 里已经带了 owner 条件，
RLS 保证即使那里写漏了也不会越权。两道防线都要有——检索 SQL 里的条件是为了
让索引能用上，RLS 是为了正确性。

### 3.3 私有材料不进公共知识

用户上传的材料**不得**：

- 进入 `entity_relation` 等公共知识表；
- 用于生成公共简报；
- 进入 `embedding_cache` 的共享缓存（私有内容的哈希缓存单独存储，按 owner 分区）。

最后一条容易漏：内容哈希缓存会跨用户复用，如果私有文档的向量进了共享缓存，
理论上可以通过哈希命中探测「某段文本是否被其他人上传过」。
`core.embedding_cache` 的主键因此包含 `owner_user` 列
（`05-document-pipeline.md` §5.3），私有内容写入自己的分区、不参与共享。

### 3.4 数据删除

用户要求删除其上传材料时：

1. 标记 `document.superseded_at = now()` 并记录删除请求；
2. 从对象存储删除原始文件；
3. **物理删除**该文档的 `doc_block` 行——这是全系统唯一允许物理删除的场景
   （`03-point-in-time.md` §3.2）；
4. 若有观点引用了这些块，观点一并标记失效并从输出中移除；
5. 全过程写 `audit.tool_call_log`，含请求人、执行人、时间。

删除是有审批的流程，不是一个 API。

## 4. 可信执行与审计

### 4.1 出网代理

所有对外网络调用经 `egress-proxy`（`01-architecture.md` §4）：

```yaml
# infra/egress-proxy/policy.yaml
allowlist:
  - host: api.tushare.pro
    inject_credential: TUSHARE_TOKEN
  - host: www.sec.gov
    inject_credential: null
  - host: api.textin.com                 # 文档解析（adr/0008）
    inject_credential: [TEXTIN_APP_ID, TEXTIN_SECRET_CODE]
    inject_as: header                    # x-ti-app-id / x-ti-secret-code
  - host: <model-api-host>
    inject_credential: MODEL_API_KEY
default_action: deny
log: full          # 记录 host、path、状态码、字节数、耗时；不记录请求体中的密钥
```

- **业务容器的环境变量中没有任何供应商密钥**——密钥只存在于代理层，
  由代理在转发时注入；
- 不在白名单的域名直接拒绝，拒绝事件告警。这同时是防数据外泄的机制：
  即使某个依赖被投毒，它也无法把数据发到任意地址。

### 4.2 密钥管理

| 规则 | 强制方式 |
|---|---|
| 密钥不进代码 | pre-commit 的 `detect-secrets` + CI 扫描 |
| 密钥不进日志 | 结构化日志的字段白名单，值默认脱敏 |
| 密钥不进测试夹具 | `tests/fixtures/` 的脱敏检查（`04-ingestion.md` §6） |
| 密钥不进 `provider_snapshot` | 入库前从 `params` 中剥离认证字段 |
| 轮换 | 每 90 天，流程写在 `infra/runbook-secrets.md` |

最后一条容易忽略：`provider_snapshot.params` 会原样记录调用参数，
如果供应商把 token 放在 query string 里，密钥就进了数据库。
入库前的剥离规则按 provider 配置。

### 4.3 执行轨迹哈希链

`audit.tool_call_log` 构成单向哈希链，任何篡改都会断链。

```python
def chain_hash(prev_hash: str, record: dict) -> str:
    """canonical_json 保证字段顺序、数值格式、空白字符稳定。"""
    payload = canonical_json({
        k: record[k] for k in (
            "run_id", "agent_name", "tool_name", "params", "as_of",
            "result_hash", "ok", "started_at", "ended_at",
        )
    })
    return sha256((prev_hash + payload).encode("utf-8")).hexdigest()
```

- 链的起点 `prev_hash = "0" * 64`；
- 写入用单一序列化的 writer（应用内加锁或数据库 advisory lock），
  避免并发导致链分叉；
- `audit` schema 对应用角色 `REVOKE UPDATE, DELETE`，只能追加；
- **每日校验任务**从头重算哈希链，不一致则告警。

```sql
-- 校验查询：找出第一个断链点
SELECT call_id, started_at
  FROM (
    SELECT call_id, started_at, prev_hash,
           LAG(hash) OVER (ORDER BY call_id) AS expected_prev
      FROM audit.tool_call_log
  ) t
 WHERE prev_hash IS DISTINCT FROM COALESCE(expected_prev, repeat('0', 64))
 ORDER BY call_id
 LIMIT 1;
```

哈希链不防篡改（有库权限的人可以重算整条链），它防的是**无声的篡改**——
任何修改都必须重写其后的全部记录，代价高且留下痕迹。P6 的机构版会把链的
周期性摘要外发到独立存储，届时才具备强防篡改能力。

### 4.4 成本审计

`tool_call_log.cost_cents` 支撑两件事：

1. **预算控制**（`07-agents.md` §3.2）：run 级与月度级；
2. **单位成本分析**：每篇简报、每个观点的模型成本。这个数字决定商业模式是否成立，
   P5 定价前必须有至少一个月的真实数据。

## 5. 机构版扩展（P6）

- **SSO**：OIDC 接入，租户级配置；
- **项目隔离**：在 `owner_tenant` 之下再加 `project_id` 维度，RLS 策略相应扩展；
- **审计日志导出**：租户管理员可导出本租户的全部 `tool_call_log`；
- **私有化部署**：架构上已就绪（[adr/0002](adr/0002-self-hosted-paradedb-single-db.md)），
  需要补的是许可证管理与离线模型端点配置；
- **许可证合规**：`pg_search` 是 AGPL-3.0，向机构交付私有化部署前需要完成
  许可证审查，见 [adr/0001](adr/0001-bm25-paradedb-pg-search.md) 的「风险」一节。
