"""SEC EDGAR 适配器。

EDGAR 是 P1 唯一免费且真实的语料来源。在公告供应商选定前，它承担
「用真实数据检验管线」的职责（adr/0005 的风险缓解），因此评测集中
要求至少 30 条基于 EDGAR 真实文档。

known_at 取 acceptanceDateTime（EDGAR 系统实际受理该申报的精确时刻，
精确到秒），不取 filingDate——filingDate 常常只是日历日期不带时间，
用它会重演本计划里已经出现过两次的前视偏差类 bug（Mock 适配器的
UTC/CST 边界、Tushare 的 end_date/ann_date 混用）。见
docs/03-point-in-time.md §1.3。

fetch() 的 replay 分支把 submissions 端点「一份申报一个数组下标」的
列式结构，展开成「一份申报一个字典」的行式列表再放进 RawResponse.payload。
这一步是必须的：AdapterContract 的通用契约测试（test_known_at_is_timezone_aware_
and_not_future / test_backfill_known_at_stays_historical）直接对
fetch() 产出的 payload 做 `list(payload) if isinstance(payload, list) else [payload]`
再逐条调用 known_at()。如果 payload 保持未展开的外层 dict（cik/name/filings），
就会被当成"一条记录"整体传给 known_at()，known_at() 在顶层找不到
acceptanceDateTime 而抛 KeyError——这正是 tushare.py 文档记录过、
已经修过一次的同类 bug，这里用同样的 _rows() 展开手法避免重犯。
parse_filings() 复用同一个 _rows()，因此既能吃 fetch() 展开后的行列表，
也能直接吃测试里 _raw() 构造的原始未展开响应。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ragdemo.adapters.base import FetchContext, RawResponse, require_aware
from ragdemo.adapters.http import HttpClient

TRACKED_FORMS = frozenset({"10-K", "10-Q", "8-K"})
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
_EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class FilingRef:
    cik: str
    accession: str
    form_type: str
    filing_date: date
    acceptance_datetime: datetime
    primary_document: str

    def __post_init__(self) -> None:
        require_aware(self.acceptance_datetime, "acceptance_datetime")

    @property
    def document_url(self) -> str:
        return (
            f"{ARCHIVES}/{int(self.cik)}/{self.accession.replace('-', '')}/{self.primary_document}"
        )


class EdgarAdapter:
    provider = "edgar"

    def __init__(self, client: HttpClient | None = None, *, replay_dir: Path | None = None) -> None:
        if (client is None) == (replay_dir is None):
            raise ValueError("client 与 replay_dir 必须且只能提供一个")
        self._client = client
        self._replay_dir = replay_dir

    @classmethod
    def for_replay(cls, fixtures_dir: Path) -> EdgarAdapter:
        return cls(replay_dir=fixtures_dir)

    def health(self) -> bool:
        return True if self._replay_dir is not None else self._client is not None

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:  # noqa: ANN401
        if self._replay_dir is not None:
            for path in sorted(self._replay_dir.glob("*.json")):
                envelope = json.loads(path.read_text(encoding="utf-8"))
                yield RawResponse(
                    provider=self.provider,
                    endpoint=f"/submissions/{path.stem}.json",
                    params={"partition": ctx.partition_date.isoformat()},
                    payload=_rows(envelope),
                    http_status=200,
                    fetched_at=datetime.now(UTC),
                )
            return
        assert self._client is not None
        cik = str(params["cik"]).zfill(10)
        raw = self._client.get_json(f"/submissions/CIK{cik}.json", {})
        yield replace(raw, payload=_rows(raw.payload))

    def known_at(self, record: Any) -> datetime:  # noqa: ANN401
        """acceptanceDateTime 是 EDGAR 公开受理该申报的精确时刻。"""
        return _parse_acceptance(record["acceptanceDateTime"])

    def parse_filings(self, raw: RawResponse) -> Iterator[FilingRef]:
        for row in _rows(raw.payload):
            form_type = str(row["form"])
            if form_type not in TRACKED_FORMS:
                continue
            yield FilingRef(
                cik=str(row["cik"]),
                accession=str(row["accessionNumber"]),
                form_type=form_type,
                filing_date=date.fromisoformat(str(row["filingDate"])),
                acceptance_datetime=_parse_acceptance(row["acceptanceDateTime"]),
                primary_document=str(row["primaryDocument"]),
            )


def _rows(payload: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """submissions 端点返回「一个字段一个等长数组」的列式结构，转成行式字典列表。

    若传入的已经是行式字典列表（fetch() 内部的展开产出），原样返回——
    这样 parse_filings() 既能接受 fetch() 展开过的结果，也能接受
    _raw() 直接构造的原始未展开响应，两种上游都不用改。
    """
    if isinstance(payload, list):
        return payload
    cik = str(payload["cik"])
    recent = payload["filings"]["recent"]
    count = len(recent["accessionNumber"])
    return [
        {
            "cik": cik,
            "accessionNumber": recent["accessionNumber"][i],
            "form": recent["form"][i],
            "filingDate": recent["filingDate"][i],
            "acceptanceDateTime": recent["acceptanceDateTime"][i],
            "primaryDocument": recent["primaryDocument"][i],
        }
        for i in range(count)
    ]


def _parse_acceptance(value: Any) -> datetime:  # noqa: ANN401
    """EDGAR 的 acceptanceDateTime 形如 '2024-11-20T16:31:24.000Z'。

    这个末尾的 'Z' 具有误导性——它通常表示 UTC，但 SEC 自己的文档与
    EDGAR API 的实际行为里，acceptanceDateTime 报的是**美国东部时间**
    （EST/EDT，随夏令时切换），不是 UTC。直接把 'Z' 替换成 '+00:00'
    会把东部时间当 UTC 读，读出来的 known_at 系统性地早了 4~5 小时。

    验证：夹具里 NVIDIA 10-Q 的 acceptanceDateTime 是
    '2024-11-20T16:31:24.000Z'。如果当 UTC 读，换算成东部时间是
    11:31 —— 一份财报在美股开盘前、盘中就"受理"，与 NVDA 实际的
    盘后发布节奏对不上；当东部时间读，16:31 ET 正是盘后受理的
    合理时刻。按 docs/03-point-in-time.md §1.3 的通则——不确定时
    取更晚的时刻——也应该选后者。

    所以：把去掉 'Z' 后的裸时间戳当**东部时间**（America/New_York）
    解析，再转成 UTC 存储；ZoneInfo 会按日期自动处理 EST/EDT。
    """
    naive = datetime.fromisoformat(str(value).removesuffix("Z"))
    return naive.replace(tzinfo=_EASTERN).astimezone(UTC)
