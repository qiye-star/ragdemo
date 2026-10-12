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

## 7. 修复轮 1（2026-10-12）

### 7.1 隔离重做（I1）
首轮把 egress-proxy 加进了 parse-internal，parse-paddle 因此能解析 `egress-proxy` 并用它的 8080 正向代理与 8081-8086 带凭据反代端口，"无出网"只对直连 socket 成立。现改为：egress-proxy **只在 default 网络**；新增无凭据网关 `parse-gateway`（`alpine/socat:1.8.0.3`，无 env_file/环境变量/卷），同时在 default 与 parse-internal，仅 `TCP-LISTEN:8091 -> parse-paddle:8000`，发布端口仅回环；policy.yaml 删除 parse-paddle 条目（不再经代理）。
实机检查（在 parse-paddle 内执行；容器无 curl，用 python 等价）：

```
DNS egress-proxy -> FAIL: gaierror [Errno -3] Temporary failure in name resolution
DNS ragdemo-egress -> FAIL: gaierror [Errno -3] Temporary failure in name resolution
DNS example.com -> FAIL: gaierror [Errno -3] Temporary failure in name resolution
proxy 8080 -> https://api.deepseek.com -> FAIL: URLError <urlopen error [Errno -3] Temporary failure in name resolution>
GET http://egress-proxy:8086/ -> FAIL: URLError <urlopen error [Errno -3] Temporary failure in name resolution>
direct 1.1.1.1:443 -> FAIL: OSError [Errno 101] Network is unreachable
host.docker.internal:{8080,8081,8086,18091,8091} -> FAIL: gaierror (not resolvable)
parse-gateway:{8080,8081,8086,18091} -> FAIL: ConnectionRefusedError [Errno 111]
parse-gateway:8091 -> connected   (设计内的唯一通路：socat 回到 parse-paddle 自己)
```
宿主回环：`curl http://127.0.0.1:18091/v1/health` -> `{"engine": "paddle", "engine_version": "3.7.0", "device": "cpu"}`（健康端点是 `/v1/health`）。网关容器环境变量中含 key/token/secret 的条数：0。`infra_parse-internal` 成员：ragdemo-parse-paddle、ragdemo-parse-gateway。

**未闭合**：按硬规则未重启 egress-proxy，只用 `docker network disconnect infra_parse-internal ragdemo-egress` 把它从内网摘下（实时生效，compose 文件已是目标状态）。它仍占着宿主 127.0.0.1:8091（旧反代端口，旧 8091 mode 仍在监听），所以网关临时用 `PARSE_GATEWAY_PORT=18091`（compose 里 `${PARSE_GATEWAY_PORT:-8091}`）。**需要协调者批准后执行** `docker compose up -d --build egress-proxy`（重建会去掉 8091 端口与 mode，并把 policy 重新烤进镜像），再 `up -d parse-gateway`（不带 PARSE_GATEWAY_PORT）把网关落到 8091；providers.yaml 的 base_url 本来就是 `http://127.0.0.1:8091`，无需改。

### 7.2 标记归一化（I2）
新增 `parse/tiered/markup_normalize.py::normalize_markup`（确定性、幂等），`HttpPageEngine` 输出统一经过它：去掉 div/html/body/head 包装（保留内部内容）、table/tr/td/th 只保留 rowspan/colspan、标签小写，不改任何文字。测试用重录夹具：原样 `markup_violations` 非空，归一化后为空，去标签后数字多重集合与内容字符序列均不变。注意：数字比较必须按去标签文本做（属性 `border="1"` 里的 1 会被 numbers_in 数到）。
实测局限：10 页真实页中 1 页（2a20d 第 7 页，表格密集）归一化后仍有 3 条违规（`</table>` 不配对、表格内单元格外有文字、`tbody` 未闭合）——引擎吐出的是结构错误的表格，归一化器不修结构，由 verify 拒绝并降级到下一档。

### 7.3 薄壳加固（M5）、其它
Content-Length 缺失/非正 -> 400，超 32 MB -> 413（读体之前校验）；`Handler.timeout=120s`；PDF 必须恰好 1 页（pypdfium2 计页，不可读 -> 400）。纯函数有单测（tests/parse/tiered/test_shim.py，不依赖 docker）。providers.yaml parse_paddle `max_attempts: 1`（M6）。EngineError 文档字符串与行为对齐（M7）。

