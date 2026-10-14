# packages/ragdemo/src/ragdemo/tools — Agent 工具外壳层（P2 占位，尚无实现）

## 这个包做什么
设计上是 Agent 访问数据的唯一关卡：参数校验 → 权限 → 预算 → 缓存 → 执行（在 `as_of_session` 内）→ 审计
（`docs/07-agents.md` §3）。**现状：只有一行 docstring 的 `__init__.py`，没有任何代码、也没有测试。**
目前可被 Agent 使用的只读接口是 `dataapi/` 的 `DataApi`；工具外壳应当包在它（及 `compute` 等）之外。

## 关键模块
暂无。设计里的 `tools/interface.py`（`AgentTools` Protocol）与 `ToolShell` 都还不存在。

## 对外入口
- 将来被 `agents/` 唯一调用；本包向下可依赖 `retrieval/`、`metrics/`、`entities/`、`dataapi/`（依赖方向见 `docs/01-architecture.md` §5）。
- 不得被 `retrieval/`、`ingest/` 等下层 import。

## 本包的硬规则（实现时必须守，规范已写死）
- 每个读取工具的 `as_of` 是必填关键字参数、无默认值、带时区（CLAUDE.md §1.1；`docs/07-agents.md` §3.1）。
- 缓存 key 必须含 `as_of`；预算超限抛异常终止 run，**不得静默截断**（`docs/07-agents.md` §3.2）。
- 每次调用写 `audit.tool_call_log`（经 `audit/`），日志含 `run_id`、`as_of`、`entity_id`（CLAUDE.md §3）。
- `compute` 只做受限表达式求值，数值由代码算（CLAUDE.md §1.2）。

## 怎么测
暂无测试。实现时先写失败测试（CLAUDE.md §3 TDD），建议放 `tests/tools/`。

## 常见坑
- 先别在这里重造数据查询：`dataapi/contract.py` 已定义了带溯源、强制 `as_of` 的返回类型，工具层应复用。
- `config/budgets.yaml`（`docs/07-agents.md` 提到的预算配置）目前不存在；`quality/budget.py` 是解析页数预算，与此无关。
