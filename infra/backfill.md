# 回填手册（阶段 2.9）

从空库到"阶段二数据齐全"的命令、顺序、限速与验收。所有命令默认 **dry run**（不调外部、不写库），
加 `--yes` 才执行；写入全部幂等，中断后重跑即可。命令前先加载环境：
`set -a && . ./.env && set +a`（Windows PowerShell 用 `Get-Content .env` 自行导入）。

> 出网一律经 `ragdemo-egress` 代理（凭据由代理注入，代码里没有密钥）。代理只在容器启动时读取
> `.env`：改了密钥后要**只重建 `ragdemo-egress` 容器**并带 `--env-file .env`，新密钥才生效。

## 0. 前置

```bash
uv run ragdemo db migrate      # 执行未执行的迁移（当前到 034）
uv run ragdemo db seed         # 种子：实体、口径、关系、规则
uv run ragdemo db check        # schema 不变量 + 时点泄漏自检，必须"全部检查通过"
```

## 1. 顺序与依赖

| 步 | 命令 | 依赖 | 外部服务 | 规模（2026-09-29 实测） |
|---|---|---|---|---|
| 1 | `docs announcements --since 2023-01-01 --until <今天> --yes` | 种子 | 巨潮、披露易、EDGAR（免费） | 96 家实体，入目录 38191 条 |
| 2 | `facts ingest --quarters 12 --yes` | 种子 | Wind MCP（**耗额度**，每实体 1 次） | 13 家 × 12 个季度，现行 1245 条 |
| 3 | `facts prices --since 2023-09-01 --until <今天> --yes` | 种子 | Tushare MCP（免费） | 59 家 A 股，43122 行 |
| 4 | `facts fx --since 2023-01-01 --until <今天> --yes` | 无 | 中国货币网 CFETS（免费） | 5 币种 × 908 个交易日，4540 行 |
| 5 | `facts holders --yes`、`facts segments --yes` | **步 1**（首次披露日）；分部还依赖**步 2**（营收对账） | Wind MCP（**耗额度**） | 试点两家：股东 160 行、分部 27 行；全量上限见第 3 节 |

步 5 依赖步 1 是硬约束：没有公告目录就没有可信的 `known_at`，对应期别不会调用 Wind、不入库。

## 2. 限速与并发上限

**全部单进程串行，不要并发跑同一个命令**（写入虽幂等，但供应商限速与冷却状态是全局的）。

| 来源 | 限制（实测） | 代码里的处理 |
|---|---|---|
| 巨潮 | 无公开限速；`pageSize` 30，翻页 | 令牌桶 60 次/分钟；User-Agent 必须带真实联系方式（`RAGDEMO_CNINFO_USER_AGENT`） |
| 披露易 | `rowRange` 上限 500，超限即截断 | 日期窗口二分，直至装得下；`recordCnt` 硬对账 |
| EDGAR | SEC 要求带联系方式的 User-Agent，限速 10 次/秒 | 令牌桶 60 次/分钟；`filings.files` 分页文件按窗口补取 |
| Tushare MCP | `daily` 可用；`stock_basic` / `trade_cal` / `adj_factor` **限频 1 次/小时**；财务、汇率、公告接口**无权限** | 三层降级（MCP → REST → Wind K 线）；回填命令**关闭 Wind K 线层**（逐日调用耗额度）；被拒后该层冷却，其余实体快速失败 |
| Wind MCP | **按调用耗额度**；自然语言接口，响应形态不稳定 | 每次响应先落快照；连续 3 次网关失败中止整批；`--from-snapshots` 用留存快照免费重解析 |
| CFETS | **`pageSize` ≥ 100 返回 403**（与频率无关）；只在交易日有值 | 页大小固定 50，按半年分窗；令牌桶 30 次/分钟 |
| TextIn | 0.1356 元/页（2026-10-13 实测，待发票复核；原 0.04 为低估）；月预算 `MONTHLY_BUDGET_CNY` | `--max-pages` 页数预算；只对试点实体下载并解析 |

## 3. Wind 额度：先估再跑

```bash
uv run ragdemo facts holders                 # dry run：输出"最多调用 Wind N 次"
uv run ragdemo facts segments
uv run ragdemo facts holders --entity-ref 688041.SH --entity-ref 000977.SZ --yes   # 先小后大
```

N 已排除"库里已有"与"公告目录里找不到该期报告"的期别。首次全量（59 家 A 股）实测上限：
股东 449 次、分部 225 次。Wind 解析被拒（列名写法变了）时，改进解析规则后**不要重新调用**：

```bash
uv run ragdemo facts holders  --from-snapshots --yes   # 用留存的同问句响应重新解析
uv run ragdemo facts segments --from-snapshots --yes
```

## 4. 验收（回填之后必跑）

```bash
uv run ragdemo docs verify-announcements --since 2023-01-01 --until <今天>   # 来源自报总数 = 库里行数
uv run ragdemo facts verify-stage2                                            # 股东/分部/汇率/财务 known_at 体检
uv run ragdemo facts crosscheck-holders                                       # 股东 vs 已解析公告原文
uv run ragdemo db check
uv run pytest -q && uv run ruff check . && uv run mypy --strict packages tests
```

`verify-*` 有任何错误以非零码退出。警告（如分部没有对应期营收、未对账）要读，不要忽略。

## 5. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 某层"冷却中" | 上次被拒（无权限 / 限频），`core.source_health` 里有冷却截止时间；等到期重跑，不要清表硬绕 |
| `facts holders` 报"没有披露记录，未调用 Wind" | 该期定期报告不在公告目录（早于 2023-01-01，或标题写法未覆盖）；先看 `docs announcements` 是否已覆盖该实体 |
| CFETS 返回 403 | `pageSize` 超过 50（适配器已固定 50）；不是 IP 被封 |
| 中断后怎么续 | 直接重跑同一条命令：写入幂等、已入库的期别不再调用外部 |
| 想把更早的公告补进来 | 把 `--since` 提前重跑即可：覆盖下界水位线（`announcements-floor`）会自动整段重取一次 |
