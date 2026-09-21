# ADR-0003 · Agent 编排用 LangGraph + 自有封装

- 状态：已接受
- 日期：2026-09-21
- 决策者：创始团队

## 背景

Agent 层需要编排的流程不是线性的：产业链映射后要并行跑基本面与事件解读，
多空辩论有三个角色互相引用彼此的输出，`write_opinion` 前有**人工确认中断点**，
任一节点失败要能从检查点恢复而不是从头重跑。

简报 §12 第 3 问：用 LangGraph 还是自写状态机。

## 备选方案

**A. LangGraph + 自有 `Orchestrator` 接口封装**
用 LangGraph 的 `StateGraph` 与 checkpointer，但 `agents/` 只依赖自有的
`Orchestrator` 协议，只有一个模块 import `langgraph`。

**B. 自写状态机**
自己实现节点调度、并发、重试、检查点、中断恢复。

**C. 裸用 LangGraph**
不加抽象层，业务代码直接用框架 API。

## 决策

**选 A。**

选 LangGraph 而非自写的关键是**人工确认中断点**（`07-agents.md` §4.3）。
「图执行到某节点暂停、持久化全部状态、等待数天后由外部输入恢复执行」
这件事看起来简单，正确实现要处理：状态序列化、并发分支的部分完成、
恢复时的幂等、超时归档。自己写至少一到两周，且这类代码的 bug 往往在
生产环境才暴露。这不是我们的差异化所在。

加封装层而非裸用，是因为 LangGraph 仍在快速迭代，API 有过破坏性变更。
把它隔离在一个模块里，换框架时改一个文件而不是改遍 `agents/`。

封装层的边界要克制：`Orchestrator` 协议只有 `run` / `resume` / `get_state`
三个方法（`07-agents.md` §4.1）。**不要试图抽象出一个通用的图 DSL**——
那等于重新实现 LangGraph，既没省事也没换来可移植性。

## 后果

1. `src/agents/langgraph_orchestrator.py` 是**唯一** import `langgraph` 的模块。
   CI 中有静态检查强制这一点。
2. 检查点用 `langgraph-checkpoint-postgres`，指向我们自己的数据库
   `orchestration` schema（`02-data-model.md` §1.1）。状态不出我们的库，
   与 `01-architecture.md` §3 的「LangGraph 状态存 schema `orchestration`」一致。
3. **Dagster 与 LangGraph 不互相调用**，衔接点是 `core.event` 表
   （`01-architecture.md` §3）。这条边界比框架选择本身更重要：
   它让数据管线不会被模型调用的延迟和失败拖垮。
4. 图定义（节点、边、条件分支）写在我们的代码里，是业务资产；
   框架只提供执行器。换框架时图的逻辑可以直接搬。
5. 工具调用**不走 LangGraph 的 tool 机制**，走我们自己的 `ToolShell`
   （`07-agents.md` §3）。外壳层的校验、预算、审计是硬需求，
   不能依赖框架的实现细节。

## 推翻条件

- LangGraph 的破坏性变更频率高到封装层本身成为维护负担
  （表现为：一年内因框架升级修改封装层超过 3 次且每次都涉及语义变化）；
- 框架的检查点实现无法满足我们的审计要求（例如无法与 `tool_call_log`
  哈希链对齐）；
- 图的复杂度始终停留在「顺序 + 一个并行分支」的水平，框架的价值不足以
  抵消依赖成本——此时改为自写状态机是合理的简化；
- 需要的执行语义（如跨进程的分布式节点执行）超出框架能力。