### 7.4 版本与构建（M4）
容器内实测：paddlepaddle 3.3.1、paddleocr 3.7.0、paddlex 3.7.2、pypdfium2 5.14.0。Dockerfile：基础镜像按 digest 固定 `python:3.11-slim@sha256:e88e9763...65f6ce`（`docker buildx imagetools inspect` 解析的 OCI index digest；该 digest 下 apt/pip 层命中缓存）；paddlex 由 paddleocr 传递依赖解析，**没有**写进 pip 命令（会使 1.7GB 的 pip 层与模型层缓存失效），而是在预热层之后加版本断言（三者漂移则构建失败）。这是断言式固定，不是安装期固定——已知缺口。

**构建方式的诚实说明**：首轮镜像的预热层在 COPY 之后（旧顺序），现 Dockerfile 的顺序（预热在 COPY 之前）使预热层缓存不命中，从头构建会重新下载约 1.8 GB 模型。按"不得重下多 GB"的规则我中止了那次构建，改为 `--build-arg BASE_IMAGE=infra-parse-paddle:t5-base-v1`（首轮镜像的本地 tag）在其上叠加新代码：apt/pip 全部 already satisfied，预热从镜像已有模型读取，零下载（日志核对过）。因此**当前运行的镜像不是从零构建的**；新 Dockerfile 顺序、digest 固定与版本断言的从零构建未端到端验证。

烘焙进镜像的模型目录（`/root/.paddlex/official_models`，共 1.8G）：PP-DocBlockLayout 124M、PP-DocLayout_plus-L 125M、PP-FormulaNet_plus-L 698M、PP-LCNet_x1_0_doc_ori 6.6M、PP-LCNet_x1_0_table_cls 6.6M、PP-LCNet_x1_0_textline_ori 6.6M、PP-OCRv5_server_det 85M、PP-OCRv5_server_rec 81M、RT-DETR-L_wired_table_cell_det 124M、RT-DETR-L_wireless_table_cell_det 124M、SLANeXt_wired 349M、SLANet_plus 7.7M、UVDoc 31M。

**许可证未核查**：以上每个模型权重的许可证，以及 paddlepaddle/paddleocr/paddlex 及其传递依赖（含 pypdfium2）的许可证均未核查，不得据此认为可商用或可再分发。

### 7.5 夹具重录与在线契约（M3）
`scripts/record_engine_fixture.py paddle http://127.0.0.1:18091` 一次真实响应同时写入 health、markdown、elapsed_ms（31519 ms），`_provenance` 已更新（不再是手工拼装）。在线契约：`PARSE_PADDLE_BASE_URL=http://127.0.0.1:18091 pytest tests/contracts/test_paddle_page_engine.py -m live_engine` -> `5 passed, 5 deselected in 58.35s`。

### 7.6 真实页逐页耗时（一次一页，串行；CPU 16 核，容器上限 15.48 GiB）
data/blob/raw 下 307 份 PDF 全在 `raw/cninfo/`（公告类，没有研报），固定种子抽 5 份 8-60 页的文件；每份按 pypdf 文字层选文字重页（数字占比<8% 中字符数最多）与表格重页（字符>=300 中数字占比最高）。

| 文件(前缀) | 总页 | 页 | 类型 | 秒 | md 字符 | 归一化后违规 |
|---|---|---|---|---|---|---|
| c246a | 11 | 4 | 文字 | 41.7 | 824 | 0 |
| c246a | 11 | 8 | 表格 | 37.3 | 701 | 0 |
| 60642 | 14 | 11 | 文字 | 44.7 | 999 | 0 |
| 60642 | 14 | 8 | 表格 | 39.5 | 770 | 0 |
| 2a20d | 12 | 1 | 文字 | 38.6 | 1091 | 0 |
| 2a20d | 12 | 7 | 表格 | 37.5 | 2318 | 3 |
| 2ffe8 | 13 | 5 | 文字 | 50.0 | 1605 | 0 |
| 2ffe8 | 13 | 9 | 表格 | 38.8 | 1725 | 0 |
| 01214 | 16 | 1 | 文字 | 42.4 | 800 | 0 |
| 01214 | 16 | 15 | 表格 | 45.3 | 2757 | 0 |

约 37-50 秒/页（均值约 41.6 秒），与页面类型关系不明显。内存：空闲 2.45 GiB；约每 2 秒采样 `docker stats` 的峰值 12.57 GiB（容器上限 15.48 GiB，余量约 3 GiB，同机其它容器也在用内存）。采样粗粒度，真实峰值可能更高。只能一容器一页串行；批量跑 T8/T9 时需看住内存。layout_threshold=0.2 在这 10 页上只检查了 markup 违规，没有逐页对照原文，过召回（重复块）仍未排除。
