"""文档管线的 Dagster 资产。

路径 A（供应商结构化接口）的文档自带块，直接入库。
路径 B 的文档只有一个 raw_bytes_ref，要先送 xParse 解析（docs/05-document-pipeline.md §1）。
分流在 prepare_documents 里，它是纯函数——不碰数据库、不要 Dagster 上下文。

`ResourceParam[...]` 包一层的理由与 P1a Task 10（ingest/assets.py）相同：
`AnnouncementProvider`（Protocol）、`DocumentWriter`（普通类）、`Embedder`（Protocol）、
`psycopg.Connection` 都不是 Dagster 的 ResourceDefinition 子类，不加这层标记
Dagster 会把它们当成需要上游资产产出的「输入」，直接调用（测试用的调用方式）会报
`DagsterInvalidInvocationError: No value provided for required input`。
`doc_blocks_loaded` 的 `prepared` 参数同样标了 `ResourceParam`——它不是真正的
Dagster 资源，是 `prepare_documents()` 的普通返回值，这样标只是为了让直接调用
成立；本任务不改 `definitions.py`，真正接入生产 DAG 时这三个资产该怎么串起来
（`prepared` 从「资源」改回「资产依赖」，或者把 `prepare_documents` 挪进
`doc_blocks_loaded` 内部调用）是留给接线任务的设计决定，不在这里下判断。

本文件刻意不用 `from __future__ import annotations`：与 ingest/assets.py 同样的
原因——Dagster 在 `@asset` 装饰期对 `context` 参数做类型校验时直接比较
`Parameter.annotation`，不解析 PEP 563 的延迟字符串注解，开着这行会让校验
永远失败，抛出的 `DagsterInvalidDefinitionError` 还会误导人去怀疑参数名。
Python 3.11 原生支持 `list[X]` / `X | None`，去掉这行不影响其余注解写法。
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

import psycopg
from dagster import AssetExecutionContext, ResourceParam, asset

from ragdemo.adapters.announcements import AnnouncementProvider, NormalizedDocument
from ragdemo.adapters.base import FetchContext
from ragdemo.embed.base import Embedder
from ragdemo.embed.batch import EmbedStats, embed_pending_blocks
from ragdemo.ingest.assets import DAILY
from ragdemo.ingest.documents import DocumentWriter, ParseArtifacts
from ragdemo.parse.chunker import chunk_document
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.textin import (
    DocumentParser,
    PageBudget,
    ParsePermanent,
    ParseTimeout,
    PrivateDocumentEgressBlocked,
)
from ragdemo.parse.tree import build_tree
from ragdemo_core.blob import BlobStore

LOOKBACK = timedelta(days=1)

# block_embeddings 每次 run 的上限。不传 limit 的话 embed_pending_blocks 会
# fetchall() 全局待办队列——两个并发的回填分区各选中同一批行，谁都看不见
# 对方还没提交的缓存行，账单翻倍（P-26）。5000 大约是 DEFAULT_BATCH_SIZE
# （64）的 78 倍，单次 run 的内存与 API 调用量可控，需要的话可以多跑几次
# run 把待办队列排空——按批提交（P-24 的修复）让重跑天然是断点续传，
# 不必一次吃完「数十万块」的首次入库。partition 自身的范围收窄留给
# 接线任务决定（见模块 docstring），这里只收紧上限，不改查询范围。
MAX_BLOCKS_PER_EMBED_RUN = 5000


@dataclass(frozen=True)
class PreparedDocument:
    doc: NormalizedDocument
    artifacts: ParseArtifacts | None


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
            prepared.append(PreparedDocument(doc, None))
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
                )
            )
            continue
        if budget.exhausted:
            break  # 预算见底就停，剩下的留给下一次 run（05 §2.7）

        try:
            raw_bytes = blob.get(doc.raw_bytes_ref)
        except FileNotFoundError as exc:
            # raw_bytes_ref 指向的对象在存储里丢了。这不是 xParse 的错误码，
            # 但后果和 ParsePermanent 一样：这份文档现在这个状态永远解析
            # 不了。放在 try 外面的旧写法会让这个异常直接炸穿整个循环，
            # 把本次 run 已经处理好、还没来得及 return 的全部文档一起丢掉——
            # 这才是真正要修的问题，不只是"记个 marker"。
            prepared.append(
                PreparedDocument(
                    replace(doc, blocks=[]),
                    ParseArtifacts(engine="blob:missing", warnings=[str(exc)]),
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
                )
            )
            continue
        except ParseTimeout as exc:
            prepared.append(
                PreparedDocument(
                    replace(doc, blocks=[]),
                    ParseArtifacts(engine="textin:skipped", warnings=[str(exc)]),
                )
            )
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
            )
        )
    return prepared


@asset(partitions_def=DAILY, group_name="documents")
def doc_normalized(
    context: AssetExecutionContext, announcements: ResourceParam[AnnouncementProvider]
) -> list[NormalizedDocument]:
    partition = date.fromisoformat(context.partition_key)
    fetch_ctx = FetchContext(
        ingest_run_id=context.op_execution_context.run_id, partition_date=partition
    )
    until = datetime.combine(partition, datetime.max.time(), tzinfo=UTC)
    since = until - LOOKBACK
    docs = [
        announcements.normalize(raw)
        for raw in announcements.list_documents(fetch_ctx, since=since, until=until)
    ]
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


@asset(partitions_def=DAILY, group_name="documents")
def doc_blocks_loaded(
    context: AssetExecutionContext,
    prepared: ResourceParam[Sequence[PreparedDocument]],
    writer: ResourceParam[DocumentWriter],
) -> int:
    cfg = ChunkConfig()
    describer = MockTableDescriber()
    total = 0
    for item in prepared:
        doc = item.doc
        chunks = build_tree(chunk_document(doc, cfg)) if doc.blocks else []
        descriptions = {
            c.ordinal: describer.describe(c.content, title=doc.title, section_path=c.section_path)
            for c in chunks
            if c.block_type == "table"
        }
        result = writer.write_document(doc, chunks, descriptions, artifacts=item.artifacts)
        if not result.skipped:
            total += len(result.block_ids)
    # entity_id 在这里没有单一取值——一次 run 横跨多份文档、多个实体，写库
    # 时才真正解析出 core.entity 的 entity_id（DocumentWriter._resolve_
    # entity_id，对调用方不可见）。能不臆造就用手头真实有的字段：供应商侧
    # 的原始代码 entity_ref，去重后的列表，至少能定位到这批文档涉及谁。
    entity_refs = sorted({item.doc.entity_ref for item in prepared if item.doc.entity_ref})
    context.log.info(
        "loaded blocks",
        extra={
            "run_id": context.op_execution_context.run_id,
            "entity_refs": entity_refs,
            "count": total,
        },
    )
    return total


@asset(partitions_def=DAILY, group_name="documents")
def block_embeddings(
    context: AssetExecutionContext,
    conn: ResourceParam[psycopg.Connection],
    embedder: ResourceParam[Embedder],
) -> EmbedStats:
    # P-26：不传 limit 会 fetchall() 全局待办队列——两个并发分区互相看不见
    # 对方还没提交的缓存行，账单翻倍。上限的取值与理由见模块顶部常量。
    stats = embed_pending_blocks(conn, embedder, limit=MAX_BLOCKS_PER_EMBED_RUN)
    # as_of／entity_id 在这里没有真实取值：这个资产查的是全局 embedding IS
    # NULL 队列，不按分区或实体过滤（P-26 明确把"按分区收窄查询范围"留给
    # 接线任务），队列本身也横跨多个实体。硬填会违反"不得臆造值"，所以
    # 只带 partition_key——它是这次 run 真实携带的调度信息，不等价于
    # 查询用的 as_of，因此没有借用那个字段名。
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
