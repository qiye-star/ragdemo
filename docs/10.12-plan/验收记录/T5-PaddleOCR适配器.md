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

**已闭合（修复轮 2 核实）**：本轮 1 的临时做法（只做 `network disconnect`、网关临时用 18091）已被取代。控制器（协调者侧，不是我）随后重建了 egress-proxy（不再发布 8091、只在 infra_default）和 parse-gateway（落在 127.0.0.1:8091）。核实时 `docker ps` 输出见第 8.4 节。

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

### 7.6 真实页逐页耗时（一次一页，串行；CPU 16 核，宿主 Docker VM 共 15.48 GiB、被约 17 个容器共享；容器当时 HostConfig.Memory=0，即无上限）
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

约 37-50 秒/页（均值约 41.6 秒），与页面类型关系不明显。内存：空闲 2.45 GiB；约每 2 秒采样 `docker stats` 的峰值 12.57 GiB（15.48 GiB 是整个 Docker VM 而不是本容器的上限，容器当时没有内存上限）。采样粗粒度，真实峰值可能更高。只能一容器一页串行；批量跑 T8/T9 时需看住内存。layout_threshold=0.2 在这 10 页上只检查了 markup 违规，没有逐页对照原文，过召回（重复块）仍未排除。


## 8. 修复轮 2（2026-10-12）

### 8.1 归一化器重写（N1-N3）
每个探针先写成失败测试（tests/parse/tiered/test_markup_normalize.py，共 5 条新增，含一个 13 条语料的性质测试），再改 `markup_normalize.py`：
- N1：被去掉的包装标签换成占位，最后折成**单个空格**（紧邻标签/换行/首尾处丢弃）；绝不零宽。`<div>91</div><div>.2</div>` 不再变成 `91.2`；`<td><div>1</div><div>2</div></td>` 不再变成 `<td>12</td>`。
- N2：`<` 后不允许空白；标签名后必须是空白、`/` 或 `>`；名字里带 `-`/`:` 的不是标签。`staff < head office > 3 people`、`if a < div and b > c then 5`、`<div-x>`、`<html:div>` 全部原样不动。属性里出现无值属性（如 `<div and b>`）也视为不确定，原样不动。
- N3：属性按 name=value 严格切分（带引号/不带引号），只保留名字**恰好**是 rowspan/colspan（不区分大小写）且值为纯数字的属性（仅 td/th），其余全丢；`data-colspan`、`title="colspan=7"` 不再被误认。切分不干净、span 值非数字、span 出现在非 td/th 上 -> 整个标签原样保留（由 verify 拒绝）。
- 注释整体原样保留；输入里含 NUL 时整体不改写。
- 性质测试：语料上 `numbers_in` 与有序内容字符序列（isalnum，去标签语法后）改写前后完全一致（含重录夹具）。一个已知的不对称：未配对的 `</div>` 即使其开标签因不确定而保留，也会被删掉（删的只是标签语法，不涉及文字）。

### 8.2 内存上限（D1）与实测
首轮记录里"容器上限 15.48 GiB"是错的：`HostConfig.Memory=0`，15.48 GiB 是整个 Docker VM，被约 17 个容器共享。已更正（7.6），并给 parse-paddle 加 `mem_limit` 与相等的 `memswap_limit`（禁 swap），只重建 parse-paddle：

| 上限 | 页 | 结果 |
|---|---|---|
| 11g | 01214 第 15 页（表格重） | **被 OOM 杀**：docker events 出现 `oom`、`die 137`，restart 策略把引擎拉起（重载模型），后续请求 RemoteProtocolError；采样峰值 1.15 GiB 之后失联，该次未采到完整峰值 |
| 12g | 同上 | **仍被 OOM 杀**（`oom`、`die 137`），采样峰值 11.73 GiB 后被杀；请求 26.5 s 后 RemoteProtocolError |
| 13g | 01214 p15 | 成功，47.3 s，采样峰值 12.30 GiB，0 违规 |
| 13g | 2ffe8 p5 | 成功，55.9 s，峰值 12.56 GiB，0 违规 |
| 13g | 2a20d p7 | 成功，40.3 s，峰值 12.59 GiB，3 违规（同 7.2 的结构错误表格，非本轮回归） |

