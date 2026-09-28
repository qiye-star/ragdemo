"""数据源分工与降级（ADR-0012）。

`routing.ROUTES` 是分工表：每项数据按层列出可用的 MCP 服务。`router.SourceRouter`
按层尝试，失败按 `failures.classify` 归类、在 `health` 里记冷却，再切下一层。
"""
