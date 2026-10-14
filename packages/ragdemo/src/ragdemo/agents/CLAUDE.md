# packages/ragdemo/src/ragdemo/agents — Agent 定义与版本化 prompt（P2 占位；目前只有 prompt 存放目录在用）

## 这个包做什么
设计上是 P2 的 Agent 层（产业链映射 → 基本面 → 事件 → 多空辩论 → 观点入库，见 `docs/07-agents.md`）。
**现状：`__init__.py` 只有一行 docstring，没有任何 Python 实现。** 目前真正在用的只有 `prompts/` 目录，
且使用者是解析层而不是 Agent：表格描述与版面整理这两个小模型任务把 prompt 存在这里。

## 关键模块
| 路径 | 职责 |
|---|---|
| `prompts/table_describe/v1.md`、`v2.md`、`v3.md` | 表格一句话描述（检索召回用）；`parse/describe_llm.py` 的 `LlmTableDescriber` 默认加载 `v3` |
| `prompts/parse_structure/v1.md` | PDF 页版面整理（只排版、不改数字）；`parse/local/structure.py` 默认加载 `v1` |

## 对外入口
- 读取方式：`parse/describe_llm.py::load_prompt(version)` 与 `parse/local/structure.py` 按 `<目录>/<version>.md` 读文件并剥掉 YAML 头。
- 设计约束（尚无代码）：`agents/` 只能经 `tools/` 取数，**不得直接 import `retrieval/` 或写 SQL**（`docs/01-architecture.md` §5）。

## 本包的硬规则
- Prompt 文件路径固定为 `prompts/<agent>/<version>.md`，头部 YAML 写 `version`、`model_tier`、`expected_output_schema`（CLAUDE.md §3；现有文件即此格式）。
- **已发布的版本不要原地改**：新增 `vN+1.md` 并在头部 `changes_from_vN` 写清改动（`table_describe/v2.md`、`v3.md` 是范例），然后跑评测（CLAUDE.md §1.4）。
- Prompt 不得让模型产出数值（CLAUDE.md §1.2）；`parse_structure` 明确禁止新增/改写数字，`table_describe` 禁止解读词与买卖建议用词。

## 怎么测
仓库根目录运行，免费、无数据库、用假 HTTP：`pytest tests/parse/test_describe_llm.py`
（断言 v1/v2/v3 都能加载、默认版本为 v3）。没有 `tests/agents/`。真实模型调用要钱，评测见 `tests/evals`。

## 常见坑
- `table_describe/v3.md` 禁用「显示/说明/表明/反映」等动词，是因为合规拦截词表会把它们当解读词拦截（头部注明 2026-09-30 实测）；改词表与改 prompt 要一起考虑。
- 换默认版本要同时改 `describe_llm.py` 的 `prompt_version="v3"` 默认值与 `test_describe_llm.py` 里对 `"v3"` 的断言。
- 文档中的 `src/agents/prompts/...` 路径是简写，真实路径含包前缀 `packages/ragdemo/src/ragdemo/agents/prompts/`。
