"""文档管线的 Dagster 资产。

路径 A（供应商结构化接口）的文档自带块，直接入库。
路径 B 的文档只有一个 raw_bytes_ref，要先送 xParse 解析（docs/05-document-pipeline.md §1）。
分流在 prepare_documents 里，它是纯函数——不碰数据库、不要 Dagster 上下文。

`ResourceParam[...]` 包一层的理由与 P1a Task 10（ingest/assets.py）相同：
`AnnouncementProvider`（Protocol）、`DocumentWriter`（普通类）、`Embedder`（Protocol）、
`psycopg.Connection` 都不是 Dagster 的 ResourceDefinition 子类，不加这层标记
Dagster 会把它们当成需要上游资产产出的「输入」，直接调用（测试用的调用方式）会报
`DagsterInvalidInvocationError: No value provided for required input`。

`doc_prepared` 是这四个资产里第三个：它把 `prepare_documents()` 接成真正的资产
依赖（消费 `doc_normalized` 的产出，产出喂给 `doc_blocks_loaded`），不再是测试
期才存在的裸函数调用——`definitions.py` 现在注册全部四个资产，`prepared` 参数
已经从「伪装成资源的资产依赖」改回真正的资产依赖，接线任务在这里完成。

本文件刻意不用 `from __future__ import annotations`：与 ingest/assets.py 同样的
原因——Dagster 在 `@asset` 装饰期对 `context` 参数做类型校验时直接比较
`Parameter.annotation`，不解析 PEP 563 的延迟字符串注解，开着这行会让校验
永远失败，抛出的 `DagsterInvalidDefinitionError` 还会误导人去怀疑参数名。
Python 3.11 原生支持 `list[X]` / `X | None`，去掉这行不影响其余注解写法。
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

import psycopg
from dagster import AssetExecutionContext, ResourceParam, asset

from ragdemo.adapters.announcements import AnnouncementProvider, NormalizedDocument
from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.local_manifest import count_pdf_pages
from ragdemo.config import load_config
from ragdemo.embed.base import Embedder
from ragdemo.embed.batch import EmbedStats, embed_pending_blocks
from ragdemo.ingest.assets import DAILY
from ragdemo.ingest.documents import DocumentWriter, ParseArtifacts
from ragdemo.ingest.partitions import partition_window
from ragdemo.parse.chunker import chunk_document
from ragdemo.parse.confidence import table_looks_closed
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.router import (
    check_monthly_budget,
    load_active_policy,
    record_c_tier_pages,
    record_retry_pending,
    resolve_retry_pending,
    should_route_to_tier_c,
)
from ragdemo.parse.textin import (
    DocumentParser,
    PageBudget,
    ParsePermanent,
    ParseRetryable,
    ParseTimeout,
    PrivateDocumentEgressBlocked,
    TextInParser,
)
from ragdemo.parse.tree import build_tree
from ragdemo.parse.validate import validate_chunks
from ragdemo.quality.metrics import MetricResult, record_metric
from ragdemo_core.blob import BlobNotFound, BlobStore

# block_embeddings 每次 run 的上限。不传 limit 的话 embed_pending_blocks 会
# 把（本分区收窄后的）待办队列一次性吃进内存、一次性把它们全部送去调嵌入
# API（P-26）。这道 LIMIT 挡的是单次 run 的内存占用与 API 调用量（爆炸
# 半径）。并发去重是另一件事，由 `embed_pending_blocks` 内部的
# `FOR UPDATE OF b SKIP LOCKED` 解决（embed/batch.py）——两个并发跑的分区
# 各自按批次加锁选取，一方持有锁的那一批会被另一方跳过，不会重复计费。
# 5000 大约是 DEFAULT_BATCH_SIZE（64）的 78 倍，单次 run 的内存与 API
# 调用量可控，需要的话可以多跑几次 run 把待办队列排空——按批提交（P-24
# 的修复）让重跑天然是断点续传，不必一次吃完首次入库的全部积压。
MAX_BLOCKS_PER_EMBED_RUN = 5000


@dataclass(frozen=True)
class PreparedDocument:
    doc: NormalizedDocument
    artifacts: ParseArtifacts | None
    # `prepare_documents` 的 owner_user 参数原样带到这里——它只在调用
    # parser.parse() 时用来触发私有材料闸门，闸门通过之后这个值就被
    # 扔掉了，doc_blocks_loaded 拿不到它，写库时只能落成公共行
    # （Finding 2：TEXTIN_ALLOW_PRIVATE 一旦打开，私有材料就会被写成
    # 公共可见）。带在这里，doc_blocks_loaded 才转得出去给
    # DocumentWriter.write_document(owner_user=...)。
    owner_user: str | None = None


def prepare_documents(
    docs: Sequence[NormalizedDocument],
    parser: DocumentParser,
    blob: BlobStore,
    budget: PageBudget,
    *,
    owner_user: str | None,
) -> list[PreparedDocument]:
    """`owner_user` 是必填关键字参数，没有默认值。

    `NormalizedDocument` 本身不带归属字段——文档归属该落在哪里是 P4 才要
    定的设计决定（`docs/10-roadmap.md` 的「用户上传 → 私有空间检索」），
    在一次缺陷修复里替那个决定拍板不是这里该做的事。但 `TextInParser` 的
    私有材料闸门（`textin.py`）只在直接调用 `parser.parse(..., owner_user=)`
    时才生效——不强制这一层传参，第一条用户上传路径接进来的那一刻，
    闸门就是摆设。必填关键字参数让「忘记传」这件事在调用点就报错，
    而不是在生产环境里悄悄把私有材料送出去。
    """
    prepared: list[PreparedDocument] = []
    for doc in docs:
        if doc.blocks:
            # 路径 A：供应商已经做过表格还原与章节识别，再解析一遍既花钱
            # 又不如原件准（05 §1 的优先级）。
            prepared.append(PreparedDocument(doc, None, owner_user=owner_user))
            continue
        if doc.raw_bytes_ref is None:
            # 既没有块也没有原件，没东西可做——但不写行、不留记号地悄悄
            # continue 会让下次分区重跑对着同一份文档再判一次"没东西可做"，
            # 永远停不下来。和下面 ParsePermanent 分支一个道理：写一行
            # 没有块的 document，留个记号，工作才不会被重复丢弃。
            prepared.append(
                PreparedDocument(
                    replace(doc, blocks=[]),
                    ParseArtifacts(
                        engine="skipped:no_content",
                        warnings=["既无 blocks 也无 raw_bytes_ref，没有可解析的内容"],
                    ),
                    owner_user=owner_user,
                )
            )
            continue
        if budget.exhausted:
            break  # 预算见底就停，剩下的留给下一次 run（05 §2.7）

        try:
            raw_bytes = blob.get(doc.raw_bytes_ref)
        except BlobNotFound as exc:
            # raw_bytes_ref 指向的对象在存储里丢了。这不是 xParse 的错误码，
            # 但后果和 ParsePermanent 一样：这份文档现在这个状态永远解析
            # 不了。放在 try 外面的旧写法会让这个异常直接炸穿整个循环，
            # 把本次 run 已经处理好、还没来得及 return 的全部文档一起丢掉——
            # 这才是真正要修的问题，不只是"记个 marker"。
            #
            # 捕获 BlobStore 的契约异常 BlobNotFound，而不是 FileNotFoundError
            # 本身：后者只是 LocalBlobStore 用 Path.read_bytes() 实现出来的
            # 偶然产物，P4 换成 MinIO 适配器时供应商抛的是 NoSuchKey，不是
            # FileNotFoundError，届时这里若还咬着实现细节不放，这个 catch
            # 会静默失效（BlobNotFound 子类化 FileNotFoundError，向后兼容）。
            prepared.append(
                PreparedDocument(
                    replace(doc, blocks=[]),
                    ParseArtifacts(engine="blob:missing", warnings=[str(exc)]),
                    owner_user=owner_user,
                )
            )
            continue

        try:
            result = parser.parse(raw_bytes, owner_user=owner_user)
        except ParsePermanent as exc:
            # 记号留在 parse_engine 里，意思是「别再试了」。文档行照写，
            # 只是没有块——否则下次分区重跑会再拉一遍、再失败一遍。
            prepared.append(
                PreparedDocument(
                    replace(doc, blocks=[]),
                    ParseArtifacts(engine=f"textin:{exc.marker}", warnings=[str(exc)]),
                    owner_user=owner_user,
                )
            )
            continue
        except ParseTimeout as exc:
            prepared.append(
                PreparedDocument(
                    replace(doc, blocks=[]),
                    ParseArtifacts(engine="textin:skipped", warnings=[str(exc)]),
                    owner_user=owner_user,
                )
            )
            continue
        except ParseRetryable:
            # 供应商侧的临时故障（限流、5xx、出网代理网络抖动）。这一支
            # 以前没在这里被捕获，会直接炸穿循环，把本次 run 已经处理好、
            # 已经付过费的 PreparedDocument 一起丢掉——和上面 FileNotFoundError
            # 那条要修的是同一个问题，只是漏了一支。
            #
            # 不写行、不留 marker：marker 的意思是「别再自动重试了」，
            # 但可重试的失败恰恰要交给下一次分区重跑去自然重试——这正是
            # ParseRetryable 这个类存在的意义。留白比写一行「失败」记号更
            # 正确：下次重跑时这份文档在 core.document 里没有任何痕迹，
            # 会被当成还没处理过，重新送一次 xParse。
            continue
        except PrivateDocumentEgressBlocked:
            raise  # 闸门不能被静默吞掉

        if not result.from_cache:
            budget.charge(result.page_count)
        prepared.append(
            PreparedDocument(
                replace(doc, blocks=list(result.blocks), page_count=result.page_count),
                ParseArtifacts(
                    engine=result.engine_version,
                    json_ref=result.json_ref,
                    md_ref=result.md_ref,
                    warnings=list(result.warnings),
                ),
                owner_user=owner_user,
            )
        )
    return prepared


@asset(partitions_def=DAILY, group_name="documents")
def doc_normalized(
    context: AssetExecutionContext,
    announcements: ResourceParam[AnnouncementProvider],
    conn: ResourceParam[psycopg.Connection],
) -> list[NormalizedDocument]:
    partition = date.fromisoformat(context.partition_key)
    fetch_ctx = FetchContext(
        ingest_run_id=context.op_execution_context.run_id, partition_date=partition
    )
    since, until = partition_window(context)
    docs = [
        announcements.normalize(raw)
        for raw in announcements.list_documents(fetch_ctx, since=since, until=until)
    ]
    # 阶段 F：把"应到"这个数字落一份到 quality_metric，供
    # quality/checks.py::ingest_reconciliation_check 读回比对。不经 Dagster
    # 的资产间依赖（额外 additional_ins + IdentityPartitionMapping 在真实
    # 物化时踩过坑：Dagster 会把它解读成"这个检查依赖 doc_normalized 的
    # 全部历史分区"，回填过的分区之外全部 FileNotFoundError）——落一行到
    # Postgres，检查按 (source_id, partition_date) 读回，和其余七项检查
    # 读 Postgres 而不读上游资产返回值的方式完全一致。
    record_metric(
        conn,
        partition,
        MetricResult(
            "fetched_count", float(len(docs)), True, source_id=announcements.provider
        ),
    )
    context.log.info(
        "normalized documents",
        extra={
            "run_id": context.op_execution_context.run_id,
            # 本次抓取窗口的截止时刻——这个资产本身就是按它去查供应商的，
            # 不是为了凑 CLAUDE.md §3 的字段表而现造一个值。
            "as_of": until.isoformat(),
            "count": len(docs),
        },
    )
    return docs


# prepare_documents 每次 run 的页数上限。与 MAX_BLOCKS_PER_EMBED_RUN 是同一个
# 模式：TextIn 按页计费，PageBudget 只能事后扣减（页数要等响应回来才知道，
# 拦不住已经发出去的这次调用，拦的是后面还没发的），没有这道闸门的话，回填
# 历史分区可能一次性把整月预算烧穿。2000 页留出足够余量处理正常的一日分区，
# 出现异常大批量回填时会在预算耗尽处自然停止（prepare_documents 的
# `budget.exhausted` 分支），剩下的留给下一次 run。
MAX_PAGES_PER_PREPARE_RUN = 2000


@asset(partitions_def=DAILY, group_name="documents")
def doc_prepared(
    context: AssetExecutionContext,
    doc_normalized: list[NormalizedDocument],
    parser: ResourceParam[DocumentParser],
    blob: ResourceParam[BlobStore],
    announcements: ResourceParam[AnnouncementProvider],
    conn: ResourceParam[psycopg.Connection],
) -> list[PreparedDocument]:
    """路径 A/B 分流 + 路径 B 的实际解析（把 `prepare_documents` 接成资产）。

    `prepare_documents` 本身仍是纯函数——不碰数据库、不要 Dagster 上下文，
    这层资产只是给它接上真正的上游依赖（`doc_normalized` 的产出）与两个
    资源（`parser` / `blob`）。此前这一步只存在于测试里直接调用
    `prepare_documents(...)`，`doc_blocks_loaded` 吃的是一个 `ResourceParam`
    包过的 `Sequence[PreparedDocument]`，生产环境从来没有物化过它。

    owner_user 固定传 None：这个资产目前只处理公开来源（`doc_normalized`
    只产出供应商公告，没有用户上传材料）。私有材料的入口在 P4 才接，届时
    这里要改成从分区配置或调用方读取真实的 owner_user，而不是继续写死
    None（docs/10-roadmap.md 的「用户上传 → 私有空间检索」）。

    阶段 G（F6）：重试队列。`prepare_documents` 对 `budget.exhausted` 与
    `ParseRetryable` 都故意"不写行、不留记号"（见该函数对应分支的注释），
    这样下次分区重跑才会把它们当"还没处理过"——好处是重试天然发生，代价
    是没有任何地方能查"现在到底有哪些文档卡着没进库"。`announcements` 与
    `doc_normalized` 的差集（在输入里出现过、没能在输出里对应上一份
    `PreparedDocument`）就是这批卡住的文档；`record_retry_pending`/
    `resolve_retry_pending` 只负责让它们可查，不改变 `prepare_documents`
    本身"不碰数据库"的纯函数边界。
    """
    budget = PageBudget(MAX_PAGES_PER_PREPARE_RUN)
    prepared = prepare_documents(doc_normalized, parser, blob, budget, owner_user=None)

    processed_ids = {p.doc.provider_doc_id for p in prepared}
    missing_ids = [
        d.provider_doc_id for d in doc_normalized if d.provider_doc_id not in processed_ids
    ]
    if missing_ids:
        record_retry_pending(
            conn,
            source=announcements.provider,
            provider_doc_ids=missing_ids,
            at=datetime.now(UTC),
        )
    resolve_retry_pending(conn, source=announcements.provider, provider_doc_ids=list(processed_ids))

    context.log.info(
        "prepared documents",
        extra={
            "run_id": context.op_execution_context.run_id,
            "count": len(prepared),
            "pages_remaining": budget.remaining,
            "retry_pending": len(missing_ids),
        },
    )
    return prepared


@asset(partitions_def=DAILY, group_name="documents")
def doc_blocks_loaded(
    context: AssetExecutionContext,
    doc_prepared: list[PreparedDocument],
    document_writer: ResourceParam[DocumentWriter],
) -> int:
    cfg = ChunkConfig()
    describer = MockTableDescriber()
    total = 0
    for item in doc_prepared:
        doc = item.doc
        chunks = build_tree(chunk_document(doc, cfg)) if doc.blocks else []
        descriptions = {
            c.ordinal: describer.describe(c.content, title=doc.title, section_path=c.section_path)
            for c in chunks
            if c.block_type == "table"
        }
        # item.owner_user 原样转发给写入层：这是 Finding 2 的闭环——不转发
        # 的话，prepare_documents 那边的私有材料闸门挡住了外送 xParse，
        # 但写库这一步会把结果悄悄存成公共行（owner_user 列留空）。
        result = document_writer.write_document(
            doc,
            chunks,
            descriptions,
            owner_user=item.owner_user,
            artifacts=item.artifacts,
            chunking_version=cfg.version,
        )
        if not result.skipped:
            total += len(result.block_ids)
    # entity_id 在这里没有单一取值——一次 run 横跨多份文档、多个实体，写库
    # 时才真正解析出 core.entity 的 entity_id（DocumentWriter._resolve_
    # entity_id，对调用方不可见）。能不臆造就用手头真实有的字段：供应商侧
    # 的原始代码 entity_ref，去重后的列表，至少能定位到这批文档涉及谁。
    entity_refs = sorted({item.doc.entity_ref for item in doc_prepared if item.doc.entity_ref})
    context.log.info(
        "loaded blocks",
        extra={
            "run_id": context.op_execution_context.run_id,
            "entity_refs": entity_refs,
            "count": total,
        },
    )
    return total


def _entity_ref_for(conn: psycopg.Connection, entity_id: str | None) -> str | None:
    """反查 entity_id 对应的任意一个供应商代码列，供重解析构造
    `NormalizedDocument.entity_ref`——`core.document` 只存了已解析的
    `entity_id`，没有存原始的供应商代码字符串；`DocumentWriter._resolve_
    entity_id` 又只认供应商代码做匹配。取任意一列非空的代码即可，
    `_resolve_entity_id` 用它反查出的一定还是同一个 entity_id（四列
    OR 匹配，只要命中一列就够）。"""
    if entity_id is None:
        return None
    row = conn.execute(
        "SELECT tushare_code, ifind_code, wind_code, edgar_cik"
        "  FROM core.entity WHERE entity_id = %s",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    return next((str(v) for v in row if v is not None), None)


def _table_closure_for_document(conn: psycopg.Connection, doc_id: int) -> tuple[float | None, bool]:
    """这份文档自己的表格闭合率与"是否含数值表格"——阶段 D 的
    `table_closure_rate_check` 算的是整个分区窗口的比率，这里要的是单份
    文档自己的，两者场景不同，各自独立计算，不复用同一个函数。"""
    rows = conn.execute(
        "SELECT content FROM core.doc_block WHERE doc_id = %s AND block_type = 'table'",
        (doc_id,),
    ).fetchall()
    tables = [str(r[0]) for r in rows]
    if not tables:
        return None, False
    closure_rate = sum(1 for t in tables if table_looks_closed(t)) / len(tables)
    has_numeric = any(ch.isdigit() for t in tables for ch in t)
    return closure_rate, has_numeric


def _mark_budget_exceeded(conn: psycopg.Connection, doc_id: int) -> None:
    """G3：超预算时仍然入库，只是告警降级——parse_warnings 追加
    'budget_exceeded'，parse_confidence 原样保留（不重解析就没有新分数，
    保留旧分数本身就是"仍标低置信度"）。"""
    conn.execute(
        "UPDATE core.document SET parse_warnings = parse_warnings || '[\"budget_exceeded\"]'::jsonb"
        " WHERE doc_id = %s",
        (doc_id,),
    )
    conn.commit()


def _next_month(moment: datetime) -> datetime:
    return datetime(moment.year + moment.month // 12, moment.month % 12 + 1, 1, tzinfo=UTC)


@asset(partitions_def=DAILY, group_name="documents", deps=[doc_blocks_loaded])
def tier_c_reparse(
    context: AssetExecutionContext,
    conn: ResourceParam[psycopg.Connection],
    blob: ResourceParam[BlobStore],
    tier_c_parser: ResourceParam[TextInParser | None],
) -> int:
    """低置信度文档的二次解析路由（阶段 G）。`deps=[doc_blocks_loaded]` 是
    纯排序依赖，理由与 `block_embeddings` 对 `doc_blocks_loaded` 的依赖
    完全一致（见该资产的 docstring）——这里同样不需要它的返回值，只要它
    先跑完：这个资产读的是它刚写进 `core.document` 的那批行。

    `tier_c_parser` 为 None 时整体空操作——`definitions.py` 按
    `TEXTIN_BASE_URL` 是否设置决定注入真实的高精度 `TextInParser` 还是
    None（与 `parser_resource` 同一个判断时机），离线开发/测试环境不该
    假装做了 C 档重解析。解析器作为资源注入（而不是在这里现场构造），
    是为了让测试能直接传一个带 `httpx.MockTransport` 的 `TextInParser`
    实例进来，不需要真的连一次网。
    """
    if tier_c_parser is None:
        context.log.info("tier C reparse skipped: TEXTIN_BASE_URL not configured")
        return 0
    cfg = load_config()
    if cfg.textin_cost_per_page_cny is None:
        context.log.info("tier C reparse skipped: TEXTIN_COST_PER_PAGE_CNY not configured")
        return 0

    since, until = partition_window(context)
    rows = conn.execute(
        "SELECT doc_id, doc_type, raw_ref, entity_id, parse_confidence, title, period,"
        " publish_at, language, source, source_url, is_correction"
        "  FROM core.document"
        " WHERE publish_at > %s AND publish_at <= %s"
        "   AND superseded_at IS NULL AND raw_ref IS NOT NULL",
        (since, until),
    ).fetchall()

    parser = tier_c_parser
    cost_per_page = cfg.require_textin_cost_per_page_cny()
    now = datetime.now(UTC)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    month_end = _next_month(month_start)
    describer = MockTableDescriber()
    chunk_cfg = ChunkConfig()
    reparsed = 0

    for (
        doc_id,
        doc_type,
        raw_ref,
        entity_id,
        parse_confidence,
        title,
        period,
        publish_at,
        language,
        source,
        source_url,
        is_correction,
    ) in rows:
        table_closure, has_numeric = _table_closure_for_document(conn, doc_id)
        policy = load_active_policy(conn, doc_type, now)
        if not should_route_to_tier_c(
            policy,
            parse_confidence=float(parse_confidence) if parse_confidence is not None else None,
            table_closure_rate=table_closure,
            has_numeric_table=has_numeric,
        ):
            continue
        assert policy is not None  # should_route_to_tier_c 只在有策略时才可能返回 True

        raw_bytes = blob.get(raw_ref)
        estimated_pages = count_pdf_pages(raw_bytes) or 1
        decision = check_monthly_budget(
            conn,
            policy,
            doc_type=doc_type,
            month_start=month_start,
            month_end=month_end,
            pages_about_to_spend=estimated_pages,
            cost_per_page_cny=cost_per_page,
        )
        if not decision.allowed:
            _mark_budget_exceeded(conn, doc_id)
            context.log.warning(
                "tier C budget exceeded",
                extra={
                    "doc_id": doc_id,
                    "doc_type": doc_type,
                    "spent_cny": str(decision.spent_cny),
                },
            )
            continue

        result = parser.parse(raw_bytes)
        new_doc = NormalizedDocument(
            provider_doc_id=str(doc_id),
            entity_ref=_entity_ref_for(conn, entity_id),
            doc_type=doc_type,
            title=title,
            period=period,
            publish_at=publish_at,
            language=language,
            source_url=source_url,
            raw_bytes_ref=raw_ref,
            content_hash=TextInParser.content_hash(raw_bytes),
            is_correction=bool(is_correction),
            supersedes_provider_doc_id=None,
            page_count=result.page_count,
            blocks=result.blocks,
        )
        chunks = build_tree(chunk_document(new_doc, chunk_cfg)) if new_doc.blocks else []
        descriptions = {
            c.ordinal: describer.describe(
                c.content, title=new_doc.title, section_path=c.section_path
            )
            for c in chunks
            if c.block_type == "table"
        }
        if validate_chunks(new_doc, chunks, descriptions):
            # 重解析结果本身没通过元数据校验——保留原文档不动，不強行替换成
            # 一份更差的结果；仍然告警，运维需要知道这份文档 C 档重解析失败了。
            context.log.warning("tier C reparse failed validation", extra={"doc_id": doc_id})
            continue

        run_id = context.op_execution_context.run_id
        writer = DocumentWriter(conn, ingest_run_id=run_id, source=source)
        artifacts = ParseArtifacts(
            engine=result.engine_version, json_ref=result.json_ref, md_ref=result.md_ref
        )
        writer.reparse_document(
            new_doc,
            chunks,
            descriptions,
            supersedes_doc_id=doc_id,
            artifacts=artifacts,
            chunking_version=chunk_cfg.version,
        )
        record_c_tier_pages(conn, doc_type=doc_type, pages=result.page_count, at=now)
        reparsed += 1

    context.log.info(
        "tier C reparse", extra={"run_id": context.op_execution_context.run_id, "count": reparsed}
    )
    return reparsed


@asset(partitions_def=DAILY, group_name="documents", deps=[doc_blocks_loaded, tier_c_reparse])
def block_embeddings(
    context: AssetExecutionContext,
    conn: ResourceParam[psycopg.Connection],
    embedder: ResourceParam[Embedder],
) -> EmbedStats:
    """`deps=[doc_blocks_loaded]` 是纯排序依赖，不接它的返回值：这个资产
    读的是 `core.doc_block.embedding IS NULL` 队列，是通过数据库状态、
    不是通过 Dagster 的 I/O 管理器与 `doc_blocks_loaded` 关联的。不声明
    这条边的话，`doc_blocks_loaded` 与 `block_embeddings` 在资产图里是两个
    互不相干的节点，Dagster 的并发执行器可能先跑 `block_embeddings`——
    这不是假设：接进 `Definitions` 后第一次真实物化就复现了，`block_
    embeddings` 在 `doc_blocks_loaded` 还没开始时就已经跑完并报告了
    0 条待办。`deps=` 只加一条排序边，不要求它的输出值，因此不需要在
    函数签名里加一个用不到的 `doc_blocks_loaded: int` 参数。
    """
    # P-26：不传 limit 会 fetchall() 全部待办队列，单次 run 的内存占用与
    # API 调用量不可控。上限的取值与理由见模块顶部常量——它挡的是单次
    # run 的爆炸半径，不是并发分区之间的去重（那个由 embed_pending_blocks
    # 内部的 FOR UPDATE OF b SKIP LOCKED 解决）。
    #
    # published_after/until 收窄到本分区：与 doc_normalized 用的是同一个
    # (since, until] 窗口（ingest/partitions.py 的 partition_window），不是
    # ingested_at。不收窄的话，重跑某一天的分区会把全部待办队列里其他分区的
    # 积压也一起吃掉；用 ingested_at 收窄则会在回填历史分区时完全找不到该
    # 分区的块——回填今天执行、写入的行 ingested_at 是今天，但这个分区该
    # 处理的是 publish_at 落在分区那一天的文档（partition_window 的
    # docstring 有完整解释）。
    since, until = partition_window(context)
    stats = embed_pending_blocks(
        conn,
        embedder,
        limit=MAX_BLOCKS_PER_EMBED_RUN,
        published_after=since,
        published_until=until,
    )
    context.log.info(
        "embedded",
        extra={
            "run_id": context.op_execution_context.run_id,
            "partition_key": context.partition_key,
            "pending": stats.pending,
            "from_cache": stats.from_cache,
            "computed": stats.computed,
        },
    )
    return stats
