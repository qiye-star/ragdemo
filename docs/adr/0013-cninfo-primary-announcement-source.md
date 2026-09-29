# ADR-0013 · 公告全文主源改为巨潮资讯网直连，修订 ADR-0012 的公告分工

- 状态：已接受
- 日期：2026-09-28
- 决策者：数据底座负责人
- 影响：修订 ADR-0012 `Capability.ANNOUNCEMENTS` 的分工表；不改 ADR-0005
  （`AnnouncementProvider` 抽象仍然成立，只是本决定的实现不走这个 Protocol，
  见下文"为什么不实现 AnnouncementProvider"）

## 背景

ADR-0012 把公告全文的主源定为 Wind `get_company_announcements`，iFind
`search_notice` 做降级。2026-09-28 下午复测（`docs/9.28-plan/验收记录/
phase-2.md`）发现两家都给不了公告全文：

- Wind `financial_docs`：返回的正文是按查询相关度截取的摘录，**摘录长度随
  问法变化**（同一份半年报测出 25412 字与 22098 字两个不同结果），没有日期
  参数，且会混入同名的其他公司。
- iFind `search_notice`：只返回标题、日期、片段，没有公告 ID、没有原文
  链接、没有页码。

公告全文 + 页码 bbox 溯源是这个系统的核心承诺（CLAUDE.md §0"每个数字必须
可溯源"），两家 MCP 都进不了要求全文的文档管线。

## 决定

**公告全文的主源改为直连巨潮资讯网（cninfo.com.cn）。**

巨潮资讯网是沪深交易所指定的官方信息披露平台，不是需要签约谈判条款的
商业数据供应商——这一点与 ADR-0005 评估过的候选（庖丁 PDFlux、通联、
iFinD）不同类，而与已经采用的 EDGAR（`docs/adr/0005` 决定内容 3、
`014_edgar_source_registry.sql` 的登记理由）是同一类：法定的公开信息
披露记录，没有"供应商合同"这回事可谈。`04-ingestion.md` 已经把 EDGAR
当作合法的免费 REST 源，这个判断在此同样适用。

它调用的 `hisAnnouncement/query` / `information/topSearch/query` 不是
巨潮官方发布的公开 API 文档，是其网站前端自己在用的接口（2026-09-28
实测确认可用，见"实测记录"）。这与"自建爬虫"的边界判断——CLAUDE.md §4
"不自建爬虫（公告走供应商接口）"——需要显式说明：这里不是对无结构网页
做内容抓取和解析，是调用该官方平台自己的结构化查询接口取公开记录，
性质上与调用 EDGAR 的 `data.sec.gov` JSON 接口相同，因此判定为不落在
"自建爬虫"这一条要防的范围内。

### 实测记录（2026-09-28，只读，未写库）

| 调用 | 结果 |
|---|---|
| `POST information/topSearch/query`，`keyWord=688041` | 200，返回 `orgId=9900048365` |
| `POST hisAnnouncement/query`，`stock=688041,9900048365`，`seDate=2025-01-01~2025-12-31` | 200，`totalAnnouncement=147`，`totalpages=29`（`pageSize=5` 硬限） |
| `GET static.cninfo.com.cn/{adjunctUrl}` | 200，`Content-Type: application/pdf`，无需鉴权 |

**关键发现：`announcementTime` 不是秒级精度。** 实测值
`1767024000000`（毫秒 epoch）解码为北京时间 `2025-12-30 00:00:00.000000`
——整点零时，是当日占位，不是真实发布时刻。这条来源此前在
`core.source_registry` 里登记的是手工 INSERT 的 `time_precision='second'`，
从未用真实接口验证过；`020_cninfo_source_registry.sql` 把它改成 `'day'`，
让 `DocumentWriter._known_at()` 走既有的"当日 23:59:59.999999（北京时间）
保守上界"分支，不是重新发明一套时点逻辑。

## 为什么不实现 `AnnouncementProvider`

`AnnouncementProvider.normalize(self, raw: RawResponse) -> NormalizedDocument`
签名只接受一个已经取回的 `RawResponse`。但巨潮这个来源天然是两次独立
HTTP 调用——先查询列表（`hisAnnouncement/query`，拿到 `adjunctUrl`），
再下载 PDF 字节（`static.cninfo.com.cn`，另一个主机）——且两次调用都要
各自留一条 `provider_snapshot`（CLAUDE.md §1.5）。把第二次调用塞进
`normalize()` 内部意味着这个方法需要持有 `BlobStore` 与 `psycopg.Connection`，
这两样都不在 Protocol 签名里，只能通过适配器自己的构造函数偷偷携带，
掩盖了"这个方法其实会发起真实网络调用与数据库写入"这件事。

EDGAR 在 P1 遇到过完全相同的结构（`fetch()` 拿列表，`fetch_full_text()`
另开一个主机拿全文），当时的解法是不实现 `AnnouncementProvider`，改用
一对专用函数（`ingest/edgar_docs.py::list_filings` / `ingest_edgar_filings`）
直接构造 `NormalizedDocument`。本决定照抄这个已经验证过的模式
（`ingest/cninfo_docs.py`），不是新发明一套设计。

`AnnouncementProvider` 协议与它的 6 条契约测试（`tests/contracts/
announcement_contract.py`）本身不受影响，继续服务于"供应商在一次调用里
就能给出结构化正文"的场景（ADR-0005 的初衷）——巨潮不属于这一类。

