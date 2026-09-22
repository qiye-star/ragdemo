"""本地 manifest 加载器：`manifest.jsonl` 一行 → 一个 `NormalizedDocument`。

**刻意不实现 `AnnouncementProvider`**（`adapters/announcements.py`）——那个
Protocol 是给供应商适配器用的，契约测试（`tests/contracts/announcement_
contract.py`）验的是"这个供应商接口在增量抓取、健康检查等场景下行为一致"。
把一个本地目录扮成供应商既不诚实，又会平白拖进 6 条与本模块无关的契约测试。
这里只是一个普通的加载函数：读 manifest，读本地文件，交回 `NormalizedDocument`
列表。

两个会咬人的地方，都在 CLAUDE.md §1.1 时点一致性的直接管辖范围内：

1. **`known_at` 必须落在 `23:59:59+08:00`**。manifest 给的是 date-only、
   无时区的午夜（`"2026-04-08 00:00:00"`）；`NormalizedDocument.__post_init__`
   经 `require_aware()` 拒绝 naive datetime，必须补时区。补 `00:00+08:00`
   看似最贴近原始字符串，但会让 `known_at` 比真实的"最早可获知时刻"提早
   最多 24 小时——`known_at <= as_of` 的过滤会在这份文档实际公开披露之前
   就判定"已知"，这正是 `docs/03-point-in-time.md` 要防的前视偏差方向。
   补 `23:59:59+08:00`（当日收盘后）只会让系统"知道得比实际晚"，不会
   "提前知道"——晚是安全的，早不是。`docs/03-point-in-time.md` §1.3
   对"供应商只给日期不给时间"的公告类数据也明确写着同样的取法。
2. **`content_hash` 必须先用真实文件字节验证过 manifest 的 `sha256`，
   再采用**。本地文件可能在下载后被截断、替换或者根本没写完；不校验的话，
   一份损坏的 PDF 会被静默当成一份正常文档送进管线，产出的块要么是垃圾
   要么让下游解析莫名其妙地失败，而错误现场早就丢了。校验失败在这里
   立刻大声拒绝（`ManifestError`），不下放给后面的步骤。
"""

from __future__ import annotations

import hashlib
import json
import re
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo_core.blob import BlobStore

# 供应商只给日期不给时间的公告，按当日 23:59:59 本地时区取 known_at
# （docs/03-point-in-time.md §1.3）。这份 manifest 全部来自巨潮资讯网，
# 时间语境是中国大陆 A 股披露，固定 +08:00，不随本机时区变化。
CST = timezone(timedelta(hours=8))

_ONLY_KIND = "announcement"  # research（券商研报）按 ADR-0008 后果 1 与
# README 的著作权限制不外送、不入库，见 load_documents 里的过滤。

_QUARTERLY_MARKERS = ("季度报告", "半年度报告")
_ANNUAL_MARKER = "年度报告"

_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?!s)")
_OBJ_BLOCK = re.compile(rb"(?:\d+)\s+\d+\s+obj(.*?)endobj", re.DOTALL)
_STREAM_START = re.compile(rb"stream\r?\n")


class ManifestError(RuntimeError):
    """manifest 行本身，或它指向的本地文件，有问题。"""


@dataclass(frozen=True)
class ManifestEntry:
    """`manifest.jsonl` 一行的原样字段，转换成 `NormalizedDocument` 之前的中间形态。"""

    kind: str
    entity_code: str
    entity_name: str
    title: str
    known_at_raw: str
    source: str
    source_id: str
    url: str
    local_path: str
    bytes_size: int
    sha256: str
    org: str
    extra: Mapping[str, Any]


def doc_type_for(title: str) -> str:
    """年度报告 → annual_report；季度报告 / 半年度报告 → quarterly；其余 → announcement。

    对照 `ingest/definitions.py` 的 `TRACKED_DOC_TYPES`，这样新文档触发传感器
    才能被看到。**半年度报告的标题本身也包含"年度报告"四个连续字**（"半"
    + "年度报告"），必须先判季度/半年度关键词，再判普通年度报告，否则半年报
    会被误判成 annual_report。
    """
    if any(marker in title for marker in _QUARTERLY_MARKERS):
        return "quarterly"
    if _ANNUAL_MARKER in title:
        return "annual_report"
    return "announcement"


