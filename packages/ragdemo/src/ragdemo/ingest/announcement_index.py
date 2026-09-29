"""公告目录写入器（公告双源接入计划 Task 3）。

`core.announcement_index`（迁移 021）每份公告一行、不论来源。这里只负责两件事：
1. 跨源去重/取代——同一份公告（同实体、同发布日、标题归一化后相同）如果先被降级层
   （Wind/iFind，`has_full_text=False`）写了目录行，主源（cninfo）到达时取代它；
   反过来，主源已经写过的公告，降级层再报到就不写，直接压制。
2. entity_ref → entity_id 解析，解析不到直接拒绝——与 `core.announcement_index.entity_id`
   的 NOT NULL 约束一致，不像 `DocumentWriter._resolve_entity_id` 那样为政策/宏观类文档
   容忍 NULL（公告永远挂在具体某家公司名下）。

`known_at` 由调用方（各来源的适配器）按自己的时间精度算好传入，这里原样存，不重算——
cninfo 只到日、披露易到分钟、EDGAR 到秒，"当日 23:59 保守上界"只对日精度的源成立，
写入器不该替所有来源做同一个假设。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum

import psycopg

from ragdemo.ingest.writer import UnknownEntityRef

_BEIJING = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class IndexEntry:
    entity_ref: str  # tushare_code 或等价的市场代码（core.entity 的任一代码列）
    source: str  # cninfo / wind / ifind / hkexnews / edgar / edinet / opendart
    source_ref: str
    title: str
    publish_date: date
    known_at: datetime
    has_full_text: bool
    url: str | None = None
    excerpt: str | None = None
    snapshot_id: int | None = None


class IndexOutcome(StrEnum):
    INSERTED = "inserted"
    SKIPPED = "skipped"
    SUPERSEDED_DEGRADED = "superseded_degraded"  # 主源到达，取代同一公告的降级行
    SHADOWED = "shadowed"  # 降级行到达时主源行已在，不写


def known_at_for_day(day: date) -> datetime:
    """日精度来源（cninfo）专用：当日 23:59:59.999999 北京时间，保守上界。"""
    return datetime.combine(day, time(23, 59, 59, 999999), tzinfo=_BEIJING)


def normalize_title(title: str, short_names: list[str] | tuple[str, ...]) -> str:
    """去掉公司简称前缀（"海光信息：" / "海光信息:"）、NFKC 规整全角字符、去首尾空白。
    跨源比对靠它——同一份公告在不同源里的标题只差这个前缀。"""
    text = unicodedata.normalize("NFKC", title).strip()
    for name in short_names:
        for sep in ("：", ":"):
            prefix = f"{name}{sep}"
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
    return text


class IndexWriter:
    def __init__(
        self, conn: psycopg.Connection, *, ingest_run_id: str, now: Callable[[], datetime]
    ) -> None:
        self.conn = conn
        self.ingest_run_id = ingest_run_id
        self._now = now
        self._entity_cache: dict[str, str] = {}

    def _entity_id(self, entity_ref: str) -> str:
        if entity_ref not in self._entity_cache:
            row = self.conn.execute(
                "SELECT entity_id FROM core.entity WHERE tushare_code = %s"
                " OR ifind_code = %s OR wind_code = %s OR edgar_cik = %s",
                (entity_ref, entity_ref, entity_ref, entity_ref),
            ).fetchone()
            if row is None:
                raise UnknownEntityRef(f"无法解析实体代码 {entity_ref!r} 为 entity_id")
            self._entity_cache[entity_ref] = str(row[0])
        return self._entity_cache[entity_ref]

    def _short_names(self, entity_id: str) -> list[str]:
        """跨源标题匹配要去掉的公司名前缀：不同源各自把公司简称/全称拼在标题前面
        （"海光信息：…" / "海光信息:…"），去掉哪个前缀取决于这家公司自己的名字，
        不是一份全局清单。"""
        row = self.conn.execute(
            "SELECT name_short, name_full FROM core.entity WHERE entity_id = %s", (entity_id,)
        ).fetchone()
        if row is None:
            return []
        return [name for name in row if name]

    def write(self, entry: IndexEntry) -> tuple[IndexOutcome, int]:
        entity_id = self._entity_id(entry.entity_ref)
        with self.conn.transaction():
            existing = self.conn.execute(
                "SELECT index_id, has_full_text FROM core.announcement_index"
                " WHERE source = %s AND source_ref = %s AND superseded_at IS NULL",
                (entry.source, entry.source_ref),
            ).fetchone()
            supersede_same_source: int | None = None
            if existing is not None:
                if entry.has_full_text and not existing[1]:
                    # 同一来源的同一份公告，之前只建了目录（如实体当时不在下载范围内），
                    # 这次带着全文再来：旧行取代、新行带全文。不能因为 (source,
                    # source_ref) 已存在就 SKIPPED——那样目录永远写着"没有全文"，
                    # 而 core.document 里其实已经有了。反过来（旧行有全文、新写入
                    # 只是目录）不降级，仍然 SKIPPED。
                    supersede_same_source = int(existing[0])
                else:
                    return IndexOutcome.SKIPPED, int(existing[0])

            title_norm = normalize_title(entry.title, self._short_names(entity_id))
            match = self.conn.execute(
                "SELECT index_id, has_full_text FROM core.announcement_index"
                " WHERE entity_id = %s AND publish_date = %s AND title_norm = %s"
                "   AND superseded_at IS NULL",
                (entity_id, entry.publish_date, title_norm),
            ).fetchone()

            outcome = IndexOutcome.INSERTED
            if supersede_same_source is not None:
                self.conn.execute(
                    "UPDATE core.announcement_index SET superseded_at = %s,"
                    " source_note = %s WHERE index_id = %s",
                    (self._now(), "同来源补全文，由带全文的新行取代", supersede_same_source),
                )
                outcome = IndexOutcome.SUPERSEDED_DEGRADED
            elif match is not None:
                match_id, match_full_text = match
                if entry.has_full_text and not match_full_text:
                    # 主源到达：取代已有的降级行。
                    now = self._now()
                    self.conn.execute(
                        "UPDATE core.announcement_index SET superseded_at = %s,"
                        " source_note = %s WHERE index_id = %s",
                        (now, f"由 {entry.source}:{entry.source_ref} 取代", match_id),
                    )
                    outcome = IndexOutcome.SUPERSEDED_DEGRADED
                elif not entry.has_full_text and match_full_text:
                    # 降级行到达时主源已有：压制，不写新行，直接返回已有行的 id。
                    return IndexOutcome.SHADOWED, int(match_id)
                # 两边同为全文或同为降级：两条独立记录并存（不同来源各自的原始响应
                # 都值得保留），继续往下插入。

            row = self.conn.execute(
                "INSERT INTO core.announcement_index (entity_id, source, source_ref, title,"
                " title_norm, publish_date, url, excerpt, has_full_text, valid_from, known_at,"
                " snapshot_id, ingest_run_id) VALUES"
                " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING index_id",
                (
                    entity_id,
                    entry.source,
                    entry.source_ref,
                    entry.title,
                    title_norm,
                    entry.publish_date,
                    entry.url,
                    entry.excerpt,
                    entry.has_full_text,
                    entry.publish_date,
                    entry.known_at,
                    entry.snapshot_id,
                    self.ingest_run_id,
                ),
            ).fetchone()
            assert row is not None
            return outcome, int(row[0])

    def linked_doc_id(self, index_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT doc_id FROM core.announcement_index WHERE index_id = %s", (index_id,)
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    def link_document(self, index_id: int, doc_id: int) -> None:
        self.conn.execute(
            "UPDATE core.announcement_index SET doc_id = %s WHERE index_id = %s",
            (doc_id, index_id),
        )
