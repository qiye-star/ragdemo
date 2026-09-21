# 07 · Agent 层与工具外壳

## 1. Agent 目录

| Agent | 输入 | 输出 | 模型档位 | 阶段 |
|---|---|---|---|---|
| 路由 | 用户问题 / 触发事件 | 意图类型 + 工具调用计划 | 小 | P2 |
| 产业链映射 | 事件、实体 | 受影响实体列表（加权由代码算） | 代码 + 小 | P3 |
| 基本面 | 实体、`as_of` | 关键指标变化、异常项、带来源 | 中 | P2 |
| 事件解读 | 新公告 / 新闻块 | `event` 记录、匹配的传导规则、方向/力度/时滞 | 中 | P3 |
| 宏观政策 | 政策文本 | 受影响环节与传导机制 | 中 | P3 |
| 多空辩论 | 上述输出 | 看多论据、看空论据、裁判结论、置信度 | 强（三角色） | P3 |
| 验证器 | 生成结果 | 通过 / 标注问题 | 小 + 代码 | P2 |
| 复盘评估 | 历史 `opinion` + 后续数据 | `opinion_score` | **代码为主** + 中 | P3 |

「模型档位」的含义：**小** = 路由/分类级小模型；**中** = 主力对话模型；
**强** = 最强推理模型。档位配置在 `config/models.yaml`，代码中只出现档位名不出现模型名。

**「代码 + 小」「代码为主」是关键标注**：产业链映射的加权、复盘评估的收益计算
全部由 Python 完成，模型只负责挑选与叙述（`CLAUDE.md` §1.2）。

## 2. Agent 输出契约

每个 Agent 的输出都是**结构化对象**，不是自由文本。自由文本无法验证、无法入库、
无法评测。

```python
# src/agents/schemas.py
@dataclass(frozen=True)
class Citation:
    """输出中每个事实的来源。二选一，不允许都为空。"""
    block_id: int | None = None
    fact_id: int | None = None
    page: int | None = None
    known_at: datetime | None = None

@dataclass(frozen=True)
class Claim:
    text: str
    citations: list[Citation]
    is_inferred: bool = False     # 无来源时必须为 True，且 text 中标注「推断」

@dataclass(frozen=True)
class FundamentalOutput:
    entity_id: str
    as_of: datetime
    highlights: list[Claim]
    metrics: list[MetricChange]    # 每项含 metric_id、期间、数值、同比、来源
    anomalies: list[Claim]
    prompt_version: str
    model: str

@dataclass(frozen=True)
class DebateOutput:
    bull_case: list[Claim]
    bear_case: list[Claim]
    verdict: Literal["bull", "bear", "neutral"]
    confidence: Literal["high", "medium", "low"]
    reasoning: str
    unresolved: list[str]          # 辩论中未能证实/证伪的关键分歧
```

`DebateOutput.unresolved` 是刻意设计的：**多空辩论最有价值的产出往往不是结论，
而是"哪些关键问题现有数据回答不了"**。它进入简报的「待跟踪」小节，
也是下一轮数据采购的需求来源。

`Claim.is_inferred` 与合规过滤器联动：`is_inferred = True` 的 Claim
在渲染时强制加「推断」前缀（`09-compliance-security.md` §1）。

## 3. 工具外壳层

### 3.1 接口

Agent **只能**通过这些工具访问数据（`01-architecture.md` §5 的依赖约束）。

```python
# src/tools/interface.py
class AgentTools(Protocol):

    def query_fin_fact(self, *, entity_id: str, metric_id: str,
                       period: str | None = None,
                       period_range: tuple[date, date] | None = None,
                       as_of: datetime) -> list[FactRow]:
        """查结构化财务/指标数据。period 与 period_range 二选一。"""

    def search_blocks(self, *, query: str, as_of: datetime,
                      entity_ids: list[str] | None = None,
                      doc_types: list[str] | None = None,
                      k: int = 10) -> list[EvidenceBlock]:
        """文本检索，转调 RetrievalService。"""

    def get_relations(self, *, entity_id: str,
                      relation_type: str | None = None,
                      as_of: datetime,
                      min_confidence: str = "medium") -> list[RelationRow]:
        """查实体关系图。低于 min_confidence 的关系不返回。"""

    def match_rules(self, *, event_type: str, trigger_node: str) -> list[PropagationRule]:
        """匹配传导规则。纯查询，无模型参与。"""

    def compute(self, *, expr: str, inputs: dict[str, Decimal]) -> ComputeResult:
        """受限表达式求值。见 §3.3。"""

    def write_opinion(self, *, opinion: OpinionDraft) -> int:
        """写入观点。需人工确认节点，见 §4.3。"""
```