def known_at_from_manifest(raw: str) -> datetime:
    """manifest 的 `known_at` 是 `"YYYY-MM-DD HH:MM:SS"`，永远是当日午夜、无时区。

    见模块 docstring 第 1 条：补 `23:59:59+08:00` 而不是 `00:00+08:00`，
    宁可晚知道，不可早知道。
    """
    naive = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    return datetime.combine(naive.date(), time(23, 59, 59), tzinfo=CST)


def count_pdf_pages(data: bytes) -> int:
    """不调用任何 API、不引入任何新依赖，本地估算一份 PDF 的页数。

    按页计费的解析器（TextIn xParse）开工前，dry run 需要一个"大概要花
    多少页"的数字——这个数字必须完全离线算出来，不能靠猜或者靠调一次
    真实解析。PDF 规范里每一页都是一个 `/Type /Page` 对象，正常情况下
    数一遍这个模式出现的次数就是页数。

    唯一的复杂之处：较新的 PDF 生成器会把多个对象打包进一个压缩的
    `/Type /ObjStm`（object stream）省空间，这时候 Page 对象不会以明文
    出现在文件里，直接数会漏掉（实测本仓库语料 308 份公告里有 98 份用了
    ObjStm）。处理办法是找到每个 `ObjStm` 对象自己的 `stream ... endstream`
    段，用标准库 `zlib` 解压（PDF 里 ObjStm 几乎总是 `/Filter /FlateDecode`），
    再在解压结果里重复同样的模式匹配——ObjStm 展开后的内容就是若干个对象
    的字典/内容原样拼接，句法和明文对象完全一样。

    两部分累加时不会重复计数：一个对象要么以明文形式存在，要么被压进
    某个 ObjStm，PDF 规范不允许同一个对象同时以两种形式出现。

    这不是一个完整的 PDF 解析器（没有处理 xref、加密、增量更新等），
    但在真实语料上验证过：全部 308 份公告 PDF 都能数出非零页数，且对
    首批入库计划里明确写出页数的 4 份文档（15/4/12/12 页）逐一核对一致。
    数不出来（比如损坏文件或非 PDF 字节）时返回 0，调用方要把 0 当作
    "无法本地估算"处理，而不是当真的"零页"。
    """
    direct = len(_PAGE_OBJECT.findall(data))
    in_object_streams = 0
    for match in _OBJ_BLOCK.finditer(data):
        body = match.group(1)
        if b"/ObjStm" not in body:
            continue
        stream_start = _STREAM_START.search(body)
        if stream_start is None:
            continue
        end = body.find(b"endstream", stream_start.end())
        if end == -1:
            continue
        raw_stream = body[stream_start.end() : end].rstrip(b"\r\n")
        try:
            decompressed = zlib.decompress(raw_stream)
        except zlib.error:
            continue  # 不是 FlateDecode，或者流本身就是坏的——跳过，不当崩溃处理
        in_object_streams += len(_PAGE_OBJECT.findall(decompressed))
    return direct + in_object_streams


def iter_manifest_entries(manifest_path: Path) -> Iterator[ManifestEntry]:
    """逐行读 `manifest.jsonl`，跳过空行。不做过滤——kind / 标题筛选交给调用方。"""
    with manifest_path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            raw: dict[str, Any] = json.loads(stripped)
            yield ManifestEntry(
                kind=str(raw["kind"]),
                entity_code=str(raw["entity_code"]),
                entity_name=str(raw["entity_name"]),
                title=str(raw["title"]),
                known_at_raw=str(raw["known_at"]),
                source=str(raw["source"]),
                source_id=str(raw["source_id"]),
                url=str(raw["url"]),
                local_path=str(raw["local_path"]),
                bytes_size=int(raw["bytes"]),
                sha256=str(raw["sha256"]),
                org=str(raw.get("org", "")),
                extra=raw.get("extra") or {},
            )


