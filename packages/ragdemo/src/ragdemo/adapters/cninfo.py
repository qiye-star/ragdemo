"""巨潮资讯网（cninfo.com.cn）公告适配器（adr/0013）。

**刻意不实现 `AnnouncementProvider`**（`adapters/announcements.py`）——那个
Protocol 的 `normalize(raw: RawResponse) -> NormalizedDocument` 只接受一次
已经取回的响应，但巨潮天然是两次独立 HTTP 调用：先查询公告列表
（`hisAnnouncement/query`，拿到 `adjunctUrl`），再下载 PDF 字节
（另一个主机 `static.cninfo.com.cn`）——且两次调用都要各自留一条
`provider_snapshot`（CLAUDE.md §1.5）。把第二次调用塞进 `normalize()`
内部意味着这个方法需要偷偷持有 `BlobStore` 与 `psycopg.Connection`，
掩盖了它其实会发起真实网络调用与数据库写入这件事。

EDGAR 在 P1 遇到过完全相同的结构（`adapters/edgar.py` 的 `fetch()` 拿列表，
`fetch_full_text()` 另开一个主机拿全文），当时的解法是不实现协议，改用一对
专用函数直接构造 `NormalizedDocument`（`ingest/edgar_docs.py`）。这里照抄
同一个已验证过的模式，见 `ingest/cninfo_docs.py`。

`orgId` 是巨潮内部的公司标识，查询接口需要它但公开代码里查不到映射表，
必须先用 `information/topSearch/query` 按股票代码换一次。`resolve_org_id()`
按进程内存缓存，同一个 `CninfoAdapter` 实例不会为同一支股票重复查两次。

时点：`announcementTime` 字段只精确到"日"（实测确认，见 adr/0013"实测
记录"），本模块不在这里做"当日 23:59:59 保守上界"这层换算——那是
`ingest/documents.py::DocumentWriter._known_at()` 已经在做的事，只要
`core.source_registry.cninfo.time_precision = 'day'`（`020_cninfo_source_
registry.sql`），公开的 `publish_at` 原样传公告发布日的午夜即可，写入层
自己会换算成保守上界。这里如果重复做一遍换算，反而会在两处各自维护
同一条时点规则，日后改一处忘了改另一处。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.http import HttpClient

PROVIDER = "cninfo"
PAGE_SIZE = 30
MAX_PAGES = 300  # 防死循环；30 条/页 × 300 页 = 9000 条，远超单个实体三年的公告量
_BEIJING = ZoneInfo("Asia/Shanghai")

# column 参数按上市板块选交易所路由；深市/北交所与沪市各有独立取值，
# 查询时两个都要试一遍才能覆盖任意一支股票——单独传错一个会得到"命中 0 条"
# 而不是报错，是这个接口本身的行为，不是我们判断失误。
_COLUMNS = ("szse", "sse")


@dataclass(frozen=True)
class AnnouncementRef:
    announcement_id: str
    sec_code: str
    org_id: str
    title: str
    publish_at: datetime
    adjunct_url: str | None
    adjunct_type: str | None = None

    @property
    def downloadable(self) -> bool:
        """有 PDF 附件才能下载并解析。没有附件（或附件不是 PDF）的公告照样是一份
        公告、必须进目录——只是拿不到可解析原文。`adjunctType` 近期公告是 ".PDF"
        （带点），必须归一化后再比，否则近期公告会被误判成无附件。"""
        kind = (self.adjunct_type or "").strip().lstrip(".").upper()
        return bool(self.adjunct_url) and kind == "PDF"

    @property
    def pdf_path(self) -> str:
        assert self.adjunct_url is not None, "只有 downloadable 的公告才有 PDF 路径"
        return f"/{self.adjunct_url}"

    @property
    def detail_url(self) -> str:
        """公告详情页，没有 PDF 时目录行用它当链接。"""
        return (
            "https://www.cninfo.com.cn/new/disclosure/detail"
            f"?stockCode={self.sec_code}&announcementId={self.announcement_id}"
            f"&orgId={self.org_id}&announcementTime={self.publish_at.date().isoformat()}"
        )


class CninfoOrgIdNotFound(RuntimeError):
    """topSearch 按股票代码查不到对应的巨潮 orgId。"""


class CninfoListingIncomplete(AdapterError):
    """列出的公告条数与接口自报总数对不上，或窗口大到翻页上限——目录不完整，
    宁可这一次失败也不静默少写（`AdapterError`：归为 REJECTED，不冷却这一层）。"""


class CninfoAdapter:
    """`query_client` 指向 `www.cninfo.com.cn`。PDF 下载走另一个主机
    （`static.cninfo.com.cn`），由调用方另外构造一个 `HttpClient` 传给
    `fetch_full_text()`——与 `EdgarAdapter.fetch_full_text(ref, client)`
    同一个理由：两个不同主机不能共用同一个 `HttpClient` 实例（`base_url`
    在构造时固定）。
    """

    provider = PROVIDER

    def __init__(self, query_client: HttpClient) -> None:
        self._client = query_client
        self._org_id_cache: dict[str, str] = {}

    def health(self) -> bool:
        return True

    def resolve_org_id(self, stock_code: str) -> str:
        if stock_code in self._org_id_cache:
            return self._org_id_cache[stock_code]
        raw = self._client.post_form(
            "/new/information/topSearch/query", {"keyWord": stock_code, "maxNum": 5}
        )
        for row in raw.payload or []:
            if str(row.get("code")) == stock_code:
                org_id = str(row["orgId"])
                self._org_id_cache[stock_code] = org_id
                return org_id
        raise CninfoOrgIdNotFound(f"topSearch 找不到股票代码 {stock_code!r} 对应的 orgId")

    def fetch(
        self, ctx: FetchContext, *, stock_code: str, since: datetime, until: datetime
    ) -> Iterator[RawResponse]:
        """按页拉取公告列表，一页一个 `RawResponse`（与 `EdgarAdapter.fetch()`
        "一次 HTTP 调用一个 RawResponse" 同一个约定）。

        **翻页以 `hasMore` 为准，不以 `totalpages` 为准**：`totalpages` 是向下取整的
        （totalAnnouncement=433、每页 30 条时它报 14 而不是 15），翻到它就停会
        系统性丢掉最后一个不满页——2026-09-29 抽查 8 家实体，入库条数全是 30 的
        整数倍。响应没有 `hasMore` 字段时退回 `totalpages`（旧版/测试夹具）。

        沪深两个 `column` 依次试：第一个有结果就不再试第二个（真实接口在 `stock`
        已定时两个 column 返回同样的全量结果，两个都翻等于白白翻倍请求）；
        第一个 0 条才试第二个。
        """
        org_id = self.resolve_org_id(stock_code)
        se_date = f"{since.date().isoformat()}~{until.date().isoformat()}"
        for column in _COLUMNS:
            got_rows = False
            page = 1
            while page <= MAX_PAGES:
                params = {
                    "stock": f"{stock_code},{org_id}",
                    "tabName": "fulltext",
                    "pageSize": PAGE_SIZE,
                    "pageNum": page,
                    "column": column,
                    "category": "",
                    "seDate": se_date,
                }
                raw = self._client.post_form("/new/hisAnnouncement/query", params)
                yield raw
                payload = raw.payload or {}
                rows = payload.get("announcements") or []
                got_rows = got_rows or bool(rows)
                if not rows or not _has_more(payload, page):
                    break
                page += 1
            else:
                raise CninfoListingIncomplete(
                    f"{stock_code} 翻到 {MAX_PAGES} 页仍有更多，窗口 {se_date} 太大，请缩小"
                )
            if got_rows:
                return

    @staticmethod
    def expected_total(raws: Sequence[RawResponse]) -> int | None:
        """接口自报的公告总数（`totalAnnouncement`），供调用方与实际列出的条数对账。
        响应里没有这个字段返回 `None`（无法对账，不是 0）。"""
        totals = [
            int(p["totalAnnouncement"])
            for raw in raws
            if isinstance(p := raw.payload, dict) and p.get("totalAnnouncement") is not None
        ]
        return max(totals) if totals else None

    def parse_announcements(self, raw: RawResponse) -> Iterator[AnnouncementRef]:
        for row in (raw.payload or {}).get("announcements") or []:
            adjunct_url = row.get("adjunctUrl")
            adjunct_type = row.get("adjunctType")
            yield AnnouncementRef(
                announcement_id=str(row["announcementId"]),
                sec_code=str(row["secCode"]),
                org_id=str(row["orgId"]),
                title=str(row["announcementTitle"]),
                publish_at=_parse_announcement_time(row["announcementTime"]),
                adjunct_url=str(adjunct_url) if adjunct_url else None,
                adjunct_type=str(adjunct_type) if adjunct_type else None,
            )

    def fetch_full_text(self, ref: AnnouncementRef, static_client: HttpClient) -> bytes:
        """`static_client` 必须指向 `static.cninfo.com.cn`，不复用 `self._client`
        （它的 `base_url` 是 `www.cninfo.com.cn`）。"""
        return static_client.get_bytes(ref.pdf_path, {})


def _parse_announcement_time(value: Any) -> datetime:  # noqa: ANN401
    """`announcementTime` 是毫秒 epoch 时间戳，解码后落在北京时间当日 0 点
    （实测确认，见 adr/0013）。这里只做时区转换，不做"日精度保守上界"的
    换算——那一步由 `DocumentWriter._known_at()` 按 `source_registry.
    time_precision` 统一处理（模块 docstring）。
    """
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).astimezone(_BEIJING)


def _has_more(payload: dict[str, Any], page: int) -> bool:
    """还有没有下一页。优先看接口自己的 `hasMore`；没有这个字段才退回 `totalpages`。"""
    if "hasMore" in payload:
        return bool(payload["hasMore"])
    return page < int(payload.get("totalpages") or 0)
