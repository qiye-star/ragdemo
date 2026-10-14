# packages/ragdemo/src/ragdemo/templates — 简报 / 周报 / 对标模板与渲染（P2 占位，尚无实现）

## 这个包做什么
设计上放 Jinja2 模板，把 Agent 输出渲染成 Markdown 与 HTML（`docs/07-agents.md` §6）。
**现状：只有一行 docstring 的 `__init__.py`，无模板、无代码、无测试。**

## 关键模块
暂无。

## 对外入口
将来被 `agents/` 或 `api/` 调用。本包只做渲染，不取数、不算数。

## 本包的硬规则（实现时必须守）
- 每个模板必须有四个固定小节：数据时点（`as_of` 与各来源最新 `known_at`）、依据（含文档标题/页码/发布时间）、待跟踪、免责声明「研究参考，不构成投资建议」（`docs/07-agents.md` §6）。
- 渲染前后都要过合规拦截与引用验证：禁买卖建议/目标价/评级/仓位词；每个数字绑定 `[block_id | page]` 或 `[metric_id | source | known_at]`，否则标 `推断`（CLAUDE.md §0、§1.3）。
- 模板里不得做数值计算，数值由代码算好传入（CLAUDE.md §1.2）。

## 怎么测
暂无测试；实现时放 `tests/templates/`，并含固定小节存在性与违禁词拦截的用例。

## 常见坑
- Excel 导出（保留公式）排在 P4，不在 P2 范围；P1–P3 通过邮件/飞书推送，无面向用户前端（adr/0006）。
