# 解析管线 + 权限隔离 只读诊断界面

> **来源**：用户请求"不用做 agent，我现在只需要看到解析管线和权限隔离，
> 基于现在的项目帮我做成前端可视化页面"。
> **依据**：`docs/adr/0010-internal-diagnostic-web-ui-in-p1.md`（已接受）——
> 技术栈已锁死：FastAPI 纯 GET、`web/` 静态资源、不引 Node 构建链、
> 只绑回环、强制 `as_of`、只查 `asof` 视图、`owner_user IS NOT NULL`
> 的块硬编码不可见。
> **写法**：与 `2026-09-22-data-foundation.md` 同一原则——A 做完并通过
> 验收，才开始 B。每条记录都注明验证方式，不是形容词。

## 为什么做这件事

文档解析管线（W2.x）与权限隔离（RLS + `asof` 视图）在代码层面都已完成并
有单测覆盖，但没有任何人能用眼睛确认它们在真实语料上做对了。ADR-0010
把这个问题说得最准：判断"切块对不对"需要同时看章节树 × 页码 × 表格
完整性 × 相邻块重叠——**那是二维的，终端一次只能打印一维**。权限隔离
同理：`app_diag` 连接 + GUC 切换 + 四分支策略，只有跑一遍才知道它真的
拦住了。

## 实施进度

- [x] **Task 1 · `app_diag` 只读角色** —— `7c4c46c`。新建专用角色（不用
  `app_read`：实测它对 `evals.*` 有 10 条 INSERT/UPDATE 授权，用它连接
  "后端物理上无法写入"这条约束就不成立）。测试证明这不是靠自觉：7 张
  登记时点表逐一断言 `InsufficientPrivilege`、遍历权限表断言只有
  SELECT、`SET ROLE` 退路本身也拒绝基表。
- [x] **前置 · fastapi/uvicorn 依赖** —— `8ca6a6f`。`uv add` 后
  starlette 版本未变（已在 lock 里，dagster-webserver 的传递依赖），
  `dagster_webserver` 仍正常导入——不必退回手写 Starlette 路由的方案。
- [x] **Task 2 · 配置/as_of 校验/错误信封** —— `bfa1030`。两种连接模式
  （生产：专用 `RAGDEMO_API_DSN`；开发：`RAGDEMO_DSN` + `SET ROLE`）。
- [x] **Task 3 · app 骨架 + GET /api/meta + 契约测试** —— `9b5be9e`。
  契约测试遍历 openapi schema 断言"每条路由都要 as_of""没有写方法"。
  **发现并修复真实 bug**：`as_of` 缺失时 `get_conn` 曾经仍会执行（两者
  是路由函数的平级依赖，FastAPI 各自独立求值）——503 会抢在 422 之前
  返回。修法是让 `get_conn` 把 `as_of` 声明成自己的参数，强制按依赖图
  顺序求值。
- [x] **Task 4 · 文档/块/树/版面接口** —— `3ecc5f8`。`build_tree` 纯
  函数，TDD 抓到一个真 bug：两节点互为父子的"纯环"完全连不上任何
  root，从 root 出发的 DFS 永远走不到，必须额外做一遍独立于 root 遍历
  的环检测（三色标记法扫父指针图本身）。
- [x] **Task 5 · 质量门禁/分档预算接口** —— `fd021b5`。全部复用
  `parse/router.py` 已测的谓词/算法当 oracle，不重新实现判定逻辑。
- [x] **Task 6 · 权限隔离演示种子** —— `ee7b678`。不用
  `DocumentWriter.write_document`：它只接受 `owner_user`，不接受
  `owner_tenant`。种子的 `publish_at=known_at=2020-01-01` 是功能性的
  ——落在 Dagster 日分区窗口（2022-01-01 起）之外，不会把
  `vector_coverage_rate` 拉低触发 blocking check。
- [x] **Task 7 · 隔离探针矩阵 + 目录佐证** —— `489b199`。六个独立顶层
  事务；已用真实 `app_diag` 登录用户 + 真实 RLS 策略跑通全部场景。
