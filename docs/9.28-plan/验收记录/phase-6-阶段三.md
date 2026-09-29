# 阶段三（文档数据）进展记录

日期：2026-09-29。状态：**3.1 代码完成、真实调用被 DeepSeek 余额挡住；3.2 已出估算、待书面确认；3.3 未开始。**

## 3.1 真实表格描述器

- 已实现：`parse/describe_llm.py`（`LlmTableDescriber`）、`parse/describer_factory.py`（配了 `MODEL_BASE_URL` 用真实，没配退回 Mock）、提示词 `agents/prompts/table_describe/v2.md`。已接入巨潮、披露易、EDGAR 三层的 deps、`assets_docs`、`parse_pending`、`docs ingest` 等全部使用点。
- **拦截规则一个字没放宽**：模型输出仍然过 `validate_description`；被拦截时把原因回给模型让它改写，最多 3 次，仍不合格抛 `DescriptionRejected`（上层记为校验失败、不入块）。v2 提示词教模型对"增持数量"这类列名用中性说法概括（"股份变动数量"），而不是照抄。
- 出网：`infra/docker-compose.yml` 新增 `reverse:https://api.deepseek.com@8086`，Bearer 由代理注入 `MODEL_API_KEY`（代码里没有密钥）。`ragdemo-egress` 已按新配置重建。
- 测试：`tests/parse/test_describe_llm.py` 9 项（请求形状、提示词版本、拦截后重试、持续违规仍报错、长表截断保留行数、响应畸形、工厂选择）。
- **真实调用未验证**：经代理连通，密钥被接受，但 DeepSeek 返回 `402 Insufficient Balance`——账户余额为 0。充值后才能做 3.1 验收（"9-23 被拦截回滚的那份公告重跑通过"）。

## 3.2 海光全部公告：页数与费用估算（真实 PDF 页数）

为得到真实页数，先把海光（688041.SH）尚未下载的公告 PDF 补下载（免费，巨潮公开文件，`docs announcements --refetch --max-pages 0`，未触发任何解析）：目录 390 条，全部 PDF 已入库。

| 档位 | 含义 | 页数 | 按 0.04 元/页 |
|---|---|---|---|
| first | 季报、年报/半年报摘要、业绩预告/快报 | 132 | 约 5.3 元 |
| second | 年报 / 半年报全文 | 1523 | 约 60.9 元 |
| none | 其余公告（董事会决议、担保、募集资金等） | 3319 | 约 132.8 元 |
| **合计（380 份待解析文档）** | | **4974** | **约 199 元** |

按 500 页/批约 10 批。已解析的 7 份不在其内。TextIn 单价 `TEXTIN_COST_PER_PAGE_CNY=0.04`（此前系统记账因单价未接入而显示"单价未知"，已修复并回填历史记录）。

**待创始人书面确认**：解析哪些档位（建议先 first + second，约 66 元、1655 页；none 档信息价值低、占费用 2/3，可暂缓）。

## 过程中发现并修复的问题

补下载时真实数据触发了一个此前测试没覆盖到的错误：**当天发布**的公告，日精度 `known_at` 是当日 23:59:59.999999，此刻的 `now` 更早，同来源补全文取代旧行时把 `superseded_at` 写成 `now`，违反 `superseded_at > known_at`。同时旧实现在 `[known_at, now)` 之间会让新旧两行同时可见。修复：旧行在 `known_at + 1 微秒`即被取代（两行是同一份公告、同一 `known_at`）；主源取代降级行时至少取 `known_at + 1 微秒`。回归测试：当天补全文、任意 `as_of` 只见一行、主源早于降级行 `known_at` 到达。

## 其它

- `docs announcements` 新增 `--refetch`：忽略增量游标整段重取，用于补下载此前只建了目录的公告全文（已下载的不重复）。
- EDGAR 全文解析范围按负责人决定缩小为 2–3 份测试申报（总计划 3.4 已改）。
- 3.4b（iFind 公告适配器）已被巨潮、披露易、EDGAR 三源实现取代（Wind/iFind 均不用于公告，见 ADR-0013 修订），总计划该行待改写。
- 3.6：`search_blocks` / `get_block` / `get_document` 已由 `PgDataApi` 接上真实库（向量仍是 Mock，BM25 为真），随阶段二 2.10 完成。
