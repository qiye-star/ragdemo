# scripts/ — 一次性/运维/实验脚本（不是产品代码）

这里的东西不被业务包 import，也不进 CI。**产品能力一律在 `packages/` 里做成 `ragdemo` 子命令**；脚本只放备份运维与解析实验的测量/审计。

## 这个目录里有什么

| 脚本 | 做什么 | 费用 / 写库 |
|---|---|---|
| `backup.sh` | PG `pg_dump` + Chroma 目录打包，同一 STAMP 配对（`make backup`） | 免费；只读库，写 `BACKUP_DIR` |
| `restore.sh` | 按 PG dump 恢复并配对恢复 Chroma，须 `CONFIRM=yes`（`make restore FILE=...`） | 免费；**覆盖目标库** |
| `restore_drill.sh` | 现库备份 → 恢复到独立临时容器 → 逐表核对 → 销毁；`KEEP=1` 保留 | 免费；不碰现库 |
| `remap_eval_gold.sh` | `reparse-cached` 后生成评测 gold 块号重映射报告 + SQL | 免费；只读，SQL 须人工审阅再执行 |
| `parse_probe.py` | 解析分级路由前置探测：`hardware` / `scan` / `scan-db` / `census` | 免费；只读 |
| `parse_compare.py` | 本地路径 vs 库内 TextIn 缓存对比：`docs` / `research <目录>`，`--engine llm`（默认）/`deterministic` | `llm` 调 DeepSeek **要钱**；`--engine deterministic` 免费；只读库 |
| `audit_textin_pages.py` | 已解析文档逐页数字审计 | 免费；只读 |
| `audit_typeset.py <研报目录>` | 确定性排版审计（有不一致退出码 1） | 免费；读库 |
| `skip_audit.py` | 减页规则测量（积压候选页、D1-D5 候选） | 免费；只读 |
| `l0v2.py` | L0 第二版评估：`split/fixtures/multicol-sheet/headings/multicol-eval/diag-v1/run/gates` | 免费；只读库；留出集一次性锁定（见 `docs/10.12-plan/l0v2/holdout-run.lock`） |
| `l1_pilot.py` | 自托管 PaddleOCR 表格页试跑：`select/run/analyze`，一次一页严禁并行 | 免费（本地容器）；占约 12 GiB 内存 |
| `tokenizer_compare.py <标签>` | BM25 分词器对比（lindera vs chinese_compatible） | 免费；`RAGDEMO_DSN` 须指向**恢复出的临时库**，别在现库重建索引 |
| `make_pdf_fixture.py` | 生成 `tests/fixtures/pdf/table_sample.pdf`（需 reportlab） | 免费；写文件 |
| `record_engine_fixture.py paddle <url>` | 录制引擎响应为 `tests/fixtures/parse_engines/` 契约回放夹具 | 免费；需引擎容器在跑 |

## 怎么用

```bash
set -a; . ./.env; set +a          # 脚本与 ragdemo CLI 都不会自己读 .env
uv run --no-sync python scripts/parse_probe.py census
uv run --no-sync python scripts/parse_compare.py docs --engine deterministic
bash scripts/backup.sh            # 需宿主机装有 pg_dump，且 docker 能访问 ragdemo-chroma
bash scripts/restore_drill.sh     # 只依赖 docker
```

每个脚本头部 docstring 有完整用法，改用法时同步改 docstring。

## 硬规则

- 输出只落 `data/experiments/`（已被 git 忽略）；输出的 TSV/终端只含计数与文档键，**不得含正文、密钥、环境变量**。
- 不得绕开 egress-proxy 直连供应商；不得把密钥写进脚本。
- 默认只读；要写库/调付费接口的必须有显式开关并在 docstring 标明。写库类操作优先做成带 `--yes` 的 `ragdemo` 子命令而非脚本。
- 恢复类脚本会覆盖数据，先跑 `restore_drill.sh` 演练。

## 常见坑

- Windows Git Bash 下 docker 命令里的 `/tmp/...` 会被改写，`restore_drill.sh` 已 `export MSYS_NO_PATHCONV=1`；自写脚本同理。
- 用 `uv run --no-sync`，避免实验时触发依赖同步。
- `restore_drill.sh` 会在当前目录建 `.restore-drill-<pid>/`（已忽略），异常退出可能残留。
- `scripts/__pycache__` 是产物，无需关心。
