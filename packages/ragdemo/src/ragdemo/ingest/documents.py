"""文档与块入库。

三条容易做错的地方：
1. doc_block 的反规范化列（entity_id / doc_type / publish_at / known_at / owner_* /
   can_show_raw）必须与 document 完全一致——检索的过滤条件全落在它们身上（02 §5.3）。
2. 重解析沿用原文档的 known_at。取重解析时刻会让这份文档在历史回测中凭空消失
   （05 §7.2 第 2 步）。
3. 四条款（can_cache / can_show_raw / can_vectorize / time_precision）只能来自
   core.source_registry，不接受调用方传参覆盖——否则合同条款与代码行为随时可能
   对不上（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 B）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import psycopg
from psycopg.types.json import Jsonb

from ragdemo.adapters.announcements import NormalizedDocument, known_at_for
from ragdemo.parse.chunker import Chunk
from ragdemo.parse.confidence import score_document, score_pages
from ragdemo.parse.validate import validate_chunks

logger = logging.getLogger(__name__)


class MetadataInvalid(RuntimeError):
    """元数据校验未通过。整份文档不入库。"""


class SourceNotRegistered(RuntimeError):
    """`source` 在 `core.source_registry` 里没有登记。

    四条款（能否缓存/能否展示原文/能否向量化/发布时间精度）没有默认值可猜——
    猜错任何一条都是合规问题，不是工程小瑕疵。新接入一个来源必须先跑一条迁移
    或运维脚本登记它，而不是让 DocumentWriter 悄悄假设一个"看起来安全"的默认值。
    """


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
        self.can_show_raw, self.time_precision = self._load_source_registry(source)

    def _load_source_registry(self, source: str) -> tuple[bool, str]:
        row = self.conn.execute(
            "SELECT can_show_raw, time_precision FROM core.source_registry WHERE source_id = %s",
            (source,),
        ).fetchone()
        if row is None:
            raise SourceNotRegistered(
                f"来源 {source!r} 没有在 core.source_registry 登记"
                "（四条款：能否缓存/能否展示原文/能否向量化/发布时间精度）。"
                "先跑一条迁移登记它，再重试。"
            )
        return bool(row[0]), str(row[1])

    # --- 公开方法 ---------------------------------------------------------

    def write_document(
        self,
        doc: NormalizedDocument,
        chunks: list[Chunk],
        descriptions: Mapping[int, str],
        *,
        owner_user: str | None = None,
        artifacts: ParseArtifacts | None = None,
        chunking_version: str | None = None,
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

        `chunking_version` 由调用方传入（通常是 `ChunkConfig.version`）：
        `DocumentWriter` 只拿到已经切好的 `chunks`，不知道切它们用的是哪个
        `ChunkConfig` 实例，这个值只有调用方知道。缺省 None——不强迫每个
        既有调用点都跟着改。
        """
        existing = self._live_doc_id(doc.content_hash)
        if existing is not None:
            return DocumentWriteResult(existing, [], skipped=True)

        if doc.is_correction and doc.supersedes_provider_doc_id is not None:
            # 阶段 F（F3）：供应商把同一份材料改一个标点重发，content_hash 会变，
            # 上面那次按 content_hash 的存在性检查因此找不到旧行，若不做这一步
            # 会被当成一份全新、与旧文档毫无关联的文档插入——version_group_id
            # 各自独立，旧行也不会被标 superseded_at，asof 视图会同时展示两份
            # "看起来不相关"的文档。NormalizedDocument 早就带着 is_correction /
            # supersedes_provider_doc_id 这两个字段（normalize() 产出时由供应商
            # 告知），只是在这次修复之前 DocumentWriter 完全没有读过它们。
            #
            # 按 provider_doc_id（存成 core.document.source_ref）找活着的前序
            # 文档：找不到就说明前序还没入库（比如跨分区乱序到达，或前序被
            # 这次改动之前的旧代码写漏了）——落回当作全新文档处理，不能因为
            # "自称是更正"就抛错阻断整个 run（CLAUDE.md §1.6 的增量触发模型下，
            # 乱序到达是常态）。
            predecessor_id = self._live_doc_id_by_provider_id(doc.supersedes_provider_doc_id)
            if predecessor_id is not None:
                row = self.conn.execute(
                    "SELECT version_group_id FROM core.document WHERE doc_id = %s",
                    (predecessor_id,),
                ).fetchone()
                assert row is not None
                (predecessor_version_group_id,) = row
                self._require_valid(doc, chunks, descriptions)
                entity_id = self._resolve_entity_id(doc.entity_ref)
                return self._write_new_version(
                    doc,
                    entity_id,
                    chunks,
                    descriptions,
                    supersedes_doc_id=predecessor_id,
                    known_at=self._known_at(doc),
                    version_group_id=int(predecessor_version_group_id),
                    # 用调用方这次传入的 owner_user，不是前序文档存的那个——
                    # 归属由这次调用的调用方决定（与非更正路径的规则一致），
                    # 不能被前序文档的归属悄悄覆盖。混淆这两者曾经真实地把
                    # 一份该私有的更正文档写成了公共行（RLS 隔离测试抓到的
                    # 回归，见 tests/ingest/test_documents.py 对应用例）。
                    owner_user=owner_user,
                    artifacts=artifacts,
                    chunking_version=chunking_version,
                )

        self._require_valid(doc, chunks, descriptions)
        entity_id = self._resolve_entity_id(doc.entity_ref)
        doc_confidence = score_document(doc)
        page_confidence = score_pages(doc)
        try:
            with self.conn.transaction():
                doc_id = self._insert_document(
                    doc,
                    known_at=self._known_at(doc),
                    entity_id=entity_id,
                    artifacts=artifacts,
                    owner_user=owner_user,
                    parse_confidence=doc_confidence,
                )
                self.conn.execute(
                    "UPDATE core.document SET version_group_id = %s WHERE doc_id = %s",
                    (doc_id, doc_id),
                )
                block_ids = self._insert_blocks(
                    doc,
                    doc_id,
                    entity_id,
                    chunks,
                    descriptions,
                    page_confidence=page_confidence,
                    doc_confidence=doc_confidence,
                    chunking_version=chunking_version,
                )
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

    def _live_doc_id_by_provider_id(self, provider_doc_id: str) -> int | None:
        """按供应商侧文档 ID（存成 source_ref）找活着的行——F3 更正路由用它把
        "改标点重发"关联回原文档，而不是只看 content_hash。"""
        row = self.conn.execute(
            "SELECT doc_id FROM core.document WHERE source = %s AND source_ref = %s"
            " AND superseded_at IS NULL",
            (self.source, provider_doc_id),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def _write_new_version(
        self,
        doc: NormalizedDocument,
        entity_id: str | None,
        chunks: list[Chunk],
        descriptions: Mapping[int, str],
        *,
        supersedes_doc_id: int,
        known_at: datetime,
        version_group_id: int,
        owner_user: str | None,
        artifacts: ParseArtifacts | None,
        chunking_version: str | None,
    ) -> DocumentWriteResult:
        """把前序行标 superseded_at，插入沿用同一 version_group_id 的新行。

        `reparse_document`（换解析器/换参数，known_at 必须沿用旧值——见模块
        docstring 第 2 点）与 F3 的更正路由（供应商真正重发了新内容，known_at
        必须是这份新内容自己的发布时刻）共用这段"标旧、插新"的骨架，只是
        known_at 的取法不同——由调用方决定，这里不替调用方猜。
        """
        doc_confidence = score_document(doc)
        page_confidence = score_pages(doc)
        with self.conn.transaction():
            # 先标旧行 superseded_at，再插新行——document_dedup_uk 是只覆盖
            # superseded_at IS NULL 的部分唯一索引（008_document_dedup_partial.sql）。
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
                known_at=known_at,
                entity_id=entity_id,
                version_group_id=version_group_id,
                supersedes_doc_id=supersedes_doc_id,
                artifacts=artifacts,
                owner_user=owner_user,
                parse_confidence=doc_confidence,
            )
            block_ids = self._insert_blocks(
                doc,
                doc_id,
                entity_id,
                chunks,
                descriptions,
                page_confidence=page_confidence,
                doc_confidence=doc_confidence,
                chunking_version=chunking_version,
            )
        self.conn.commit()  # 理由见 write_document 里同样这一行上面的注释。
        return DocumentWriteResult(doc_id, block_ids, skipped=False)

    def reparse_document(
        self,
        doc: NormalizedDocument,
        chunks: list[Chunk],
        descriptions: Mapping[int, str],
        *,
        supersedes_doc_id: int,
        artifacts: ParseArtifacts | None = None,
        chunking_version: str | None = None,
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
        return self._write_new_version(
            doc,
            entity_id,
            chunks,
            descriptions,
            supersedes_doc_id=supersedes_doc_id,
            known_at=original_known_at,
            version_group_id=int(version_group_id),
            owner_user=original_owner_user,
            artifacts=artifacts,
            chunking_version=chunking_version,
        )

    # --- 内部 -------------------------------------------------------------

    def _known_at(self, doc: NormalizedDocument) -> datetime:
        if self.time_precision == "day":
            # `day` 精度的源报不出发布时刻的具体时分秒，publish_at 里的时间
            # 部分不可信（可能是供应商随手填的午夜或任意时刻）。用该来源自己
            # 时区下的当日 23:59:59.999999 做保守上界——"最迟不会晚于当天
            # 结束"，比相信一个不存在的精确时刻安全（CLAUDE.md §1.1）。
            day_end = datetime.combine(
                doc.publish_at.date(), time.max, tzinfo=doc.publish_at.tzinfo
            )
            return known_at_for(day_end, self.disclosure_lag)
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
        parse_confidence: float | None = None,
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
            " parse_json_ref, parse_md_ref, parse_warnings, can_show_raw, time_precision,"
            " parse_confidence) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, 0),%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s) "
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
                self.can_show_raw,
                self.time_precision,
                parse_confidence,
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
        *,
        page_confidence: Mapping[int, float] | None = None,
        doc_confidence: float | None = None,
        chunking_version: str | None = None,
    ) -> list[int]:
        # owner_user / can_show_raw 从刚插入的 document 行读回，而不是让调用方
        # 另传一份——与 known_at / publish_at 同样的道理（本方法原有的反规范化
        # 列），单一数据源保证 doc_block 这几列不可能与 document 打架
        # （02 §5.3：block 的反规范化列必须与 document 完全一致）。
        known_at, publish_at, owner_user, can_show_raw = self.conn.execute(
            "SELECT known_at, publish_at, owner_user, can_show_raw"
            " FROM core.document WHERE doc_id = %s",
            (doc_id,),
        ).fetchone()  # type: ignore[misc]
        page_confidence = page_confidence or {}

        ordinal_to_block_id: dict[int, int] = {}
        # 先插父块，再插叶子块，这样 parent_block_id 已经拿得到
        for chunk in sorted(chunks, key=lambda c: (c.parent_ordinal is not None, c.ordinal)):
            parent_block_id = (
                ordinal_to_block_id[chunk.parent_ordinal]
                if chunk.parent_ordinal is not None
                else None
            )
            # 块用自己的 page 去查 per-page 分；查不到（page 为 None，或
            # score_pages 因为 doc.page_count 未知返回了空字典）就回退用
            # 文档整体分——见 parse/confidence.py 模块 docstring。
            block_confidence = page_confidence.get(chunk.page) if chunk.page is not None else None
            if block_confidence is None:
                block_confidence = doc_confidence
            row = self.conn.execute(
                "INSERT INTO core.doc_block (doc_id, parent_block_id, block_type,"
                " section_path, ordinal, page, bbox, content, content_desc, char_len,"
                " is_leaf, entity_id, doc_type, publish_at, owner_user, valid_from, known_at,"
                " source, source_ref, ingest_run_id, can_show_raw, parse_confidence,"
                " table_html, chunking_version) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s) "
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
                    can_show_raw,
                    block_confidence,
                    chunk.table_html,
                    chunking_version,
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