## 对 ADR-0012 分工表的修订（2026-09-28 公告双源接入计划再次修订）

首次修订（本 ADR 原文，同日上午）把 `Capability.ANNOUNCEMENTS` 从"Wind 主 + iFind
降级"改为单层直写：Wind/iFind 两家都给不出可用的全文，"降级到它们"没有实际意义，
源不可用时整批失败（人工介入），不悄悄写入不完整数据。

同日下午（负责人决定 D1）：**这个判断只对"全文"成立，对"目录"不成立。** 目录
（标题、日期、链接）Wind/iFind 是能给的，只是不够精确、不保证列全、偶尔混入他司
（见公告双源接入计划"实测记录"）。当天没有全文来源时，一条不够精确的目录行也
好过完全没有记录——公告目录（`core.announcement_index`，新表）与公告全文
（`core.document`）分开建模后，这个折中不再需要"要么完美要么没有"：

```python
Capability.ANNOUNCEMENTS_CN: (
    LayerSpec("cninfo", service="cninfo-http", source="cninfo", degraded=False),
    LayerSpec("wind-docs", service="wind-financial-docs", source="wind",
              degraded=True, full_text=False),
    LayerSpec("ifind-notice", service="ifind-news", source="ifind",
              degraded=True, full_text=False),
),
```

`LayerSpec.full_text=False` 是这次修订加的字段：降级层只准写目录行、绝不写
`core.document`，全文记入 `core.ingest_backlog` 等巨潮恢复后补——这是
`Capability.FINANCIALS`"不跨源写入现行值"那条原则在"目录 vs 全文"两个粒度上的
延伸，不是推翻它。

同一次修订也把公告从单一 `Capability` 拆成 `ANNOUNCEMENTS_CN` /
`_HK` / `_US`（公告双源接入计划"决策 D5"）：巨潮只覆盖 A 股，港股走港交所
披露易、美股与已在 SEC 报送的 ADR 走 EDGAR，三条渠道的时间精度、鉴权方式、
是否需要降级都不同，合并成一个 Capability 会强迫写一套通用接口掩盖这些差异。
`ANNOUNCEMENTS_HK` / `_US` 都是单层——它们本身就是官方渠道，不需要也不该
降级到给不出全文的来源。

## 后果

1. **公告全文管线解锁**，阶段 3（文档数据）可以按真实质量推进，不再卡在
   "两家 MCP 都给不了全文"这个阻塞项。
2. **代价：多一套专用管线**，不复用 `doc_normalized`/`doc_prepared`/
   `doc_blocks_loaded` 三个通用资产（与 EDGAR 同样的代价，ADR-0005 决定
   内容 3 已经接受过一次）。
3. **反爬 / 频控没有官方保证**。实测时的请求间隔、503/429 退避策略是
   保守估计，不是巨潮承诺的 SLA——`adapters/cninfo.py` 的限流参数需要按
   真实批量抓取（阶段 3.3 的 500 页/批）的观察结果调整，不能假设它一直
   稳定。

## 推翻条件

- 巨潮资讯网的公开接口被证明不稳定（大规模抓取时封 IP、要求验证码，或
  返回结构频繁变化）：改回评估商业供应商。
- 巨潮资讯网上线官方公开 API 文档且条款与现在假设的不同（例如要求签约、
  限制缓存）：按新条款重新登记 `core.source_registry`，不再套用"官方公开
  记录，无需商务确认"这条判断。
- iFind 未来能给出完整正文（而非片段）：重新评估是否作为第二层，参照
  `Capability.FINANCIALS` 目前"只有一层"的保守设计。

## 三次修订：移除 Wind 降级层（2026-09-29，公告双源接入计划 Task 13 实盘验收）

`Capability.ANNOUNCEMENTS_CN` 的 `wind-docs` 层（`get_company_announcements`）
从路由里删除，`ANNOUNCEMENTS_VERIFY`（Wind 漏收核对）整条能力一并删除：

```python
Capability.ANNOUNCEMENTS_CN: (
    LayerSpec("cninfo", service="cninfo-http", source="cninfo", degraded=False),
    LayerSpec("ifind-notice", service="ifind-news", source="ifind",
              degraded=True, full_text=False),
),
```

原因不是代码缺陷，是账号服务范围的误判：实盘验收对全部 A 股实体跑漏收核对时，
`get_company_announcements` 报"账户积分余额不足"——核实后这个账号购买的 Wind
MCP 服务范围只覆盖财务、行情、股东、分部数据，不包含公告检索。`vserver_
financial_docs` 这个服务在 API 层面确实存在、工具列表里也确实列着
`get_company_announcements`，但调用即报额度不足，说明账号侧从未开通这项。

Task 6 当初把它接进来时只做过 Mock 契约测试与一次性的 Live 探测（列出工具、
验证鉴权），没有在真实业务窗口下（跨月、跨全部 A 股实体）实际使用过，这类
额度类问题只有在这个量级的真实调用下才会暴露——不是"探测通过"能覆盖的。

**推翻条件**：账号方确认 Wind MCP 服务范围已包含公告检索且额度充足：重新评估
是否作为 `ANNOUNCEMENTS_CN` 的降级层。
