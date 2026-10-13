# W7 TextIn 记账单价修正

价格位置：代码无硬编码 0.04。单价来自环境变量 TEXTIN_COST_PER_PAGE_CNY（本地 .env 曾设 0.04，文档 infra/backfill.md、phase-1/3/6 验收记录也写 0.04）；config.py 原默认 None。
读取方：assets_docs.record_parse_snapshots（cost_cents = billed_pages x 单价 x 100，numeric(10,4)，无取整）、parse/router.check_monthly_budget（C 档，对比 core.parse_tier_policy.monthly_cap_cny）、quality/budget.month_to_date（汇总 cost_cents，对 RAGDEMO_MONTHLY_BUDGET_CNY）。PageBudget 只按页数，不受影响。
改动：config.py 新增 DEFAULT_TEXTIN_COST_PER_PAGE_CNY = Decimal("0.1356")（注释含实测依据、待发票复核），为默认值，env 可覆盖；.env.example、infra/backfill.md 更新；本地 .env 该行改为 0.1356（未打印）。
预算影响：月度总预算 5000 元（80% 告警=4000 元，约 29.5k 页，旧价下需 10 万页）；C 档策略 monthly_cap_cny 占位 500 元（约 3688 页，旧价 12500 页）；单次 run PageBudget 500 页不变，但单 run 最大记账由 20 元升至 67.8 元。历史行未改写。
测试编辑：tests/test_config.py 两处断言默认值（原"默认 None"改为默认 0.1356；原"缺失抛错"改为显式 None 才抛错）。新增：snapshot_linkage 10 页 = 135.6 分；test_budget 3 万页 = 4068 元进入 WARN。其余旧测试显式传价，未动。
重述历史（未执行）：UPDATE core.provider_snapshot SET cost_cents = (params->>'billed_pages')::numeric * 13.56 WHERE provider='textin' AND params ? 'billed_pages';
全量测试 tests/parse ingest quality ops test_config：1104 过，4 败均与本改动无关（test_figures 3 个 FileNotFound，来自并行任务；test_schedules 分区日期 2026-10-13 不在分区内）。
