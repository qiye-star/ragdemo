# 阶段 1 验收记录 · 地基补漏 + 数据契约

- 日期：2026-09-28
- 依据：`docs/9.28-plan/2026-09-28-数据底座完善总计划.md` 阶段 1
- 起点提交：`41c5d50`（阶段 0 完成）；终点提交见文末

## 结论

**工程部分全部完成并通过；出口验收三项里两项通过，一项等外部签字。**
按总计划 §0.1，阶段 1 **尚不能宣布通过**：需要你决定是等待 Agent 负责人签字，
还是"带条件放行"进入阶段 2（阶段 2 本身另有一个外部阻塞，见"阶段 2 入口"）。

| 出口验收 | 结果 |
|---|---|
| 上表全部单测 / 集成测试通过 | ✅ `make test`：960 passed / 1 skipped / 4 deselected |
| 契约文档定稿 | ⏳ 草案已写（`docs/9.28-plan/数据接口契约-v1.md`），**待 Agent 负责人评审签字** |
| `provider_snapshot` 写入路径覆盖率 100%（扫描测试） | ✅ `tests/test_snapshot_coverage.py` 识别全部 10 个取数点，删掉任意一处快照写入即失败 |

三道门：

```
$ make lint       → All checks passed! / 245 files already formatted
$ make typecheck  → Success: no issues found in 244 source files
$ make test       → 960 passed, 1 skipped, 4 deselected in 685.98s
```

- 1 skipped：7 天双跑一致性测试，北京时间 2026-09-28 22:11 之后可复跑（阶段 4.5）。
- 4 deselected：`seed_full` 标记的实体 / 关系计数断言，等创始人录入（阶段 6）。
- 测试总数 843 → 960（+117）。

---

## 任务逐项

| # | 任务 | 结果 | 提交 |
|---|---|---|---|
| 1.1 | 原始响应快照 | ✅ | `a595f27` `552ff39` `aae3ce5` |
| 1.2 | 口径版本化 | ✅ | `862939a` |
| 1.3 | 口径 Excel 导入命令 | ✅ | `7e62842` |
| 1.4 | `ingested_at` 静态守卫 | ✅ | `5051d83` |
| 1.5 | 元数据校验第 7、8 条 | ✅ | `16db967` |
| 1.6 | 月度成本预算 | ✅ 机制完成；⚠️ TextIn 单价未配置 | `d5df860` |
| 1.7 | 交易日历 | ✅ | `9e317b0` |
| 1.8 | 契约文档 | ✅ 草案；⏳ 待签字 | `2c09f73` |
| 1.9 | 契约 Mock + 契约测试 | ✅ | `2c09f73` |

### 1.1 原始响应快照

每一次向供应商取数，先把原始响应写进 `core.provider_snapshot`（**立即提交**，解析或下游
写库失败都不会丢），再解析；解析出的每一行事实 / 行情 / 文档带 `snapshot_id` 指回那次响应
（迁移 `017`，`fin_fact` / `price_daily` / `document` 新增外键列，asof 视图同步）。

| 取数路径 | 快照内容 |
|---|---|
| 财务事实资产、行情资产 | 原始响应内联 jsonb |
| 公告资产 | 每份公告一条 |
| 本地 manifest（`docs ingest --yes`） | manifest 原始记录 + PDF 在 blob 中的位置；dry run 不写 |
| EDGAR 申报列表 | 内联 jsonb |
| EDGAR 申报全文 | `response_ref` 指向已存的全文字节（逻辑从 CLI 抽到 `ingest/edgar_docs.py`，首次有测试） |
| TextIn 解析（含 C 档重解析） | 每次**真实计费**的解析一条，`response_ref` 指向 xParse JSON，`cost_cents` = 计费页数 × 单价；缓存命中不记 |

验收：每条路径都有"任取一行能从快照回放出同一行"的测试；超过 1 MB 的响应落 blob 且可回放；
调用参数里混进的 token 不会落库。

