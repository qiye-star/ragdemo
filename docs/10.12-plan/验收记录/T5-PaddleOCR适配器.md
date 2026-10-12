# T5 PaddleOCR 适配器 验收记录

日期：2026-10-12。状态见第 7 节（修复轮 1）；第 1-6 节为首轮记录，其中 egress 反代 8091 的做法已被第 7 节取代。

## 1. 交付

- 薄壳 `infra/parse-engines/common/shim.py`、引擎 `infra/parse-engines/paddle/{Dockerfile,server.py}`。
- HTTP 适配器 `ragdemo/parse/tiered/http_engine.py`（`HttpPageEngine`、`engine_from_url`），`providers.yaml` 增 `parse_paddle/mineru/docling`，`pytest` 标记 `live_engine`。
- compose：`parse-paddle` 服务、`parse-internal`（internal）网络、egress-proxy 加入该网络并反代 8091；policy 白名单加 `parse-paddle`（无凭据）。

## 2. 构建与版本

paddlepaddle 3.3.1、paddleocr[doc-parser] 3.7.0（paddlex 随依赖解析）。镜像大小 2.17 GB（`docker images`）。
来源：pip 清华镜像（T1 记录所列）；模型权重由 paddlex 在**构建期**从 BOS（`PADDLE_PDX_MODEL_SOURCE=BOS`）下载进镜像层；运行日志显示全部模型 "Model files already exist. Using cached files"，运行期无下载。

构建中遇到并修复的三个问题：
1. 缺 `libgomp1`（`ImportError: libgomp.so.1`）：apt 加装。
2. Paddle 3.x CPU 默认 oneDNN(PIR) 执行器，版面检测报 `NotImplementedError: ConvertPirAttribute2RuntimeAttribute`：设 `PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0`（Dockerfile 与 compose 均设）。
3. 默认 `layout_threshold=0.5` 把无框线稀疏表格（得分 0.49）判成只覆盖左列的文字块，数字列落在任何块之外而丢失（OCR 本身识别出了全部数字：`91.2 60.1 19.3 12.7`）。`layout_threshold=0.2` 后得到含全部数字的 HTML 表格。已在 `server.py` 以 `PARSE_LAYOUT_THRESHOLD`（默认 0.2）传入。注意：该阈值只在合成夹具上验证；真实研报页是否出现过召回（重复块）未测，由 verify.py 数字闭合校验兜底，T8/T9 基准阶段应复核。

## 3. 隔离验证（真实运行）

- `docker network inspect infra_parse-internal` -> `Internal: true`。
- `docker compose exec parse-paddle python -c "socket.create_connection(('pypi.tuna.tsinghua.edu.cn', 443), timeout=5)"` -> `socket.gaierror: [Errno -3] Temporary failure in name resolution`（失败，符合预期）。
- 经 8091 的健康检查：`{"engine": "paddle", "engine_version": "3.7.0", "device": "cpu"}`；egress 日志显示 `egress host=parse-paddle path=/v1/health status=200`。
- egress-proxy 的 policy 是烤进镜像的，改 policy 后必须 `up --build egress-proxy`（否则返回 `egress blocked: host not in allowlist`）。

## 4. 冒烟与耗时（真实运行，CPU，16 核）

| 项 | 值 |
|---|---|
| 夹具页（table_sample.pdf 第 1 页，默认阈值 0.5，经 8091 的 `/v1/parse-page`） | elapsed_ms = 29869（含该容器首个请求的预热） |
| 同页一次性 `docker run`（含模型加载） | 约 44 秒总耗时 |
| 容器常驻内存 | 2.17-2.41 GiB（`docker stats`） |
| 镜像 | 2.17 GB |

同一容器内再并开第二个流水线实例时第二页被 OOM 杀掉（`Killed`），说明单容器单实例串行是必要的（与薄壳设计一致）。

## 5. 未完成项与原因

1. **Docker Desktop 在最后阶段崩溃**（后台自动更新包已下载并 "preparing install" 后引擎停止，重启后每次启动都弹错误对话框，引擎起不来；强制重启三次无效，需人在 GUI 上处理）。后果：
   - 含 `layout_threshold=0.2` 与最终 Dockerfile 顺序的**镜像未能重建**，`parse-paddle` 现跑的是未含阈值修复的镜像（compose 已含 oneDNN 开关）。
   - **夹具 `paddle_table_sample.json` 是手工拼装的**（文件内 `_provenance` 字段已写明）：health 与默认阈值响应取自真实录制；其 markdown 逐字取自同一流水线在 `layout_threshold=0.2` 下的真实运行输出，`elapsed_ms` 沿用默认阈值那次录制的值。重新起容器后须用 `scripts/record_engine_fixture.py` 重录并去掉 `_provenance`。
   - 在线契约 `TestLivePaddleEngine`（5 条）**未能通过**：Docker 不可用，健康检查失败。回放 5 条通过。
2. Step 9 的"5 份真实研报各 2 页"逐页耗时**未做**（同一原因），现只有上面夹具页一个样本。
3. 用户同意：brief 要求构建前取得用户同意；本次由上级任务明确授权执行构建，来源即上述 pip 镜像与 BOS 权重源。

## 6. 恢复步骤

Docker Desktop 恢复后：
`docker compose -f infra/docker-compose.yml --env-file .env up -d --build parse-paddle egress-proxy`
`uv run --no-sync python scripts/record_engine_fixture.py paddle http://127.0.0.1:8091`（重录夹具）
`PARSE_PADDLE_BASE_URL=http://127.0.0.1:8091 uv run --no-sync pytest tests/contracts/test_paddle_page_engine.py -m live_engine -q`
并补做 Step 9 的真实研报逐页耗时。
