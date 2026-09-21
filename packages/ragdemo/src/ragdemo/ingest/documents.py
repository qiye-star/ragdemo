"""文档与块入库。

两条容易做错的地方：
1. doc_block 的反规范化列（entity_id / doc_type / publish_at / known_at / owner_*）
   必须与 document 完全一致——检索的过滤条件全落在它们身上（02 §5.3）。
2. 重解析沿用原文档的 known_at。取重解析时刻会让这份文档在历史回测中凭空消失
   （05 §7.2 第 2 步）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

from ragdemo.adapters.announcements import NormalizedDocument, known_at_for
from ragdemo.parse.chunker import Chunk
from ragdemo.parse.validate import validate_chunks

logger = logging.getLogger(__name__)


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

    engine 形如 'textin:4.2.1+a3f19c02d4e17b5f'——版本与参数指纹缺一不可，
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
        owner_user: str | None = None,
        artifacts: ParseArtifacts | None = None,
    ) -> DocumentWriteResult:
        """`owner_user` 为 None（缺省）时写公共行——与改动前的行为完全一致。

        非 None 时这份文档与它的全部块都落进用户私有空间：
        db/migrations/006_asof_views_and_roles.sql 的 doc_visibility /
        block_visibility 策略把 owner_user IS NULL 当作"公共"，这里不写这一
        列的话，TEXTIN_ALLOW_PRIVATE 一旦打开，私有材料就会被悄悄存成公共行
        ——正是 CLAUDE.md §0"用户上传材料私有隔离"要防的事。`owner_tenant`
        不在这次修的范围内：现有代码库里没有任何地方产出租户级别的归属
        （embed / retrieval 也只认 owner_user），引入它需要的是 P4 才该做的
        租户模型设计决定，这里保持它恒为 NULL。
        """
        existing = self._live_doc_id(doc.content_hash)
        if existing is not None:
            return DocumentWriteResult(existing, [], skipped=True)

        self._require_valid(doc, chunks, descriptions)
        entity_id = self._resolve_entity_id(doc.entity_ref)
        try:
            with self.conn.transaction():
                doc_id = self._insert_document(
                    doc,
                    known_at=self._known_at(doc),
                    entity_id=entity_id,
                    artifacts=artifacts,
                    owner_user=owner_user,
                )
                self.conn.execute(
                    "UPDATE core.document SET version_group_id = %s WHERE doc_id = %s",
                    (doc_id, doc_id),
                )
                block_ids = self._insert_blocks(doc, doc_id, entity_id, chunks, descriptions)
        except psycopg.errors.UniqueViolation:
            # 两份从未见过的文档并发写入：两边都在各自的存在性检查里判定"不存在"，
            # 然后都去插入——分区唯一索引 document_dedup_uk（source, content_hash
            # WHERE superseded_at IS NULL，008_document_dedup_partial.sql）保证数据
            # 不会重复，但落败的一方原本会拿到未处理的 UniqueViolation 直接崩溃。
            # CLAUDE.md §1.6 的增量触发模型下，同一份公告被并发摄入是正常路径
            # （多个 partition run 撞上同一条新公告），不该让整个 run 因此失败——
            # 降级为 skipped，doc_id 指向赢家刚提交的活跃行。
            #
            # 这里选择"捕获 UniqueViolation 再重新查询"而不是给 INSERT 加
            # ON CONFLICT ... DO NOTHING：_insert_document 是 reparse_document
            # 共用的内部方法，给它的 SQL 加 DO NOTHING 会让 reparse 路径也悄悄
            # 开始允许插入返回空结果，那条路径目前没有、也不需要处理"跳过"的
            # 语义（重解析冲突是完全不同的场景，不在本次修复范围内）。把处理
            # 范围收在 write_document 自己的 try/except 里，改动面更小。
            live_id = self._live_doc_id(doc.content_hash)
            if live_id is None:
                raise  # 不是预期中的那种冲突，原样抛出而不是吞掉未知错误
            return DocumentWriteResult(live_id, [], skipped=True)
        # `with self.conn.transaction()` 只是 SAVEPOINT——self._live_doc_id
        # 顶上那次 SELECT 已经在这个连接上隐式开了外层事务（ragdemo_core/
        # db/migrate.py:61-64 记录过同一个坑，embed/batch.py 的按批提交是
        # 同一个修复）。没有这个显式 commit()，没有任何调用方会替它提交：
        # doc_blocks_loaded 资产循环调 write_document 但从不 commit，
        # definitions.py 目前也没有接这几个资产；测试之所以"看起来通过"，
        # 是因为断言都在写入用的同一个连接上读——同一事务内自己能看见自己
        # 未提交的写入，换一个连接就什么都看不到，进程一崩溃就真的全丢。
        self.conn.commit()
        return DocumentWriteResult(doc_id, block_ids, skipped=False)

    def _live_doc_id(self, content_hash: str) -> int | None:
        """只看"活着"的行——与 document_dedup_uk 的部分唯一索引谓词一致。

        重解析后同一个 (source, content_hash) 会有两行：被取代的旧行与新的活跃行。
        不加 superseded_at IS NULL 的话，这里可能把旧行的 doc_id 当作"已存在，
        跳过"返回给调用方——那个 doc_id 在 asof.document 里不可见，调用方一旦
        信了 DocumentWriteResult.doc_id 就会引用一份死文档。
        """
        row = self.conn.execute(
            "SELECT doc_id FROM core.document WHERE source = %s AND content_hash = %s"
            " AND superseded_at IS NULL",
            (self.source, content_hash),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def reparse_document(
        self,
        doc: NormalizedDocument,
        chunks: list[Chunk],
        descriptions: Mapping[int, str],
        *,
        supersedes_doc_id: int,
        artifacts: ParseArtifacts | None = None,
    ) -> DocumentWriteResult:
        """升级解析器或换供应商后重新解析。新增版本，不原地替换。

        归属沿用被取代的那份文档——重解析是同一份文档换了个解析结果，不是
        换了归属；这里不接受调用方传 owner_user，就像 known_at 不接受调用方
        传一样（改了归属会让这份文档从它原本该属于的私有空间里消失，
        或者反过来混进公共空间）。
        """
        self._require_valid(doc, chunks, descriptions)
        row = self.conn.execute(
            "SELECT known_at, version_group_id, owner_user FROM core.document WHERE doc_id = %s",
            (supersedes_doc_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"被取代的文档 {supersedes_doc_id} 不存在")
        original_known_at, version_group_id, original_owner_user = row
        entity_id = self._resolve_entity_id(doc.entity_ref)

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
                entity_id=entity_id,
                version_group_id=int(version_group_id),
                supersedes_doc_id=supersedes_doc_id,
                artifacts=artifacts,
                owner_user=original_owner_user,
            )
            block_ids = self._insert_blocks(doc, doc_id, entity_id, chunks, descriptions)
        self.conn.commit()  # 理由见 write_document 里同样这一行上面的注释。
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
        entity_id: str | None,
        version_group_id: int | None = None,
        supersedes_doc_id: int | None = None,
        artifacts: ParseArtifacts | None = None,
        owner_user: str | None = None,
    ) -> int:
        # 路径 A 走供应商结构化接口，没有解析产物；路径 B 三列都有（05 §2.4）。
        parse_engine = artifacts.engine if artifacts else f"vendor:{self.source}"
        json_ref = artifacts.json_ref if artifacts else None
        md_ref = artifacts.md_ref if artifacts else None
        warnings = Jsonb(artifacts.warnings if artifacts else [])
        row = self.conn.execute(
            "INSERT INTO core.document (entity_id, doc_type, title, period, publish_at,"
            " language, source, source_url, raw_ref, content_hash, version_group_id,"
            " is_correction, supersedes_doc_id, parse_engine, page_count, owner_user,"
            " valid_from, known_at, source_ref, ingest_run_id,"
            " parse_json_ref, parse_md_ref, parse_warnings) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, 0),%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s) "
            "RETURNING doc_id",
            (
                entity_id,
                doc.doc_type,
                doc.title,
                doc.period,
                doc.publish_at,
                doc.language,
                self.source,
                doc.source_url,
                doc.raw_bytes_ref,
                doc.content_hash,
                version_group_id,
                doc.is_correction,
                supersedes_doc_id,
                parse_engine,
                doc.page_count,
                owner_user,
                doc.publish_at.date(),
                known_at,
                doc.provider_doc_id,
                self.ingest_run_id,
                json_ref,
                md_ref,
                warnings,
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _insert_blocks(
        self,
        doc: NormalizedDocument,
        doc_id: int,
        entity_id: str | None,
        chunks: list[Chunk],
        descriptions: Mapping[int, str],
    ) -> list[int]:
        # owner_user 从刚插入的 document 行读回，而不是让调用方另传一份——
        # 与 known_at / publish_at 同样的道理（本方法原有的反规范化列），
        # 单一数据源保证 doc_block.owner_user 不可能与 document.owner_user
        # 打架（02 §5.3：block 的反规范化列必须与 document 完全一致）。
        known_at, publish_at, owner_user = self.conn.execute(
            "SELECT known_at, publish_at, owner_user FROM core.document WHERE doc_id = %s",
            (doc_id,),
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
                " is_leaf, entity_id, doc_type, publish_at, owner_user, valid_from, known_at,"
                " source, source_ref, ingest_run_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING block_id",
                (
                    doc_id,
                    parent_block_id,
                    chunk.block_type,
                    chunk.section_path,
                    chunk.ordinal,
                    chunk.page,
                    list(chunk.bbox) if chunk.bbox else None,
                    chunk.content,
                    descriptions.get(chunk.ordinal),
                    len(chunk.content),
                    chunk.is_leaf,
                    entity_id,
                    doc.doc_type,
                    publish_at,
                    owner_user,
                    doc.publish_at.date(),
                    known_at,
                    self.source,
                    doc.provider_doc_id,
                    self.ingest_run_id,
                ),
            ).fetchone()
            assert row is not None
            ordinal_to_block_id[chunk.ordinal] = int(row[0])

        return [ordinal_to_block_id[c.ordinal] for c in sorted(chunks, key=lambda c: c.ordinal)]

    def _resolve_entity_id(self, entity_ref: str | None) -> str | None:
        """entity_ref 为 None 是政策/宏观文档的正常情况（004_documents.sql:6）；
        entity_ref 非 None 但查不到匹配行，是两种情况都会落到的同一个返回值
        （None），但含义完全不同——后者是数据质量问题，不该跟前者一样悄无声息。

        这里的处理只是"写警告日志，照常入库"：拒绝写入会在新上市实体还没
        回填 core.entity 代码列时挡住它全部的公告（CLAUDE.md §1.6 的增量触发
        模型下这是常态，不是例外），比"文档暂时没有 entity_id"更糟。
        长期正确的做法是把它送进 core.entity_resolution_queue——
        entities/resolver.py 的 EntityResolver.enqueue_unresolved 已经有现成的
        入队方法——但那条队列、去重、人工处理流程的归属是实体解析模块，
        接线方式（在这里同步入队，还是让下游按 entity_ref 补扫）是需要那边
        决定的设计选择，本次修复不在这里替它做主。
        """
        if entity_ref is None:
            return None
        row = self.conn.execute(
            "SELECT entity_id FROM core.entity WHERE tushare_code = %s"
            " OR ifind_code = %s OR wind_code = %s OR edgar_cik = %s",
            (entity_ref, entity_ref, entity_ref, entity_ref),
        ).fetchone()
        if row is None:
            logger.warning(
                "document entity_ref did not resolve to any core.entity row",
                extra={"ingest_run_id": self.ingest_run_id, "entity_ref": entity_ref},
            )
            return None
        return str(row[0])