**顺手修掉的真实缺陷**：`save_snapshot()` 不传 blob 时，超大响应只记一个指向空处的
`response_ref`——原始响应等于没存。取数路径统一走的 `record_snapshot()` 现在强制要求 blob。

### 1.2 口径版本化（迁移 018）

指标定义、供应商字段映射（含换算系数）、AI 收入拆分规则改为只追加的版本记录，登记为
时点表、配 asof 视图。改口径 = 追加新版本 + 旧版本 superseded，旧版本永久保留。
存量与种子口径的版本 1 取固定基线 2000-01-01（在任何历史 as_of 下可见），与种子实体
是同一个有意接受的乐观偏差（docs/03 §6.3）。

验收：同一指标两版口径，`as_of` 在新版本之前查到旧定义、之后查到新定义 ✅；
乱序写入被拒；重复导入相同内容不产生新版本；迁移对已有库的存量行回填正确。
实库已执行迁移，30 个指标均有版本 1，`ragdemo db check` 全部通过。

### 1.3 口径 Excel 导入

```bash
ragdemo metrics export-template --out 口径.xlsx
ragdemo metrics import --file 口径.xlsx --author <你的名字>            # 只预览差异
ragdemo metrics import --file 口径.xlsx --author <你的名字> --note "说明" --yes
```

三张可编辑表（指标定义 / 字段映射 / AI 收入规则）+ 说明表。导入先校验并**一次报全**
所有问题（每行的每个错字段、不在册的环节名、引用了不存在的指标或实体、重复键），再逐字段
展示"修改前 → 修改后"；`--yes` 才写库，整批一个事务，任一行被拒整批回滚。表里删掉的行
不会删除口径。走命令行而不是网页上传（ADR-0010 规定诊断界面只读）。

实库冒烟：导出现有 30 个指标再原样导入 → "30 行不变"，未写库。

### 1.4 `ingested_at` 静态守卫

`tests/test_no_ingested_at_in_filters.py`：`ingested_at` 作为比较操作数出现在任何 SQL
（Python 字符串字面量、f-string、迁移文件）或 Python 比较表达式里即失败。验证：在
`retrieval/filters.py` 植入 `ingested_at <=`，守卫失败并指出文件。

### 1.5 元数据校验（纠正了审计结论）

- **规则 8 其实早已实现**：`vector_coverage_check` 是嵌入资产之后的阻断型检查，阈值 1.0，
  正是 05 §6 要求的形态。审计时只看了 `validate.py`，结论有误，本项不需要新代码。
- **规则 5 只实现了一半**：一致性检查只比 `entity_id` / `doc_type` / `known_at`，漏了
  `publish_at` 与 `owner_*`。**`owner_*` 漏检是隐私风险**：RLS 按块自己的 owner 列判定
  可见性，私有文档下一个 owner 为空的块会被当成公共数据检索出来。已补齐，实库通过。

### 1.6 月度成本预算

- 账本：`provider_snapshot.cost_cents`（免费来源显式记 0，NULL 只表示"计费但单价未知"）。
- 本月（北京时间自然月）已用 ≥ 80%：质量看板 `monthly_cost_ratio` 记为未通过（告警）。
- ≥ 100%：所有计费调用在发起前拒绝——管线 TextIn 解析、C 档重解析、
  `docs ingest --yes --parser textin`。
- 上限：`RAGDEMO_MONTHLY_BUDGET_CNY`，默认 5000。

验收：注入超预算数据，`doc_prepared` 资产在调用解析器之前抛 `BudgetExceeded`（解析器
调用次数 0）✅；CLI 拒绝执行且不写任何文档 ✅。

**⚠️ `TEXTIN_COST_PER_PAGE_CNY` 仍未配置**（商务单价，需要你提供）。在此之前 TextIn
调用的成本记为"未知"，预算看板会持续告警，无法判断是否超支。

### 1.7 交易日历