13g 下 `OOMKilled=false`、RestartCount=0。**现 compose 取 13g**（未去掉上限）；峰值 12.6 GiB 距上限只有约 0.4 GiB，距 VM 总量 15.48 GiB 余量约 2.9 GiB，并非宽裕，页面更重时仍可能被杀。（`docker inspect` 的 OOMKilled 字段在容器被 restart 策略拉起后会被清零，所以 11g/12g 的证据以 `docker events` 的 `oom` 事件为准。）
运行约束：**并发必须恰好为 1**（两个实例同时跑会 OOM）；吞吐约 41.6 s/页 ≈ 86 页/小时，1000 页 ≈ 11.5 小时。若要降内存，可考虑去掉可选的重模型（如 PP-FormulaNet_plus-L，698M 权重、公式识别）——仅记录，本轮没有做。

### 8.3 其它小项
- shim：`count_pages` 用 try/finally 关闭 `PdfDocument`；新增 `count_pages` 单测（1 页/2 页）与处理器状态码接线测试（真起一个 shim 线程：缺 Content-Length -> 400、负数 -> 400、超限 -> 413、不可读 PDF -> 400），均不依赖 docker。
- http_engine.py 文档字符串已改为与行为一致：引擎自报失败（200 + ok=false）-> EngineError；薄壳对请求的 400/413 与其它非 2xx 一样经 HttpClient 成为 AdapterError，再转成 EngineUnavailable。

### 8.4 现场状态核实（D2）
egress-proxy 与 parse-gateway 由控制器重建（不是我）。我核实时：

```
NAMES                   PORTS                                STATUS
ragdemo-parse-paddle    8000/tcp                             Up 2 minutes
ragdemo-parse-gateway   127.0.0.1:8091->8091/tcp             Up 12 minutes
ragdemo-egress          127.0.0.1:8080-8086->8080-8086/tcp   Up 12 minutes
```
`docker inspect ragdemo-egress` 的网络：只有 `infra_default`。网关经 8091 对外的健康检查（本轮用 `curl http://127.0.0.1:8091/v1/health`）返回 `{"engine": "paddle", "engine_version": "3.7.0", "device": "cpu"}`。

### 8.5 带入的已知缺口（不在本轮修）
没有从零构建的镜像（当前镜像叠在首轮镜像上）；paddlex 是构建期断言而非安装期固定；socat 镜像只按 tag（`alpine/socat:1.8.0.3`）固定、未按 digest；各组件/模型许可证未核查；`tests/parse/test_router.py` 需要数据库（当前 down）；Docker Desktop VM 网关上的 rpcbind/statd 可从 internal 网络访问（由复核者发现，未处理）；layout_threshold=0.2 的过召回未对照原文核查。


## 9. 修复轮 3：关掉不用的子模型（2026-10-12）

**结论先说：内存没有降到可用水平。峰值仍约 11.6 GiB，高于 9 GiB 的门槛；L1（自托管 PaddleOCR）在这台 15.5 GiB 的主机上不可行，除非加内存。**

### 9.1 做了什么
容器内核对了 `PPStructureV3.__init__` 的真实签名（有 `use_doc_orientation_classify / use_doc_unwarping / use_seal_recognition / use_chart_recognition / use_formula_recognition / use_table_recognition / use_region_detection / use_textline_orientation`）。`infra/parse-engines/paddle/server.py` 现在默认：方向分类、文档矫正、印章、图表、公式全关；表格识别、版面检测、文字检测/识别保持开启。每个开关可用环境变量覆盖：`PARSE_USE_DOC_ORIENTATION_CLASSIFY`、`PARSE_USE_DOC_UNWARPING`、`PARSE_USE_SEAL_RECOGNITION`、`PARSE_USE_CHART_RECOGNITION`、`PARSE_USE_FORMULA_RECOGNITION`、`PARSE_USE_TABLE_RECOGNITION`（取值 1/true/yes/on 为开，空串=默认）；另有可选的 `PARSE_USE_REGION_DETECTION`、`PARSE_USE_TEXTLINE_ORIENTATION`（不设=库默认，即开）。compose 只透传了公式/区域检测/文本行方向三个（其余可在 server.py 默认值处改）。
构建：Dockerfile 未改，预热层仍然加载全部 13 个模型（镜像里仍烘焙着公式、方向、矫正模型，只是运行时不加载），所以代码改动只触发 COPY 之后的层。仍沿用首轮的叠加办法（`--build-arg BASE_IMAGE=<上一版本地镜像>`），日志里没有任何下载。运行时 `Creating model` 从 13 个降到 10 个（去掉 PP-FormulaNet_plus-L、PP-LCNet_x1_0_doc_ori、UVDoc）。

