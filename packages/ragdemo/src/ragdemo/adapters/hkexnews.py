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

分页：`rowRange` 上限 500，`pageNo` 类参数实测不生效，没有真正的翻页。响应里的
`recordCnt` 是窗口内的真实总数、`loadedRecord`/`hasNextRow` 说明这次装载了多少——
装不下时（2026-09-29 实测腾讯 3 年窗口 recordCnt=535 只装载 500）**把日期窗口二分
再各查一次**，直到每个子窗口都装得下；单日仍装不下就抛 `HkexnewsListingIncomplete`。
被截断的那次响应照样 yield 出去（每次真实抓取都要留快照，CLAUDE.md §1.5），
用 `params["truncated"]` 标记，`expected_total` 不计它。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.errors import AdapterError
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
    file_link: str  # 没有附件时为空串
    file_type: str = "PDF"

    @property
    def downloadable(self) -> bool:
        """有 PDF 附件才能下载并解析；其余（HTM、无附件）照样是一份公告、必须进目录。"""
        return bool(self.file_link) and self.file_type.strip().lstrip(".").upper() == "PDF"

    @property
    def pdf_path(self) -> str:
        assert self.file_link, "只有 downloadable 的公告才有 PDF 路径"
        return self.file_link


class HkexnewsStockIdNotFound(RuntimeError):
    """prefix.do 按股票代码查不到对应的披露易 stockId。"""


class HkexnewsListingIncomplete(AdapterError):
    """列出的公告条数与响应自报的 `recordCnt` 对不上，或单日仍超过 `rowRange`——
    目录不完整（`AdapterError`：归为 REJECTED，不冷却这一层）。"""


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
        """装不下就二分日期窗口（模块文档字符串"分页"一节）。"""
        stock_id = self.resolve_stock_id(stock_code)
        yield from self._fetch_range(stock_id, stock_code, since.date(), until.date())

    def _fetch_range(
        self, stock_id: str, stock_code: str, start: date, end: date
    ) -> Iterator[RawResponse]:
        params = {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "-1",
            "fromDate": start.strftime("%Y%m%d"),
            "toDate": end.strftime("%Y%m%d"),
            "title": "",
            "searchType": "0",
            "t1code": "-2",
            "t2Gcode": "-2",
            "t2code": "-2",
            "rowRange": str(ROW_RANGE),
            "lang": "zh",
        }
        raw = self._client.get_json("/search/titleSearchServlet.do", params)
        payload = raw.payload if isinstance(raw.payload, dict) else {}
        loaded = len(json.loads(payload.get("result") or "[]"))
        record_cnt = int(payload.get("recordCnt") or 0)
        truncated = bool(payload.get("hasNextRow")) or loaded < record_cnt
        if not truncated:
            yield raw
            return
        if start >= end:
            raise HkexnewsListingIncomplete(
                f"{stock_code} 在 {start} 一天内就有 {record_cnt} 条，超过 rowRange={ROW_RANGE}"
            )
        yield replace(raw, params={**raw.params, "truncated": "1"})
        mid = start + (end - start) // 2
        yield from self._fetch_range(stock_id, stock_code, start, mid)
        yield from self._fetch_range(stock_id, stock_code, mid + timedelta(days=1), end)

    @staticmethod
    def expected_total(raws: Sequence[RawResponse]) -> int | None:
        """各个**完整**子窗口自报的 `recordCnt` 之和（被截断的那次不计——它的行是
        子窗口行的子集）。没有任何完整响应时返回 `None`。"""
        counts = [
            int(p.get("recordCnt") or 0)
            for raw in raws
            if isinstance(p := raw.payload, dict) and not raw.params.get("truncated")
        ]
        return sum(counts) if counts else None

    def parse_announcements(self, raw: RawResponse) -> Iterator[HkAnnouncementRef]:
        payload = raw.payload if isinstance(raw.payload, dict) else {}
        rows = json.loads(payload.get("result") or "[]")
        for row in rows:
            yield HkAnnouncementRef(
                news_id=str(row["NEWS_ID"]),
                stock_code=str(row["STOCK_CODE"]).split("<br/>")[0],
                title=str(row["TITLE"]),
                publish_at=_parse_publish_at(str(row["DATE_TIME"])),
                file_link=str(row.get("FILE_LINK") or ""),
                file_type=str(row.get("FILE_TYPE") or ""),
            )

    def fetch_full_text(self, ref: HkAnnouncementRef, client: HttpClient) -> bytes:
        return client.get_bytes(ref.pdf_path, {})


def _parse_publish_at(value: str) -> datetime:
    """ "23/09/2026 06:05" → DD/MM/YYYY HH:MM，香港时间（实测确认，模块文档字符串）。"""
    return datetime.strptime(value, "%d/%m/%Y %H:%M").replace(tzinfo=_HKT)
