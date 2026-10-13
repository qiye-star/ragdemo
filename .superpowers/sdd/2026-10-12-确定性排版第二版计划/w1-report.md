# W1 报告：逐页验收 P1-P8

状态：完成。文件：parse/acceptance.py、tests/parse/test_acceptance.py（29 例全过）、docs/10.12-plan/验收记录/逐页验收定义.md、路由计划末尾「W1 迁移 / 记账字段设计」。
ruff check/format、mypy --strict 通过。

保守解读（详见模块 docstring）：
- 6 种结论：accepted / accepted_with_scan / partially_accepted / textin_unverified（S5）/ rejected / empty_document。
- P1：L1/L2/L3 必须有原因；文档级并入缺页/重复页/forced_l3 检查。
- P2：failed 与 not_run 都算未过；L0/L1 无文字层算未过；L3 推给 P3。
- P3：多重集合子集；无文字层 = unverifiable_scan 且 passed=False；含 TextIn。
- P4：阈值不预填，None -> not_evaluated（不算通过，只告警）；stripped_text 仅在有 figure 时扣分母。
- P7：meta 缺失按失败。

关注点：PageKind 枚举尚不存在（用字符串）；遗留 TextIn 整文档无路由记录，需合成 reasons，否则 P1 拒绝；页数缺失只告警。