**每个读取工具的 `as_of` 都是必填关键字参数，没有默认值。**
签名层面的强制比代码审查可靠。

### 3.2 外壳层职责

```python
class ToolShell:
    """所有工具调用的统一入口：校验 → 权限 → 预算 → 缓存 → 执行 → 审计。"""

    def invoke(self, run_id: str, agent_name: str,
               tool: str, params: dict) -> ToolResult:
        # 1. 参数校验：类型、必填、as_of 带时区、k 在合理范围
        # 2. 权限：按 run 上下文的 tenant/user 注入过滤条件
        # 3. 预算：累计成本超过 run 预算时抛 BudgetExceeded
        # 4. 缓存：(tool, 规范化 params) 命中则直接返回，run 内有效
        # 5. 执行：在 as_of_session 中执行
        # 6. 审计：写 audit.tool_call_log，含哈希链
        ...
```

缓存的 key 必须包含 `as_of`。同一 run 内 `as_of` 固定，因此缓存安全；
跨 run 不复用，避免时点混淆。

**预算控制**：每个 run 有 token 与金额上限（配置在 `config/budgets.yaml`）。
超限时抛异常终止 run，而不是静默截断——截断会产出「看起来完整但证据不全」的观点，
这比失败更危险。

### 3.3 `compute` 的安全约束

`compute` 是「计算与生成分离」原则的落点：模型给出表达式与输入名，代码求值。

```python
ALLOWED_OPS = {"+", "-", "*", "/", "**"}
ALLOWED_FUNCS = {"abs", "min", "max", "sum", "round"}
MAX_EXPR_LEN = 200
```

- 用 `ast.parse` 解析后遍历节点白名单校验，**不用 `eval`**；
- 输入必须是 `Decimal`，不是 `float`——财务数值用浮点会在同比、占比计算中
  累积可见误差；
- 除数为 0 返回明确的 `ComputeResult(ok=False, error=...)`，不抛异常也不返回 NaN；
- 结果携带完整的输入溯源：`ComputeResult.inputs` 记录每个输入值来自哪个
  `fact_id` 或 `block_id`。这样「同比 +58.2%」这个数字本身也可溯源到两个原始值。

## 4. 编排

### 4.1 分层

```
Orchestrator (自有接口)          ← agents/ 只认识这一层
      │
      ▼
LangGraphOrchestrator            ← 唯一 import langgraph 的模块
      │
      ▼
LangGraph StateGraph + PostgresSaver (schema: orchestration)
```

见 [adr/0003](adr/0003-langgraph-with-own-orchestrator.md)。

```python
# src/agents/orchestrator.py
class Orchestrator(Protocol):
    def run(self, graph_name: str, initial_state: dict,
            run_id: str, as_of: datetime) -> RunHandle: ...
    def resume(self, run_id: str, decision: HumanDecision) -> RunHandle: ...
    def get_state(self, run_id: str) -> RunState: ...
```

### 4.2 事件解读图

```
        [load_event]
             │
             ▼
     [chain_mapping]  ← 代码加权，无模型
             │
      ┌──────┴──────┐
      ▼             ▼
[fundamental]   [event_interp]      并行
      └──────┬──────┘
             ▼
       [debate_bull] ──┐
       [debate_bear] ──┼─► [debate_judge]
                       │
             ┌─────────┘
             ▼
        [validator]  ← 代码 + 小模型
             │
      ┌──────┴──────┐
   通过│             │不通过
      ▼             ▼
[await_human]   [log_correction] ──► END
      │
      ▼
[write_opinion] ──► END
```

### 4.3 人工确认节点

`write_opinion` 前的 `await_human` 是**中断点**：图在此暂停并持久化状态，
等待人工在 CLI（P1–P3）或 Web（P4 起）确认。

- 未确认的观点 `confirmed_by IS NULL`，**不得进入任何对外输出**
  （`02-data-model.md` §6 有对应索引供查询待办）；
- 确认超时（默认 7 天）的 run 自动归档，观点保留但标记为过期；
- 这个节点是 [adr/0003](adr/0003-langgraph-with-own-orchestrator.md) 选择 LangGraph
  而非纯自写状态机的主要理由——中断与恢复的正确实现比看上去难。

### 4.4 Prompt 版本化

```
src/agents/prompts/
  fundamental/
    v1.md
    v2.md
    CHANGELOG.md
  debate_bull/
    v1.md
  ...
```

- 每个 prompt 文件顶部的 YAML front matter 声明 `version`、`model_tier`、
  `expected_output_schema`；
