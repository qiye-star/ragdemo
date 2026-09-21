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


@dataclass(frozen=True)
class PreparedDocument:
    doc: NormalizedDocument
    artifacts: ParseArtifacts | None


def prepare_documents(
    docs: Sequence[NormalizedDocument],
    parser: DocumentParser,
    blob: BlobStore,
    budget: PageBudget,
) -> list[PreparedDocument]:
    prepared: list[PreparedDocument] = []
    for doc in docs:
        if doc.blocks:
            # 路径 A：供应商已经做过表格还原与章节识别，再解析一遍既花钱
            # 又不如原件准（05 §1 的优先级）。
            prepared.append(PreparedDocument(doc, None))
            continue
        if doc.raw_bytes_ref is None:
            continue  # 既没有块也没有原件，没东西可做
        if budget.exhausted:
            break  # 预算见底就停，剩下的留给下一次 run（05 §2.7）

        try:
            result = parser.parse(blob.get(doc.raw_bytes_ref))
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
        extra={"run_id": context.op_execution_context.run_id, "count": len(docs)},
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
    context.log.info(
        "loaded blocks", extra={"run_id": context.op_execution_context.run_id, "count": total}
    )
    return total


@asset(partitions_def=DAILY, group_name="documents")
def block_embeddings(
    context: AssetExecutionContext,
    conn: ResourceParam[psycopg.Connection],
    embedder: ResourceParam[Embedder],
) -> EmbedStats:
    stats = embed_pending_blocks(conn, embedder)
    context.log.info(
        "embedded",
        extra={
            "run_id": context.op_execution_context.run_id,
            "pending": stats.pending,
            "from_cache": stats.from_cache,
            "computed": stats.computed,
        },
    )
    return stats