- [x] **Task 8 · 前端外壳 + 三态渲染** —— `377bee0`。真实浏览器验证时
  发现两处 Task 8 自身的 bug 并修复：① `view.render()` 抛出的错误此前
  被 re-throw 到全局而不是渲染 errorCard；② 切换顶部标签页会静默丢弃
  已选的 as_of。
- [x] **Task 9 · 文档列表视图** —— `291d754`。
- [x] **Task 10 · 版面还原视图（bbox 叠加）** —— `2977da2`。写代码时
  自己踩了自己的 CSP：四处内联 `style="..."` 属性被 `style-src 'self'`
  当场拦下，全部改成具名 CSS class。
- [x] **Task 11 · 块树视图** —— `2c90d9b`。按 `section_path` 分组，
  长度条改用小 SVG 画（元素属性不受 CSP 的 style-src 限制），
  `sanitizeTable` 用合成的 `<script>`/`onclick` payload 验证过。
- [x] **Task 12 · 质量门禁看板** —— `da56992`。**发现并修复一个更严重
  的接口层 bug**：`MetricPoint` 一直没有 `metric` 字段——dashboard 把
  11 个 metric 汇总在同一个列表里返回，前端按 metric 分组时所有点全部
  落进同一个 `undefined` 桶，11 张卡片只有最后一张显示数据且标题是空
  的。这个 bug 只有连起真实浏览器、真实数据、多 metric 同时存在时才会
  显形——纯 API 层单测当时没有覆盖到"多个 metric 混在一个 dashboard
  响应里"这个场景，补了字段和两条回归测试。
- [x] **Task 13 · 分档与预算视图** —— `c9b5339`。
- [x] **Task 14 · 权限隔离面板** —— `09c11fb`。六个视图至此全部接入。
  端到端用 `ragdemo db seed-isolation-demo` 播种真实私有数据，确认
  四个身份分支的可见行数精确符合预期、8 项检查全部通过、两个错误
  分支精确报出 22023/42501、页面全文搜不到任何合成标记。
- [x] **Task 15 · 收口** —— `cd11770`、`27cc27e`。
  - `ragdemo serve`（非回环地址需要 `RAGDEMO_API_ALLOW_LAN`）、
    `ragdemo db grant-api-read`（此前只在计划里写了但没有实现，这次
    补上——已验证创建的登录用户真的能连接、真的是 `app_diag` 成员）。
  - Makefile 新增 `serve`/`seed-isolation-demo`/`clear-isolation-demo`；
    `.env.example` 补齐四段配置说明。
  - `tests/api/test_sql_guards.py`：六条源码扫描守卫。过程中修了测试
    自身的一处误判（`_code_only` 最初没剥三引号文档字符串，isolation.py
    的 docstring 里举例引用的字面量被误判成第二处执行代码）。
  - 文档同步：`docs/01-architecture.md` §4 补充"P1 现状：`api` 以宿主
    进程形态运行，不是拓扑图里的容器"。

## 已知不做（刻意）

- **PDF 底图**：版面还原是空白页画框，不渲染真实 PDF——零新依赖，
  不需要"吐原始文件字节"的通道，不碰 `can_show_raw`（用户已确认）。
- **compose 里的 `api` 服务**：P1 以宿主进程形态运行（`ragdemo serve`），
  容器化留到 P4。
- **前端自动化测试**：没有 Node 就没有 vitest/playwright，验证靠真实
  浏览器手工验收（本计划每个 Task 都记录了验证方式）+ 后端的完整
  pytest 覆盖。

## 验证

```bash
make up && uv run ragdemo db migrate
uv run ragdemo db grant-api-read --user ragdemo_api   # 生产形态；开发可跳过，用 RAGDEMO_API_ALLOW_PRIVILEGED_DSN=1
uv run ragdemo db seed-isolation-demo --yes
uv run ragdemo db check                                # 演示数据不得把它搞红
make lint && make typecheck && uv run pytest tests/api tests/seed -v
make serve
```

然后浏览器打开 `http://127.0.0.1:8088/`，六个视图 × 4 种 `as_of`
（正常 / 早于全部文档 / 未来 / 从 URL 删掉）逐格看一遍。
