"""文档与块入库。

两条容易做错的地方：
1. doc_block 的反规范化列（entity_id / doc_type / publish_at / known_at / owner_*）
   必须与 document 完全一致——检索的过滤条件全落在它们身上（02 §5.3）。
2. 重解析沿用原文档的 known_at。取重解析时刻会让这份文档在历史回测中凭空消失
   （05 §7.2 第 2 步）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

from ragdemo.adapters.announcements import NormalizedDocument, known_at_for
from ragdemo.parse.chunker import Chunk
from ragdemo.parse.validate import validate_chunks


class MetadataInvalid(RuntimeError):
    """元数据校验未通过。整份文档不入库。"""


@dataclass(frozen=True)
class DocumentWriteResult:
    doc_id: int
    block_ids: list[int]
    skipped: bool


@dataclass(frozen=True)
class ParseArtifacts:
    """路径 B 的解析产物引用。路径 A（供应商结构化接口）没有这些，传 None。

    engine 形如 'textin:4.2.1+a3f19c02'——版本与参数指纹缺一不可，
    参数一改切块结果就变，而评测集的 gold_block_ids 绑在切块结果上
    （docs/05-document-pipeline.md §2.2）。
    """

    engine: str
    json_ref: str | None = None
    md_ref: str | None = None
    warnings: list[str] = field(default_factory=list)


class DocumentWriter:
    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        ingest_run_id: str,
        source: str,
        disclosure_lag: timedelta = timedelta(0),
    ) -> None:
        self.conn = conn
        self.ingest_run_id = ingest_run_id
        self.source = source
        self.disclosure_lag = disclosure_lag

    # --- 公开方法 ---------------------------------------------------------

    def write_document(
        self,
        doc: NormalizedDocument,
        chunks: list[Chunk],
        descriptions: Mapping[int, str],
        *,
        artifacts: ParseArtifacts | None = None,
    ) -> DocumentWriteResult:
        existing = self.conn.execute(
            "SELECT doc_id FROM core.document WHERE source = %s AND content_hash = %s",
            (self.source, doc.content_hash),
        ).fetchone()
        if existing is not None:
            return DocumentWriteResult(int(existing[0]), [], skipped=True)

        self._require_valid(doc, chunks, descriptions)
        with self.conn.transaction():
            doc_id = self._insert_document(
                doc, known_at=self._known_at(doc), artifacts=artifacts
            )
            self.conn.execute(
                "UPDATE core.document SET version_group_id = %s WHERE doc_id = %s",
                (doc_id, doc_id),
            )
            block_ids = self._insert_blocks(doc, doc_id, chunks, descriptions)
        return DocumentWriteResult(doc_id, block_ids, skipped=False)

    def reparse_document(
        self,
        doc: NormalizedDocument,
        chunks: list[Chunk],
        descriptions: Mapping[int, str],
        *,
        supersedes_doc_id: int,
        artifacts: ParseArtifacts | None = None,
    ) -> DocumentWriteResult:
        """升级解析器或换供应商后重新解析。新增版本，不原地替换。"""
        self._require_valid(doc, chunks, descriptions)
        row = self.conn.execute(
            "SELECT known_at, version_group_id FROM core.document WHERE doc_id = %s",
            (supersedes_doc_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"被取代的文档 {supersedes_doc_id} 不存在")
        original_known_at, version_group_id = row

        with self.conn.transaction():
            # 先标旧行 superseded_at，再插新行——document_dedup_uk 是只覆盖
            # superseded_at IS NULL 的部分唯一索引（008_document_dedup_partial.sql）。
            # 顺序反过来的话，新行插入那一刻旧行还「活」着，content_hash 相同，
            # 直接撞唯一索引：重解析永远走不通。
            self.conn.execute(
                "UPDATE core.document SET superseded_at = now() WHERE doc_id = %s",
                (supersedes_doc_id,),
            )
            self.conn.execute(
                "UPDATE core.doc_block SET superseded_at = now() WHERE doc_id = %s",
                (supersedes_doc_id,),
            )
            doc_id = self._insert_document(
                doc,
                known_at=original_known_at,
                version_group_id=int(version_group_id),
                supersedes_doc_id=supersedes_doc_id,
                artifacts=artifacts,
            )
            block_ids = self._insert_blocks(doc, doc_id, chunks, descriptions)
        return DocumentWriteResult(doc_id, block_ids, skipped=False)

    # --- 内部 -------------------------------------------------------------

    def _known_at(self, doc: NormalizedDocument) -> object:
        return known_at_for(doc.publish_at, self.disclosure_lag)

    def _require_valid(
        self,
        doc: NormalizedDocument,
        chunks: list[Chunk],
        descriptions: Mapping[int, str],
    ) -> None:
        violations = validate_chunks(doc, chunks, descriptions)
        if violations:
            raise MetadataInvalid(
                f"{doc.provider_doc_id} 元数据校验未通过（{len(violations)} 项）: "
                + "; ".join(violations[:5])
            )

    def _insert_document(
        self,
        doc: NormalizedDocument,
        *,
        known_at: object,
        version_group_id: int | None = None,
        supersedes_doc_id: int | None = None,
        artifacts: ParseArtifacts | None = None,
    ) -> int:
        entity_id = self._entity_id(doc.entity_ref)
        # 路径 A 走供应商结构化接口，没有解析产物；路径 B 三列都有（05 §2.4）。
        parse_engine = artifacts.engine if artifacts else f"vendor:{self.source}"
        json_ref = artifacts.json_ref if artifacts else None
        md_ref = artifacts.md_ref if artifacts else None
        warnings = Jsonb(artifacts.warnings if artifacts else [])
        row = self.conn.execute(
            "INSERT INTO core.document (entity_id, doc_type, title, period, publish_at,"
            " language, source, source_url, raw_ref, content_hash, version_group_id,"
            " is_correction, supersedes_doc_id, parse_engine, page_count, valid_from,"
            " known_at, source_ref, ingest_run_id,"
            " parse_json_ref, parse_md_ref, parse_warnings) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, 0),%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s) "
            "RETURNING doc_id",
            (
                entity_id, doc.doc_type, doc.title, doc.period, doc.publish_at,
                doc.language, self.source, doc.source_url, doc.raw_bytes_ref,
                doc.content_hash, version_group_id, doc.is_correction, supersedes_doc_id,
                parse_engine, doc.page_count, doc.publish_at.date(),
                known_at, doc.provider_doc_id, self.ingest_run_id,
                json_ref, md_ref, warnings,
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _insert_blocks(
        self,
        doc: NormalizedDocument,
        doc_id: int,
        chunks: list[Chunk],
        descriptions: Mapping[int, str],
    ) -> list[int]:
        entity_id = self._entity_id(doc.entity_ref)
        known_at, publish_at = self.conn.execute(
            "SELECT known_at, publish_at FROM core.document WHERE doc_id = %s", (doc_id,)
        ).fetchone()  # type: ignore[misc]

        ordinal_to_block_id: dict[int, int] = {}
        # 先插父块，再插叶子块，这样 parent_block_id 已经拿得到
        for chunk in sorted(chunks, key=lambda c: (c.parent_ordinal is not None, c.ordinal)):
            parent_block_id = (
                ordinal_to_block_id[chunk.parent_ordinal]
                if chunk.parent_ordinal is not None
                else None
            )
            row = self.conn.execute(
                "INSERT INTO core.doc_block (doc_id, parent_block_id, block_type,"
                " section_path, ordinal, page, bbox, content, content_desc, tokens,"
                " is_leaf, entity_id, doc_type, publish_at, valid_from, known_at,"
                " source, source_ref, ingest_run_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING block_id",
                (
                    doc_id, parent_block_id, chunk.block_type, chunk.section_path,
                    chunk.ordinal, chunk.page,
                    list(chunk.bbox) if chunk.bbox else None,
                    chunk.content, descriptions.get(chunk.ordinal),
                    len(chunk.content), chunk.is_leaf, entity_id, doc.doc_type,
                    publish_at, doc.publish_at.date(), known_at, self.source,
                    doc.provider_doc_id, self.ingest_run_id,
                ),
            ).fetchone()
            assert row is not None
            ordinal_to_block_id[chunk.ordinal] = int(row[0])

        return [ordinal_to_block_id[c.ordinal] for c in sorted(chunks, key=lambda c: c.ordinal)]

    def _entity_id(self, entity_ref: str | None) -> str | None:
        if entity_ref is None:
            return None
        row = self.conn.execute(
            "SELECT entity_id FROM core.entity WHERE tushare_code = %s"
            " OR ifind_code = %s OR wind_code = %s OR edgar_cik = %s",
            (entity_ref, entity_ref, entity_ref, entity_ref),
        ).fetchone()
        return str(row[0]) if row else None
