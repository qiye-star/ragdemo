"""元数据完整性校验（docs/05-document-pipeline.md §6）。

整份文档回滚而非跳过坏块：部分入库的文档会让检索返回残缺证据，
比没有这份文档更危险。
"""
from __future__ import annotations

from collections.abc import Mapping

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.parse.chunker import Chunk

MetadataViolation = str


def validate_chunks(
    doc: NormalizedDocument,
    chunks: list[Chunk],
    descriptions: Mapping[int, str] | None = None,
) -> list[MetadataViolation]:
    """返回全部违规项；空列表表示通过。一次报全，避免修一个跑一次。"""
    violations: list[MetadataViolation] = []
    descriptions = descriptions or {}

    ordinals = sorted(c.ordinal for c in chunks)
    if ordinals != list(range(len(chunks))):
        violations.append(f"ordinal 不连续或有重复: {ordinals}")

    by_ordinal = {c.ordinal: c for c in chunks}
    children: dict[int, list[Chunk]] = {}
    for chunk in chunks:
        if chunk.parent_ordinal is not None:
            children.setdefault(chunk.parent_ordinal, []).append(chunk)

    for chunk in chunks:
        where = f"块 {chunk.ordinal}"

        if not chunk.content.strip():
            violations.append(f"{where}: content 为空")

        if chunk.block_type == "table" and not descriptions.get(chunk.ordinal, "").strip():
            violations.append(f"{where}: 表格块缺少 content_desc")

        if chunk.parent_ordinal is not None:
            parent = by_ordinal.get(chunk.parent_ordinal)
            if parent is None:
                violations.append(f"{where}: parent_ordinal {chunk.parent_ordinal} 不存在")
            elif parent.parent_ordinal is not None:
                violations.append(f"{where}: 嵌套超过两层，父块自己还有父块")

        if chunk.page is not None:
            upper = doc.page_count or 0
            if chunk.page < 1 or (upper and chunk.page > upper):
                violations.append(f"{where}: page {chunk.page} 超出 [1, {upper}]")

        has_children = bool(children.get(chunk.ordinal))
        if chunk.is_leaf and has_children:
            violations.append(f"{where}: is_leaf=true 但有子块")
        if not chunk.is_leaf and not has_children:
            violations.append(f"{where}: is_leaf=false 但没有子块")

    return violations
