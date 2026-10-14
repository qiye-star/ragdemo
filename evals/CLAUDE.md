# evals/ — 评测数据的预留目录（目前只有 .gitkeep）

## 这个目录里有什么

只有 `.gitkeep`。**评测集本身目前不在这里**：
- 评测用例与标注存在数据库里（`ragdemo eval ...` 子命令录入/管理，标注入口仍是 CLI，见 ADR-0010 后果 6）。
- 评测基线与冻结快照在 `docs/10.12-plan/验收记录/`（`*-freeze.json`、`新配置基线B1.md`/`B2.md` 等）。
- 评测代码在 `packages/ragdemo/src/ragdemo/evals/`，测试在 `tests/evals/`。
- `eval` 子命令的帮助文案里把 `evals/retrieval/...` 当作 CSV/快照输出的示例路径，说明约定落点是这里。

## 怎么用

```bash
set -a; . ./.env; set +a
uv run ragdemo eval --help
make eval-retrieval        # = ragdemo eval run --suite retrieval --gate；回退超 0.02 容差非零退出
```

## 硬规则

- 改 prompt/切块/检索参数/模型必须跑三套评测并把数字贴进 PR（根 CLAUDE.md §1.4）。
- 若要把标注 CSV/冻结快照放进这里：先确认不含受版权保护的研报正文、不含密钥；评测基线要成对保存（freeze.json + 报告）。
- 评测命令会调嵌入/重排 API（SiliconFlow，**按量计费**）；跑前确认 `.env` 与出网代理，优先复用嵌入缓存。

## 常见坑

- 看到空目录别以为评测集不存在；先查库与 `docs/10.12-plan/验收记录/`。
- `ADR-0010` 写于 W9.3 未开始时，其中「evals/ 只有 .gitkeep」是当时事实，现在仍恰好成立但评测集已另有落点。