### 9.2 实测（每页都换新容器，一次一页；cgroup 的 `memory.peak` 与 ~0.3 s 间隔的 `memory.current` 采样一致）

| 页 | 之前（全开，13g 上限） | 之后（5 个子模型关） |
|---|---|---|
| 01214 p15（表格重） | 41.8 s，峰值 12.95 GiB | 43.0 s，峰值 11.46 GiB |
| 2ffe8 p5（文字重） | 52.7 s，峰值 12.95 GiB（累计） | 61.9 s，峰值 11.53 GiB |
| 2a20d p7（表格重） | 40.6 s，峰值 12.95 GiB（累计） | 41.9 s，峰值 11.59 GiB |

之前三页是在同一容器里连跑，cgroup 峰值是累计值，所以三个相同；之后是每页新容器。空闲（模型载入后）约 2.15 GiB，所以单页处理本身要再多吃约 9.4 GiB。只省了约 1.4 GiB。
额外试了两个杠杆，都没用：再关掉区域检测与文本行方向（01214：11.24 GiB，基本没变，输出与默认完全一致）；`FLAGS_eager_delete_tensor_gb=0.0`（反而 12.05 GiB）。页内峰值来自推理本身，不是子模型数量；没有继续猜别的参数。没有 OOM（13g 上限下 `OOMKilled=false`、RestartCount=0）。

### 9.3 输出对比（之前 vs 之后，原始 markdown，去标签后）
没有哪页逐字相同（空白/全半角括号有差异）。数字与内容字符：
- 2ffe8 p5：数字多重集合与内容字符序列**完全相同**。
- 01214 p15：数字不同。之前 `17,053,14,859.87`（漏了一个 9），之后 `17,053,149,859.87`；对照 pypdf 文字层，**之后是对的**。另外之后**丢了一个文字标签「非流动负债：」**（无数字）；还有 `实收资本（或股本）` 全角括号变成半角、`2023 年` 空格变化。
- 2a20d p7：之前 `4,388,214,499.44` 出现两次（重复块），之后一次；文字层里只有一次，**之后是对的**。归一化后的 markup 违规数从 3 降到 0（之前的结构错误表格没了）。
所以关子模型后没有丢数字，两处数字更准确，但在 01214 上丢了一个纯文字标签，这是内容字符的损失，记在这里，没有掩盖。三页的样本太小，不能据此说关子模型总体更准。5 条在线契约测试：`5 passed, 5 deselected in 58.09s`（最终配置）。

### 9.4 内存上限与运行数字
按"峰值 + 25% 余量"要求，需要约 14.5g（11.59 x 1.25），这在 15.48 GiB 的 VM 上等于吃掉整台机器，做不到。所以**没有**按规则下调，`mem_limit`/`memswap_limit` 保持 13g（余量只有约 12%，比要求的 25% 少）；同机还有 ParadeDB 等其它容器，这个上限实际上无法安全使用。
运行数字：并发必须恰好为 1；约 41-62 s/页（本轮三页均值 49 s，早先 10 页均值 41.6 s）≈ 60-86 页/小时；1000 页约 12-17 小时；单页峰值 11.5-11.6 GiB，空闲 2.15 GiB。
若仍要在本机用 L1：需要更多内存（建议 VM 至少 20 GiB，给 parse-paddle 14.5g 上限），或改用 GPU/更小的版面与表格模型，或把 L1 放到别的机器上。这些都不是本轮能做的。
