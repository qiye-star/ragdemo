"""旧文档缓存重解析：用**已缓存**的 TextIn JSON 与本地 PDF，把现行文档按新流程重写一遍。

新流程 = `TieredParser`（整份 L3、不启用任何减页规则、L0 永远关闭），因此块带 `parse_source`、
写逐页台账（`core.parse_page_route`）与文档验收结论（`core.parse_doc_acceptance`），表格带网格。

零外部调用、零付费：
- TextIn 的传输层是 `ReplayTransport`，任何真实请求都抛 `ReplayMiss`（缓存未命中 = 不能重解析）；
- blob 只读（`ReadOnlyBlob`），重解析不在 blob 里写任何东西；
- 不调 LLM：表格描述只从旧的现行块沿用（内容完全相同才沿用），沿用不了的整份文档跳过并列出；
- 不调嵌入：嵌入输入（`embed/base.py::embedding_input`）逐字节相同的叶子块直接沿用旧向量，
  其余留空待嵌入。

时点：经 `DocumentWriter.reparse_document` 写入——旧文档 / 块 / 网格标 `superseded_at`（不删），
新块沿用旧文档的 `known_at`（发布时刻），不用 `ingested_at`。

缓存或 PDF 缺失的文档不能重解析：列出原因，原样不动。
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import httpx
import psycopg

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.embed.base import embedding_input
from ragdemo.ingest.assets_docs import PreparedDocument, record_parse_snapshots
from ragdemo.ingest.documents import DocumentWriter, ParseArtifacts
from ragdemo.ingest.parse_pending import (
    ParseInput,
    _entity_ref_for,
    _PendingDocument,
    resolve_parse_input,
)
from ragdemo.ingest.render_html import is_html_ref
from ragdemo.parse.chunker import Chunk, chunk_document
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.textin import ParseResult, TextInParser, artifact_keys
from ragdemo.parse.tiered.census import ALWAYS_L3_DOC_TYPES
from ragdemo.parse.tiered.parser import TieredParser
from ragdemo.parse.tree import build_tree
from ragdemo.parse.validate import validate_chunks
from ragdemo.tables.backfill import desc_provenance_of
from ragdemo_core.blob import BlobNotFound, BlobStore

_LOG = logging.getLogger(__name__)

REPLAY_BASE_URL: Final = "http://replay.invalid"
# TieredParser 只接受这两类进入分页路由；定期报告走 parse_periodic，
# 其余类型（如 EDGAR 8-K）按公告路由。
ROUTED_DOC_TYPES: Final = frozenset({"announcement", "research_report"})
_MISSING_DESC_SUFFIX: Final = "表格块缺少 content_desc"


class ReplayMiss(RuntimeError):
    """重放传输层收到了真实请求：缓存没命中。重解析必须零外部调用，宁可失败。"""


class ReplayTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise ReplayMiss(f"重放模式拒绝真实请求：{request.method} {request.url.path}")


class ReadOnlyBlob:
    """只读包装：重解析只读缓存与原文，任何写入都是缺陷。"""

    def __init__(self, inner: BlobStore) -> None:
        self._inner = inner

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def put(self, key: str, data: bytes) -> str:
        raise PermissionError(f"缓存重解析不写 blob（拒绝写入 {key}）")


@dataclass(frozen=True)
class OldBlock:
    block_id: int
    ordinal: int
    block_type: str
    section_path: str
    page: int | None
    content: str
    content_desc: str | None
    is_leaf: bool
    has_embedding: bool
    desc_model: str | None
    desc_prompt_version: str | None
    embedding_version: str | None = None


@dataclass(frozen=True)
class Candidate:
    row: _PendingDocument
    is_correction: bool
    parse_in: ParseInput
    json_key: str


@dataclass(frozen=True)
class NotReparseable:
    doc_id: int
    doc_type: str
    title: str
    reason: str


@dataclass
class DocOutcome:
    doc_id: int
    doc_type: str
    title: str
    status: str  # dry_run / written / skipped_desc / validation_failed / failed
    old_blocks: int = 0
    new_blocks: int = 0
    identical_blocks: int = 0
    old_tables: int = 0
    new_tables: int = 0
    desc_carried: int = 0
    desc_missing: list[int] = field(default_factory=list)
    state: str | None = None
    new_doc_id: int | None = None
    embed_carried: int = 0
    embed_needed: int = 0
    pages: int = 0
    error: str = ""


@dataclass
class ReparseReport:
    not_reparseable: list[NotReparseable] = field(default_factory=list)
    outcomes: list[DocOutcome] = field(default_factory=list)
    replay_calls: int = 0


_SELECT = (
    "SELECT d.doc_id, d.entity_id, d.source, d.source_ref, d.doc_type, d.title,"
    "       d.period, d.publish_at, d.language, d.source_url, d.raw_ref,"
    "       d.content_hash, d.snapshot_id, d.parse_warnings, d.is_correction, d.owner_user,"
    # 已在新流程上：现行块全部带 parse_source，且该解析产物已有文档验收结论（再跑一遍只会多一版）
    "       NOT EXISTS (SELECT 1 FROM core.doc_block b WHERE b.doc_id = d.doc_id"
    "                    AND b.superseded_at IS NULL AND b.parse_source IS NULL)"
    "       AND EXISTS (SELECT 1 FROM core.parse_doc_acceptance a"
    "                    WHERE a.routes_ref = d.parse_json_ref)"
    "  FROM core.document d"
    " WHERE d.superseded_at IS NULL"
    "   AND (%s::bigint[] IS NULL OR d.doc_id = ANY(%s))"
    "   AND EXISTS (SELECT 1 FROM core.doc_block b"
    "                WHERE b.doc_id = d.doc_id AND b.superseded_at IS NULL)"
    " ORDER BY d.doc_id"
)


def select_documents(
    conn: psycopg.Connection, blob: BlobStore, *, doc_ids: Sequence[int] | None
) -> tuple[list[Candidate], list[NotReparseable]]:
    """现行、已有现行块的文档里，能从缓存免费重解析的挑出来；不能的给出原因。"""
    ids = list(doc_ids) if doc_ids is not None else None
    out: list[Candidate] = []
    skipped: list[NotReparseable] = []
    for r in conn.execute(_SELECT, (ids, ids)).fetchall():
        row = _PendingDocument(
            doc_id=int(r[0]),
            entity_id=r[1],
            source=str(r[2]),
            source_ref=r[3],
            doc_type=str(r[4]),
            title=str(r[5]),
            period=r[6],
            publish_at=r[7],
            language=str(r[8]),
            source_url=r[9],
            raw_ref=r[10],
            content_hash=str(r[11]),
            snapshot_id=r[12],
            parse_warnings=tuple(str(w) for w in (r[13] or [])),
        )

        def no(reason: str, row: _PendingDocument = row) -> None:
            skipped.append(NotReparseable(row.doc_id, row.doc_type, row.title, reason))

        if r[15] is not None:
            no("private_document")
            continue
        if r[16]:
            no("already_new_flow")
            continue
        if row.raw_ref is None:
            no("no_raw_ref")
            continue
        parse_in = resolve_parse_input(conn, blob, row, strict=False)
        if is_html_ref(parse_in.raw_ref):
            no("derived_pdf_missing")
            continue
        try:
            raw = blob.get(parse_in.raw_ref)
        except BlobNotFound:
            no("raw_missing")
            continue
        if not raw.startswith(b"%PDF"):
            no("raw_not_pdf")
            continue
        json_key, _ = artifact_keys(TextInParser.content_hash(raw), _param_fp())
        if not blob.exists(json_key):
            no("textin_cache_missing")
            continue
        out.append(Candidate(row, bool(r[14]), parse_in, json_key))
    return out, skipped


def _param_fp() -> str:
    from ragdemo.parse.textin import XPARSE_PARAMS, param_fingerprint

    return param_fingerprint(XPARSE_PARAMS)


def replay_parser(blob: BlobStore) -> tuple[TieredParser, ReplayTransport]:
    transport = ReplayTransport()
    textin = TextInParser(REPLAY_BASE_URL, blob, transport=transport)
    parser = TieredParser(
        textin,
        blob,
        doc_types=ROUTED_DOC_TYPES,
        enforced_skips=frozenset(),
        textin_fp=textin.param_fp,
    )
    return parser, transport


def parse_cached(parser: TieredParser, raw: bytes, *, doc_type: str, title: str) -> ParseResult:
    if doc_type in ALWAYS_L3_DOC_TYPES:
        return parser.parse_periodic(raw)
    routed = doc_type if doc_type in ROUTED_DOC_TYPES else "announcement"
    return parser.parse_for(raw, doc_type=routed, title=title)


def load_old_blocks(conn: psycopg.Connection, doc_id: int) -> list[OldBlock]:
    rows = conn.execute(
        "SELECT b.block_id, b.ordinal, b.block_type::text, b.section_path, b.page, b.content,"
        "       b.content_desc, b.is_leaf, b.embedding IS NOT NULL, t.desc_model,"
        "       t.desc_prompt_version, b.embedding_version"
        "  FROM core.doc_block b"
        "  LEFT JOIN core.doc_table t ON t.block_id = b.block_id AND t.superseded_at IS NULL"
        " WHERE b.doc_id = %s AND b.superseded_at IS NULL ORDER BY b.ordinal",
        (doc_id,),
    ).fetchall()
    return [
        OldBlock(
            int(r[0]),
            int(r[1]),
            str(r[2]),
            str(r[3] or ""),
            r[4],
            str(r[5]),
            r[6],
            bool(r[7]),
            bool(r[8]),
            r[9],
            r[10],
            r[11],
        )
        for r in rows
    ]


def carry_table_descriptions(
    chunks: Sequence[Chunk], old_blocks: Sequence[OldBlock]
) -> tuple[dict[int, str], dict[int, tuple[str, str]], list[int]]:
    """新表格块与旧现行表格块内容逐字相同才沿用描述（及其来源）；一对一消耗，优先同页。

    返回 (ordinal -> 描述, ordinal -> (模型, prompt 版本), 沿用不了的 ordinal)。"""
    pool = [b for b in old_blocks if b.block_type == "table" and (b.content_desc or "").strip()]
    used: set[int] = set()
    desc: dict[int, str] = {}
    prov: dict[int, tuple[str, str]] = {}
    missing: list[int] = []
    for c in sorted((c for c in chunks if c.block_type == "table"), key=lambda c: c.ordinal):
        same = [b for b in pool if b.block_id not in used and b.content == c.content]
        if not same:
            missing.append(c.ordinal)
            continue
        best = min(same, key=lambda b: (b.page != c.page, abs(b.ordinal - c.ordinal)))
        used.add(best.block_id)
        assert best.content_desc is not None
        desc[c.ordinal] = best.content_desc
        if best.desc_model is not None and best.desc_prompt_version is not None:
            prov[c.ordinal] = (best.desc_model, best.desc_prompt_version)
        else:
            guessed = desc_provenance_of(best.content_desc)
            if guessed is not None:
                prov[c.ordinal] = guessed
    return desc, prov, missing


def _block_key(block_type: str, content: str, page: int | None, is_leaf: bool) -> tuple[Any, ...]:
    return (block_type, content, page, is_leaf)


def _identical(chunks: Sequence[Chunk], old: Sequence[OldBlock]) -> int:
    a = Counter(_block_key(c.block_type, c.content, c.page, c.is_leaf) for c in chunks)
    b = Counter(_block_key(o.block_type, o.content, o.page, o.is_leaf) for o in old)
    return sum((a & b).values())


def carry_embeddings(
    conn: psycopg.Connection, *, new_doc_id: int, title: str, old_blocks: Sequence[OldBlock]
) -> tuple[int, int]:
    """嵌入输入逐字节相同的新叶子块直接复制旧向量（同一模型版本）；返回 (沿用, 仍需嵌入)。"""
    old_by_input: dict[str, int] = {}
    for o in old_blocks:
        if o.is_leaf and o.has_embedding:
            key = embedding_input(
                doc_title=title,
                section_path=o.section_path,
                content_desc=o.content_desc or "",
                content=o.content,
            )
            old_by_input.setdefault(key, o.block_id)
    rows = conn.execute(
        "SELECT block_id, section_path, content_desc, content FROM core.doc_block"
        " WHERE doc_id = %s AND is_leaf AND superseded_at IS NULL AND embedding IS NULL",
        (new_doc_id,),
    ).fetchall()
    carried = 0
    for block_id, section_path, content_desc, content in rows:
        key = embedding_input(
            doc_title=title,
            section_path=str(section_path or ""),
            content_desc=str(content_desc or ""),
            content=str(content),
        )
        src = old_by_input.get(key)
        if src is None:
            continue
        conn.execute(
            "UPDATE core.doc_block n SET embedding = o.embedding,"
            " embedding_version = o.embedding_version"
            "  FROM core.doc_block o WHERE n.block_id = %s AND o.block_id = %s",
            (block_id, src),
        )
        carried += 1
    conn.commit()
    return carried, len(rows) - carried


def _normalized(c: Candidate, conn: psycopg.Connection, result: ParseResult) -> NormalizedDocument:
    row = c.row
    return NormalizedDocument(
        provider_doc_id=row.source_ref or str(row.doc_id),
        entity_ref=_entity_ref_for(conn, row.entity_id) if row.entity_id else None,
        doc_type=row.doc_type,
        title=row.title,
        period=row.period,
        publish_at=row.publish_at,  # type: ignore[arg-type]
        language=row.language,
        source_url=row.source_url,
        raw_bytes_ref=c.parse_in.raw_ref,
        content_hash=row.content_hash,
        is_correction=c.is_correction,
        supersedes_provider_doc_id=None,
        page_count=result.page_count,
        blocks=list(result.blocks),
        snapshot_id=c.parse_in.snapshot_id,
    )


def _log(event: str, *, run_id: str, entity_id: str | None, **fields: object) -> None:
    _LOG.info(event, extra={"run_id": run_id, "as_of": None, "entity_id": entity_id, **fields})


def reparse_one(
    conn: psycopg.Connection,
    blob: BlobStore,
    parser: TieredParser,
    c: Candidate,
    *,
    write: bool,
    ingest_run_id: str,
    chunk_cfg: ChunkConfig,
    cost_per_page_cny: Decimal | None,
) -> DocOutcome:
    row = c.row
    out = DocOutcome(row.doc_id, row.doc_type, row.title, "dry_run")
    raw = blob.get(c.parse_in.raw_ref)
    result = parse_cached(parser, raw, doc_type=row.doc_type, title=row.title)
    if result.json_ref != c.json_key:
        raise RuntimeError(f"解析结果引用 {result.json_ref} 不是预期的缓存 {c.json_key}")
    doc = _normalized(c, conn, result)
    chunks = build_tree(chunk_document(doc, chunk_cfg))
    old = load_old_blocks(conn, row.doc_id)
    descriptions, provenance, missing = carry_table_descriptions(chunks, old)
    out.old_blocks, out.new_blocks = len(old), len(chunks)
    out.identical_blocks = _identical(chunks, old)
    out.old_tables = sum(1 for o in old if o.block_type == "table")
    out.new_tables = sum(1 for ch in chunks if ch.block_type == "table")
    out.desc_carried, out.desc_missing = len(descriptions), missing
    out.state = result.ledger.doc.state if result.ledger is not None else None
    out.pages = result.page_count
    violations = [
        v
        for v in validate_chunks(doc, chunks, descriptions)
        if not v.endswith(_MISSING_DESC_SUFFIX)
    ]
    if missing:
        # 新表格内容与旧现行块不同：描述只能重新生成（LLM，付费）——本命令不做，整份不写，列出待定。
        out.status = "skipped_desc"
        return out
    if violations:
        out.status, out.error = "validation_failed", "; ".join(violations[:5])
        return out
    if not write:
        return out

    artifacts = ParseArtifacts(
        engine=result.engine_version,
        json_ref=result.json_ref,
        md_ref=result.md_ref,
        warnings=[*result.warnings, *c.parse_in.markers],
    )
    writer = DocumentWriter(conn, ingest_run_id=ingest_run_id, source=row.source, blob=blob)
    written = writer.reparse_document(
        doc,
        chunks,
        descriptions,
        supersedes_doc_id=row.doc_id,
        artifacts=artifacts,
        chunking_version=chunk_cfg.version,
        desc_provenance=None,
        desc_provenance_by_ordinal=provenance,
    )
    out.new_doc_id = written.doc_id
    conn.execute(
        "UPDATE core.announcement_index SET doc_id = %s WHERE doc_id = %s",
        (written.doc_id, row.doc_id),
    )
    conn.commit()
    # 台账（缓存命中，billed 0：不留 provider_snapshot，只写逐页台账与文档结论，及无输出告警）
    record_parse_snapshots(
        conn,
        [
            PreparedDocument(
                doc,
                artifacts,
                billed_pages=0,
                textin_calls=result.textin_calls,
                ledger=result.ledger,
            )
        ],
        ingest_run_id=ingest_run_id,
        cost_per_page_cny=cost_per_page_cny,
    )
    out.embed_carried, out.embed_needed = carry_embeddings(
        conn, new_doc_id=written.doc_id, title=row.title, old_blocks=old
    )
    out.status = "written"
    return out


def run_reparse_cached(
    conn: psycopg.Connection,
    blob: BlobStore,
    *,
    doc_ids: Sequence[int] | None,
    write: bool,
    ingest_run_id: str,
    cost_per_page_cny: Decimal | None = None,
    chunk_cfg: ChunkConfig | None = None,
) -> ReparseReport:
    ro = ReadOnlyBlob(blob)
    cfg = chunk_cfg or ChunkConfig()
    candidates, skipped = select_documents(conn, ro, doc_ids=doc_ids)
    report = ReparseReport(not_reparseable=skipped)
    parser, transport = replay_parser(ro)
    for c in candidates:
        try:
            outcome = reparse_one(
                conn,
                ro,
                parser,
                c,
                write=write,
                ingest_run_id=ingest_run_id,
                chunk_cfg=cfg,
                cost_per_page_cny=cost_per_page_cny,
            )
        except Exception as exc:  # 单份失败不拖垮其余文档；原文档原样保留
            conn.rollback()
            outcome = DocOutcome(
                c.row.doc_id,
                c.row.doc_type,
                c.row.title,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        report.outcomes.append(outcome)
        _log(
            "reparse_cached.doc",
            run_id=ingest_run_id,
            entity_id=c.row.entity_id,
            doc_id=c.row.doc_id,
            status=outcome.status,
            old_blocks=outcome.old_blocks,
            new_blocks=outcome.new_blocks,
            state=outcome.state,
        )
    report.replay_calls = transport.calls
    return report


REPORT_COLUMNS: Final = (
    "doc_id",
    "doc_type",
    "status",
    "new_doc_id",
    "pages",
    "old_blocks",
    "new_blocks",
    "identical_blocks",
    "old_tables",
    "new_tables",
    "desc_carried",
    "desc_missing",
    "state",
    "embed_carried",
    "embed_needed",
    "error",
    "title",
)


def report_rows(report: ReparseReport) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for o in report.outcomes:
        rows.append(
            {
                "doc_id": o.doc_id,
                "doc_type": o.doc_type,
                "status": o.status,
                "new_doc_id": o.new_doc_id or "",
                "pages": o.pages,
                "old_blocks": o.old_blocks,
                "new_blocks": o.new_blocks,
                "identical_blocks": o.identical_blocks,
                "old_tables": o.old_tables,
                "new_tables": o.new_tables,
                "desc_carried": o.desc_carried,
                "desc_missing": ",".join(map(str, o.desc_missing)),
                "state": o.state or "",
                "embed_carried": o.embed_carried,
                "embed_needed": o.embed_needed,
                "error": o.error,
                "title": o.title,
            }
        )
    for n in report.not_reparseable:
        rows.append(
            {
                **dict.fromkeys(REPORT_COLUMNS, ""),
                "doc_id": n.doc_id,
                "doc_type": n.doc_type,
                "status": f"not_reparseable:{n.reason}",
                "title": n.title,
            }
        )
    return rows