def load_local_bytes(manifest_path: Path, entry: ManifestEntry) -> bytes:
    """读本地文件字节，校验 sha256 与 manifest 一致；不一致就大声拒绝。

    `manifest.jsonl` 里的 `local_path` 是相对 manifest 文件所在目录的路径
    （`docs/rawdata/688041-hygon/manifest.jsonl` 与其同目录下的
    `announcements/`），不是相对当前工作目录。
    """
    file_path = manifest_path.parent / entry.local_path
    data = file_path.read_bytes()
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != entry.sha256:
        raise ManifestError(
            f"{entry.local_path} 的 sha256 与 manifest 不符"
            f"（manifest={entry.sha256}, 实际={actual_sha256}）——"
            "本地文件可能已损坏或被替换，拒绝当作正常文档处理"
        )
    return data


def to_normalized_document(
    entry: ManifestEntry,
    data: bytes,
    blob: BlobStore,
    *,
    entity_ref: str,
) -> NormalizedDocument:
    """把已校验过的字节放进 blob store，再构造 `NormalizedDocument`。

    `raw_bytes_ref` 必须在 `blob.put()` 之后才能引用它——`prepare_documents`
    (`ingest/assets_docs.py`) 靠 `blob.get(doc.raw_bytes_ref)` 取原始字节，
    这一步不做的话，写进 `NormalizedDocument` 的 key 在 blob store 里根本
    不存在。

    `entity_ref` 是调用方显式传入的值（CLI 的 `--entity-ref`），**不是**
    manifest 里的 `entity_code`：manifest 给的 "688041" 匹配不上
    `DocumentWriter._resolve_entity_id` 只查的 `tushare_code` /
    `ifind_code` / `wind_code` / `edgar_cik` 四列（它不查 `entity_alias`），
    传 "688041" 会静默解析成 `entity_id = NULL`——一份按实体查不到的文档。
    """
    raw_bytes_ref = f"raw/{entry.source}/{entry.sha256}.pdf"
    blob.put(raw_bytes_ref, data)
    known_at = known_at_from_manifest(entry.known_at_raw)
    return NormalizedDocument(
        provider_doc_id=entry.source_id,
        entity_ref=entity_ref,
        doc_type=doc_type_for(entry.title),
        title=entry.title,
        period=None,
        # NormalizedDocument 只有 publish_at 这一个时间戳字段；manifest 只给
        # 日期精度的披露时刻，与 known_at 是同一个值（本模块 docstring 第 1
        # 条），两者在这里重合是诚实的表达，不是偷懒——我们确实不知道更精确
        # 的发布时刻。DocumentWriter 用默认 disclosure_lag=0 时，
        # known_at_for(publish_at, 0) == publish_at，写库的 known_at 因此
        # 正好落在 23:59:59+08:00。
        publish_at=known_at,
        language="zh",
        source_url=entry.url or None,
        raw_bytes_ref=raw_bytes_ref,
        content_hash=entry.sha256,
        is_correction=False,
        supersedes_provider_doc_id=None,
        page_count=None,  # 解析后由 prepare_documents 用真实页数覆盖
        blocks=[],
    )


def load_documents(
    manifest_path: Path,
    blob: BlobStore,
    *,
    entity_ref: str,
    select_title: str | None = None,
) -> list[NormalizedDocument]:
    """加载 manifest，过滤，逐条校验并放进 blob，返回 `NormalizedDocument` 列表。

    只保留 `kind == "announcement"`——研报（`kind == "research"`）著作权归
    发布券商所有，`docs/rawdata/688041-hygon/README.md` 明确写了不得对外
    分发；这条过滤没有开关，不是一个可选策略。
    """
    docs: list[NormalizedDocument] = []
    for entry in iter_manifest_entries(manifest_path):
        if entry.kind != _ONLY_KIND:
            continue
        if select_title and select_title not in entry.title:
            continue
        data = load_local_bytes(manifest_path, entry)
        docs.append(to_normalized_document(entry, data, blob, entity_ref=entity_ref))
    return docs