- 运行时加载的版本记录进 `opinion.prompt_version` 与 `tool_call_log`；
- **改 prompt 必须跑评测并把数字贴进 PR**（`CLAUDE.md` §1.4）；
- `CHANGELOG.md` 记录每个版本改了什么、评测数字如何变化。没有这个文件，
  三个月后没人知道 v3 为什么比 v2 好。

## 5. 引用验证器

验证器在输出离开系统前运行，是溯源约束的执行者。它**以代码为主**，
小模型只用于最后一步的语义一致性抽查。

### 5.1 检查项

| # | 检查 | 手段 | 失败处理 |
|---|---|---|---|
| 1 | 输出中每个数值都出现在 citations 指向的原文里 | 正则提取数值 + 归一化（千分位、单位、百分号）后在原文中匹配 | 拦截 |
| 2 | 每个 `Citation` 的 `block_id` / `fact_id` 真实存在 | 查库 | 拦截 |
| 3 | 每个引用的 `known_at <= opinion.as_of` | 查库 | **拦截并告警**——这是时点泄漏 |
| 4 | 无来源的数值被标记 `is_inferred` | 遍历 Claim | 自动补标 + 记 `correction_log` |
| 5 | 输出不含违禁词 | 词表匹配 | 拦截 |
| 6 | 引用的原文确实支持该论断（而非仅包含相同数字） | 小模型逐条判定 | 标注低置信度，不拦截 |

第 3 项是最重要的一条。它与 `03-point-in-time.md` §5 的第 5 条自检查询重复，
这是有意的**双重防线**：验证器在生成时拦截，自检查询在事后审计。

第 6 项不拦截的原因：小模型的判定本身有误差，用它做硬拦截会误伤。
它的产出进入人工确认界面，由人决定。

### 5.2 数值归一化

检查项 1 的难点在于同一个数字有多种写法。归一化规则：

```
"12,340"  →  12340
"1.234 万" →  12340
"12.34 亿" →  1234000000
"+58.2%"  →  0.582
"(3.1)"   →  -3.1        （财报中括号表示负数）
```

匹配时允许 ±0.5% 的相对误差，容纳四舍五入差异。**超出误差即视为不匹配**——
Agent 把 12,340 写成 12,400 是必须拦截的错误。

## 6. 输出模板

| 模板 | 触发 | 阶段 |
|---|---|---|
| 公司追踪简报 | 单实体，定期或按需 | P2 |
| 环节周报 | L3 环节，每周 | P3 |
| 事件解读 | 事件驱动 | P3 |
| 一二级对标报告 | 按需 | P6 |
| Excel 导出（保留公式） | 按需 | P4 |

模板渲染用 Jinja2，输出 Markdown 与 HTML 两种（`templates/`）。
P1–P3 经邮件 / 飞书推送（[adr/0006](adr/0006-no-frontend-until-p4.md)）。

**每个模板必须包含的固定小节**：

1. **数据时点**：本文使用的 `as_of`，以及各数据源的最新 `known_at`；
2. **依据**：全部引用的来源清单，含文档标题、页码、发布时间；
3. **待跟踪**：`DebateOutput.unresolved` 的内容；
4. **免责声明**：本文为研究参考，不构成投资建议。

第 1 项让读者知道「这篇简报是站在哪一天写的」。缺了它，一篇三天前生成的简报
会被误读成今天的判断。

## 7. 增量触发

Agent 流程**只由新数据触发**（`CLAUDE.md` §1.6）。触发条件：

| 触发源 | 条件 | 启动的图 |
|---|---|---|
| 新公告入库 | `doc_type` 在关注列表且实体在覆盖范围 | 事件解读图 |
| 新财报入库 | 定期报告类 `doc_type` | 基本面图 → 事件解读图 |
| 新政策文件 | 政策源的新文档 | 宏观政策图 |
| 新闻 | 命中关键词规则且实体可解析 | 事件解读图（轻量档） |
| 定时 | 每周一 08:00 | 环节周报图 |
| 定时 | 每日 02:00 | 复盘评估（对到期的 1M / 3M 观点） |

### 7.1 去重

同一事件常被多个来源报道（公告 + 三家新闻）。去重规则：

1. 按 `(trigger_entity, event_type, publish_at 所在的 24 小时窗口)` 聚合；
2. 组内保留信息源等级最高的一条（公告 > 财报 > 权威媒体 > 一般新闻）作为
   `core.event` 的主记录，其余作为附加证据块挂上去；
3. 已有 `event` 在 24 小时内不重复生成观点。

没有去重，一条台积电扩产新闻会触发七八次辩论，把预算烧光。

### 7.2 背压

事件队列积压超过阈值（默认 50 条待处理）时，**降级而非丢弃**：
暂停辩论图（最贵的一环），只跑事件解读与入库，待队列回落后补跑。
降级事件记入结构化日志并告警。
