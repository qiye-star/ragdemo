"""港交所披露易（hkexnews.hk）公告适配器（公告双源接入计划 Task 7）。

与 `adapters/cninfo.py` 同一个模式，同一个理由（两次独立 HTTP 调用，`fetch_full_text`
另开一个 `HttpClient`；不实现 `AnnouncementProvider`，见 `cninfo.py` 模块文档字符串）。
披露易是港交所官方指定的披露平台，港股这条链路因此是单层——不需要也不该降级
（ADR-0012/0013、2026-09-28 公告双源接入计划分工表）。

`stockId` 是披露易内部的证券标识，查询接口需要它但公开代码里查不到映射表，
必须先用 `search/prefix.do` 按股票代码换一次；`resolve_stock_id()` 按进程内存缓存。

时点：`DATE_TIME` 是分钟精度（"23/09/2026 06:05"，DD/MM/YYYY HH:MM，香港时间）——
比巨潮的日精度更准，这里直接当作 `publish_at`，**不做**"当日 23:59"这层保守上界
换算（那只对日精度的源成立）。`core.source_registry` 里这个来源不该登记
`time_precision='day'`，登记成能反映真实精度的档位（或干脆不经 `DocumentWriter`
走这条换算，改走公告目录直接存 `known_at`——本模块只产出 `HkAnnouncementRef`，
真正落 `known_at` 是 `ingest/hkexnews_docs.py` 的事）。

分页：实测 `rowRange` 可以开到至少 500 且服务端按请求返回，本模块因此固定用一次
较大的 `rowRange` 覆盖查询窗口，不做真正的翻页（`pageNo` 类参数实测不生效）。
超过这个上限的公司/窗口组合会漏掉更早的公告——3 年窗口内实测阿里巴巴共 551 条，
属于极端量级；`parse_announcements` 返回的条数与响应里的 `recordCnt` 不一致时，
调用方应该能看到这个信号（`RawResponse.payload` 里两者都在）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.http import HttpClient

PROVIDER = "hkexnews"
ROW_RANGE = 500
_HKT = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class HkAnnouncementRef:
    news_id: str
    stock_code: str
    title: str
    publish_at: datetime  # 分钟精度，香港时间
    file_link: str

    @property
    def pdf_path(self) -> str:
        return self.file_link


class HkexnewsStockIdNotFound(RuntimeError):
    """prefix.do 按股票代码查不到对应的披露易 stockId。"""


def _parse_jsonp(body: str) -> Any:  # noqa: ANN401
    start, end = body.find("("), body.rfind(")")
    return json.loads(body[start + 1 : end])


class HkexnewsAdapter:
    """`query_client` 指向 `www1.hkexnews.hk`；PDF 下载与查询同一个主机，
    `fetch_full_text` 因此接受任意指向该主机的 `HttpClient`（不像 EDGAR/巨潮那样
    要求另一个 host，但仍然是独立参数，保持同一个调用约定）。"""

    provider = PROVIDER

    def __init__(self, query_client: HttpClient) -> None:
        self._client = query_client
        self._stock_id_cache: dict[str, str] = {}

    def health(self) -> bool:
        return True

    def resolve_stock_id(self, stock_code: str) -> str:
        """`prefix.do` 回 JSONP（`cb({...});`），不是纯 JSON——用 `get_bytes` 取原始文本，
        自己剥壳，不能走 `get_json`（它按纯 JSON 解析，会在这里直接报错）。"""
        if stock_code in self._stock_id_cache:
            return self._stock_id_cache[stock_code]
        body_bytes = self._client.get_bytes(
            "/search/prefix.do",
            {"callback": "cb", "lang": "ZH", "type": "A", "name": stock_code, "market": "SEHK"},
        )
        body = _parse_jsonp(body_bytes.decode("utf-8"))
        for row in body.get("stockInfo") or []:
            if str(row.get("code")) == stock_code:
                stock_id = str(row["stockId"])
                self._stock_id_cache[stock_code] = stock_id
                return stock_id
        raise HkexnewsStockIdNotFound(f"prefix.do 找不到股票代码 {stock_code!r} 对应的 stockId")

    def fetch(
        self, ctx: FetchContext, *, stock_code: str, since: datetime, until: datetime
    ) -> Iterator[RawResponse]:
        """一次查询覆盖整个窗口（模块文档字符串"分页"一节）。"""
        stock_id = self.resolve_stock_id(stock_code)
        params = {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "-1",
            "fromDate": since.strftime("%Y%m%d"),
            "toDate": until.strftime("%Y%m%d"),
            "title": "",
            "searchType": "0",
            "t1code": "-2",
            "t2Gcode": "-2",
            "t2code": "-2",
            "rowRange": str(ROW_RANGE),
            "lang": "zh",
        }
        raw = self._client.get_json("/search/titleSearchServlet.do", params)
        yield raw

    def parse_announcements(self, raw: RawResponse) -> Iterator[HkAnnouncementRef]:
        payload = raw.payload if isinstance(raw.payload, dict) else {}
        rows = json.loads(payload.get("result") or "[]")
        for row in rows:
            file_link = str(row.get("FILE_LINK") or "")
            if not file_link or str(row.get("FILE_TYPE")).upper() != "PDF":
                continue  # 没有 PDF 附件的条目拿不到可解析的原文，跳过而不是报错
            yield HkAnnouncementRef(
                news_id=str(row["NEWS_ID"]),
                stock_code=str(row["STOCK_CODE"]).split("<br/>")[0],
                title=str(row["TITLE"]),
                publish_at=_parse_publish_at(str(row["DATE_TIME"])),
                file_link=file_link,
            )

    def fetch_full_text(self, ref: HkAnnouncementRef, client: HttpClient) -> bytes:
        return client.get_bytes(ref.pdf_path, {})


def _parse_publish_at(value: str) -> datetime:
    """ "23/09/2026 06:05" → DD/MM/YYYY HH:MM，香港时间（实测确认，模块文档字符串）。"""
    return datetime.strptime(value, "%d/%m/%Y %H:%M").replace(tzinfo=_HKT)