`ragdemo.dataapi.calendar.last_trading_day(conn, as_of=, markets=)`：从 `asof.price_daily`
推导，周末 / 节假日 / 跨年 / 盘中 / 跨市场隔离 / 停牌行都有单测。

### 1.8–1.9 数据接口契约 v1

- 11 个只读接口（`ragdemo.dataapi.contract.DataApi`），全部强制带时区的 `as_of`；数值一律
  `Decimal` 且带溯源；私有材料不可见；不可展示原文的来源不给正文。
- `MockDataApi` 装载覆盖全部边界的样例数据集，Agent 负责人现在就能用。
- `tests/contracts/dataapi_contract.py`：29 条契约断言，真实实现以后跑同一套。验证过它
  有牙齿：让 Mock 忽略私有归属、或忽略更正，各有 3 条测试失败。
- 同比由代码计算（`metrics/derived.py`），负基数 / 零基数 / 缺期 / 单位不一致时不给数、
  写明原因。
- 文档中的使用示例已照原文实际执行，输出与文档一致。

---

## 本阶段新发现（记入后续阶段）

| # | 发现 | 处理 |
|---|---|---|
| 1 | **Tushare 缓存代理**（你给的 `https://tushare.a.finovadeep.com/`）TLS 证书只签给 `a.finovadeep.com`，不覆盖 `tushare.a.` 子域名，任何校验证书的客户端都连不上。我**没有**关掉证书校验去发送你的 key | 需要服务端补证书（见"阶段 2 入口"） |
| 2 | 它不是 MCP 网关，而是**兼容 Tushare 原生接口**的缓存代理（`POST /api/query`，请求体 `api_name` / `token` / `params` / `fields`），公开注册表列出 198 个接口：A 股三大报表、主营构成、前十大股东、行情、复权因子、交易日历、港股 / 美股财务报表、**汇率**、公告（`anns_d`）、新闻等 | 阶段 2 按 Tushare 直连方式接入，覆盖面比原计划更大 |
| 3 | key 已存入被 git 忽略的 `.env`，变量名 `TUSHARE_CACHE_PROXY_TOKEN`（只给出网代理注入用）。它在对话里明文出现过 | 建议证书修好后在代理后台轮换一次 |
| 4 | `api/queries/quality.py` 注释里说有测试核对指标注册表与源码里的指标名，**该测试不存在** | 阶段 4 补 |
| 5 | 全量测试从约 3 分钟涨到约 11.5 分钟（每个库测试都新建临时库并跑完 18 条迁移） | 阶段 4 接 CI 前优化（共享模板库） |
| 6 | 研报（`kind = research`）在 `load_documents` 里已被代码无条件排除（著作权） | 总计划 3.5 部分已有结论 |

---

## 阶段 2 入口条件现状

总计划：阶段 1 通过 + 拿到 Tushare 令牌或 MCP 网关。

- 数据源：你提供的是 Tushare 缓存代理，**当前因证书问题无法安全连接**。需要服务端为
  `tushare.a.finovadeep.com` 签发证书（如 Let's Encrypt 加这个域名），修好后我先用只读的
  `/api/registry` 与一次小查询验证 key 的权限覆盖哪些接口。
- 阶段 1 的契约签字未完成。

## 需要你决定 / 提供

| # | 事项 | 阻塞 |
|---|---|---|
| 1 | 契约文档交 Agent 负责人评审签字，或书面决定"带条件放行" | 阶段 1 出口 |
| 2 | 让代理服务端修证书（`tushare.a.finovadeep.com`） | 阶段 2 入口 |
| 3 | TextIn 单页价格 `TEXTIN_COST_PER_PAGE_CNY` | 预算判断 |
| 4 | 硅基流动旧 key 作废确认（阶段 0 遗留） | 阶段 5 |
| 5 | 创始人录入（实体 / 别名 / 关系 / 评测用例） | 阶段 5、6 |
