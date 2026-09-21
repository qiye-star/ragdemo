# P1b 文档管线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把原始文档变成可检索的块——解析（含 JSON 与 Markdown 产物落盘）、按结构切分、构造父子块、生成表格描述、校验元数据、向量化入库，全程保持时点语义与幂等。

**Architecture:** 路径 B 的解析走 TextIn xParse 托管 API，产物（完整响应 JSON + Markdown）落对象存储，key 由「原始字节哈希 + 参数指纹」决定，因而同时是成本缓存与重切块的数据源；切块的输入是响应里的 `detail[]` 而不是 `markdown`（后者没有页码）。纯函数的切块器（`chunker.py`）不碰数据库，只做 `NormalizedDocument → list[Chunk]`；`DocumentWriter` 负责把块连同反规范化列写进 `core.document` / `core.doc_block`；嵌入是**独立的 Dagster 资产**，以 `embedding IS NULL` 为待办队列，因此天然可断点续传。

**Tech Stack:** Python 3.11+ / psycopg 3 / pgvector / Dagster / httpx / TextIn xParse / pytest

**Spec:** [`docs/05-document-pipeline.md`](../../05-document-pipeline.md)、[`docs/02-data-model.md`](../../02-data-model.md) §5、[`docs/adr/0008`](../../adr/0008-textin-xparse-document-parsing.md)
**工作流：** [`docs/11-sdlc.md`](../../11-sdlc.md) §3 的 W2.1–W2.5
**前置：** [P0](2026-09-21-p0-foundation.md) 全部验收 + [P1a](2026-09-21-p1a-ingestion.md) Task 1/3/8

## 规范冲突与裁定（开工前必读）

**冲突：** [`05-document-pipeline.md`](../../05-document-pipeline.md) §3.1 的表格写「叶子块长度
300–800 字」，§4 写「子块 ≤ 200 字用于检索」，而 §4.2 第 2 步写「父块内按 §3.1 规则切出
≤ 200 字的子块」——**一句话里同时引用了两个互斥的长度**。矛盾源自原始简报 §8，
它同时列了这两条而没说明层级关系。

**裁定：两层结构，叶子块 200–400 字。**

| 层 | 长度 | 用途 | 嵌入 |
|---|---|---|---|
| 父块 | 整个小节，不限长 | 生成时提供完整上下文 | 否（`embedding IS NULL`、`is_leaf = false`） |
| 叶子块 | **200–400 字**，重叠 12% | 召回 | 是 |

**理由：** ≤200 字对中文财务文本太短——「智能计算集群系统业务实现营业收入 12,340 万元，
同比增长 58.2%，主要系云端训练芯片出货量提升所致」这一句就 48 字，200 字上限只能装
2–4 句，「数字 + 归因」的因果链经常被切断，而这恰恰是研究场景最需要完整保留的东西。
300–800 字则对向量检索太长，信号被稀释。200–400 是折中，且**做成可配置**，
由 P1c 的检索评测集实测定最终值。

**代价：** 若评测显示 400 上限仍偏长，需要重切块并重嵌入一次（约数小时的 API 调用）。
可接受——总比现在拍一个未经检验的数字然后一路错下去好。

**Task 1 包含修正 `05-document-pipeline.md` §3.1 与 §4.2，让文档与实现一致。**

## 对上游的接口假设

| 接口 | 来自 |
|---|---|
| `NormalizedDocument` / `NormalizedBlock` | P1a Task 8 |
| `MockAnnouncementProvider` | P1a Task 8 |
| `PointInTimeWriter`、`WriteOutcome` | P1a Task 3 |
| `known_at_for(publish_at, lag)` | P1a Task 8 |
| `temp_db` fixture、`migrate()` | P0 Task 2/3 |
| `core.document` / `core.doc_block` / `core.embedding_cache` | P0 Task 7 |
| `core.document.parse_json_ref` / `parse_md_ref` / `parse_warnings` | 迁移 `007_parse_artifacts.sql`（随 adr/0008 新增，已落盘） |

> **仓库布局**：本仓库是 **uv workspace 双包**结构——底层 `packages/ragdemo-core/src/ragdemo_core/`
> （迁移、时点会话、Schema 不变量）与业务层 `packages/ragdemo/src/ragdemo/`（接入、解析、检索、Agent）。
> 依赖方向由包边界物理强制（[`01-architecture.md`](../../01-architecture.md) §5）：
> `ragdemo` 依赖 `ragdemo-core`，反向 import 会因包边界而失败。
> 装依赖用 `uv sync`，跑命令用 `uv run`，**不要 `pip install -e .`**。
> 测试在仓库根的 `tests/`，不在包内。

## Global Constraints

- Python **3.11+**；完整类型注解；`ruff` 与 `mypy --strict` 通过。
- **TDD**：先写失败的测试，跑一遍确认失败，再写最小实现。
- **切块器是纯函数**，不碰数据库、不调网络——这样它能被大量用例快速覆盖。
- **表格块永不切分**，无论多长。切开的表格无法理解。
- **父块不做嵌入**；只有 `is_leaf = true` 的块参与召回。
- 向量维度固定 **1024**，写入前做 **L2 归一化**（HNSW 用 `vector_cosine_ops`）。
- **重新解析不改 `known_at`**：沿用原文档的 `known_at`，否则该文档在历史回测中凭空消失。
- 元数据校验任一项不过，**整份文档回滚**，不写半份。
- 嵌入缓存主键是 `(content_hash, model, owner_user)` 三列。
- **切块的输入是 xParse 的 `detail[]`，不是 `markdown`**——后者没有页码，
  而 CLAUDE.md §0 要求每个数字绑定 `[block_id | page]`。
- **`parse_engine` 必须是 `textin:<版本>+<参数指纹>`**。只记版本不够：
  参数一改切块结果就变，评测集的 `gold_block_ids` 会静默失效。
- **调用计费 API 前先查产物缓存**（`parse/textin/<content_hash>/<param_fp>.json`）。
  同一份文档在同一套参数下只解析一次。
- **私有文档不外送**：`owner_user` 非空且 `TEXTIN_ALLOW_PRIVATE` 未开时直接拒绝。
- **认证头不进业务代码**：`x-ti-app-id` / `x-ti-secret-code` 由出网代理注入
  （`09-compliance-security.md` §4.1）。测试夹具里也不得出现。
- 提交信息用 Conventional Commits。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `packages/ragdemo/src/ragdemo/parse/__init__.py` | 包声明 |
| `packages/ragdemo/src/ragdemo/parse/config.py` | `ChunkConfig`（长度、重叠、过滤开关） |
| `packages/ragdemo/src/ragdemo/parse/chunker.py` | 纯函数切块：结构切分、重叠、表格保护 |
| `packages/ragdemo/src/ragdemo/parse/tree.py` | 父子块构造与 `is_leaf` 计算 |
| `packages/ragdemo/src/ragdemo/parse/filters.py` | 页眉页脚、目录、会计政策模板的过滤规则 |
| `packages/ragdemo/src/ragdemo/parse/describe.py` | `TableDescriber` 协议 + Mock + prompt 版本 |
| `packages/ragdemo/src/ragdemo/parse/validate.py` | 元数据完整性校验 8 项 |
| `packages/ragdemo/src/ragdemo/parse/textin.py` | TextIn xParse 封装：参数指纹、错误码分类、`detail[]` → 块、产物落盘 |
| `packages/ragdemo-core/src/ragdemo_core/blob.py` | `BlobStore` 协议 + `LocalBlobStore`（解析产物与原始件的对象存储） |
| `packages/ragdemo/src/ragdemo/ingest/documents.py` | `DocumentWriter`：文档与块入库、版本化重解析 |
| `packages/ragdemo/src/ragdemo/embed/__init__.py` | 包声明 |
| `packages/ragdemo/src/ragdemo/embed/base.py` | `Embedder` 协议、`l2_normalize`、`embedding_input` |
| `packages/ragdemo/src/ragdemo/embed/mock.py` | `MockEmbedder`（确定性向量，供测试与离线开发） |
| `packages/ragdemo/src/ragdemo/embed/cache.py` | 内容哈希缓存读写 |
| `packages/ragdemo/src/ragdemo/embed/batch.py` | 批处理 + 断点续传 |
| `packages/ragdemo/src/ragdemo/ingest/assets_docs.py` | 文档与嵌入的 Dagster 资产 |

---

## Task 1: 切块配置与规范修正

**Files:**
- Create: `packages/ragdemo/src/ragdemo/parse/__init__.py`, `packages/ragdemo/src/ragdemo/parse/config.py`, `tests/parse/__init__.py`, `tests/parse/test_config.py`
- Modify: `docs/05-document-pipeline.md`（§3.1 表格与 §4.2 第 2 步）

**Interfaces:**
- Consumes: 无
- Produces:
  - `ChunkConfig(leaf_min_chars=200, leaf_max_chars=400, overlap_ratio=0.12, keep_table_whole=True, drop_boilerplate=True)`
  - `ChunkConfig.overlap_chars: int`（派生属性）
  - `ChunkConfig.validate() -> None`

- [ ] **Step 1: 写失败的测试**

`tests/parse/__init__.py`：空文件。

`tests/parse/test_config.py`：

```python
"""切块配置。默认值来自本计划开头的裁定，且必须自洽。"""
from __future__ import annotations

import pytest

from ragdemo.parse.config import ChunkConfig


def test_defaults_match_the_ruling() -> None:
    cfg = ChunkConfig()
    assert (cfg.leaf_min_chars, cfg.leaf_max_chars) == (200, 400)
    assert cfg.overlap_ratio == 0.12
    assert cfg.keep_table_whole is True


def test_overlap_chars_is_derived_from_max() -> None:
    assert ChunkConfig(leaf_max_chars=400, overlap_ratio=0.12).overlap_chars == 48


def test_min_greater_than_max_is_rejected() -> None:
    with pytest.raises(ValueError, match="leaf_min_chars"):
        ChunkConfig(leaf_min_chars=500, leaf_max_chars=400).validate()


def test_overlap_must_be_smaller_than_min_chunk() -> None:
    """重叠 ≥ 最小块长会让切分不收敛——每一步前进的距离为零或负。"""
    with pytest.raises(ValueError, match="overlap"):
        ChunkConfig(leaf_min_chars=200, leaf_max_chars=400, overlap_ratio=0.9).validate()


def test_overlap_ratio_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="overlap_ratio"):
        ChunkConfig(overlap_ratio=1.5).validate()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/parse/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.parse'`

- [ ] **Step 3: 写最小实现 + 修正规范文档**

`packages/ragdemo/src/ragdemo/parse/__init__.py`：

```python
"""文档解析与切块。切块器是纯函数，不碰数据库。"""
```

`packages/ragdemo/src/ragdemo/parse/config.py`：

```python
"""切块参数。

长度取 200–400 字，见 docs/05-document-pipeline.md §3.1。
做成配置而非常量，是因为最终值要由 P1c 的检索评测集实测确定。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkConfig:
    leaf_min_chars: int = 200
    leaf_max_chars: int = 400
    overlap_ratio: float = 0.12
    keep_table_whole: bool = True
    drop_boilerplate: bool = True

    @property
    def overlap_chars(self) -> int:
        return int(self.leaf_max_chars * self.overlap_ratio)

    def validate(self) -> None:
        if not 0.0 <= self.overlap_ratio < 1.0:
            raise ValueError(f"overlap_ratio 必须在 [0, 1) 内，收到 {self.overlap_ratio}")
        if self.leaf_min_chars > self.leaf_max_chars:
            raise ValueError(
                f"leaf_min_chars {self.leaf_min_chars} 大于 leaf_max_chars {self.leaf_max_chars}"
            )
        if self.overlap_chars >= self.leaf_min_chars:
            raise ValueError(
                f"overlap {self.overlap_chars} 不小于 leaf_min_chars {self.leaf_min_chars}，"
                "切分不会收敛"
            )
```

修改 `docs/05-document-pipeline.md` §3.1 的长度行：

```markdown
| 叶子块长度 | **200–400 字**（中文字符），可配置 | 短于 200 会切断「数字 + 归因」的因果链；长于 400 向量信号被稀释。最终值由检索评测集实测确定，见 `ChunkConfig` |
| 重叠 | 12%（约 48 字） | 防止关键句被切在边界上 |
```

修改 §4.2 第 2 步：

```markdown
2. 父块内按 §3.1 的 `ChunkConfig` 切出 200–400 字的叶子块，`parent_block_id` 指向父块；
```

并在 §4.1 后追加一段：

```markdown
> **长度的两层含义**：父块是整个小节（不限长，只供生成），叶子块是 200–400 字
> （只供召回）。早期版本的本文档在这两处写了互斥的数字（300–800 与 ≤200），
> 已按 `docs/superpowers/plans/2026-09-21-p1b-document-pipeline.md` 的裁定统一。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/parse/test_config.py -v && grep -n "200–400" docs/05-document-pipeline.md`
Expected: 5 passed；文档中能查到修正后的长度

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo/src/ragdemo/parse tests/parse docs/05-document-pipeline.md
git commit -m "feat(parse): 切块配置；修正规范中叶子块长度的自相矛盾"
```

---

## Task 2: 切块器

**Files:**
- Create: `packages/ragdemo/src/ragdemo/parse/filters.py`, `packages/ragdemo/src/ragdemo/parse/chunker.py`, `tests/parse/test_chunker.py`

**Interfaces:**
- Consumes: Task 1 的 `ChunkConfig`；P1a Task 8 的 `NormalizedDocument` / `NormalizedBlock`
- Produces:
  - `Chunk(ordinal, block_type, section_path, content, page, bbox, is_leaf, parent_ordinal)`
  - `chunk_document(doc: NormalizedDocument, cfg: ChunkConfig) -> list[Chunk]`（只产叶子块，父块由 Task 3 构造）
  - `is_boilerplate(text: str) -> bool`
  - `split_text(text: str, cfg: ChunkConfig) -> list[str]`

- [ ] **Step 1: 写失败的测试**

`tests/parse/test_chunker.py`：

```python
"""切块器：纯函数，覆盖长度、重叠、表格保护、章节边界、样板过滤。"""
from __future__ import annotations

from datetime import UTC, datetime

from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument
from ragdemo.parse.chunker import chunk_document, split_text
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.filters import is_boilerplate

CFG = ChunkConfig()


def _doc(blocks: list[NormalizedBlock]) -> NormalizedDocument:
    return NormalizedDocument(
        provider_doc_id="D1", entity_ref="688256.SH", doc_type="quarterly",
        title="三季报", period="2024Q3",
        publish_at=datetime(2024, 10, 28, 18, 32, tzinfo=UTC),
        language="zh", source_url=None, raw_bytes_ref=None, content_hash="h",
        is_correction=False, supersedes_provider_doc_id=None, page_count=1,
        blocks=blocks,
    )


def test_short_paragraph_stays_one_chunk() -> None:
    parts = split_text("报告期内营业收入 12,340 万元。", CFG)
    assert parts == ["报告期内营业收入 12,340 万元。"]


def test_long_paragraph_is_split_within_max() -> None:
    text = "。".join(f"第{i}句内容" * 6 for i in range(40))
    parts = split_text(text, CFG)
    assert len(parts) > 1
    assert all(len(p) <= CFG.leaf_max_chars for p in parts)


def test_consecutive_chunks_overlap() -> None:
    text = "甲" * 1200
    parts = split_text(text, CFG)
    assert len(parts) >= 3
    assert parts[0][-CFG.overlap_chars:] == parts[1][: CFG.overlap_chars]


def test_split_terminates_and_covers_all_text() -> None:
    """重叠切分最容易写出死循环或丢尾巴。"""
    text = "乙" * 997
    parts = split_text(text, CFG)
    assert len(parts) < 20
    assert parts[-1].endswith("乙")


def test_table_block_is_never_split_however_long() -> None:
    long_table = "| 列 | 值 |\n" + "\n".join(f"| 行{i} | {i} |" for i in range(400))
    chunks = chunk_document(
        _doc([NormalizedBlock(0, "table", "第三节 > 分部收入", long_table, page=13)]), CFG
    )
    assert len(chunks) == 1
    assert chunks[0].content == long_table
    assert len(chunks[0].content) > CFG.leaf_max_chars


def test_chunks_do_not_span_sections() -> None:
    """跨小节的块会混入不相关内容，稀释 BM25 与向量信号。"""
    chunks = chunk_document(
        _doc(
            [
                NormalizedBlock(0, "paragraph", "第一节 概况", "丙" * 150),
                NormalizedBlock(1, "paragraph", "第二节 业务", "丁" * 150),
            ]
        ),
        CFG,
    )
    assert len(chunks) == 2
    assert {c.section_path for c in chunks} == {"第一节 概况", "第二节 业务"}


def test_section_path_is_prepended_to_content() -> None:
    """块自带章节上下文，对 BM25 与嵌入都有增益（05 §3.1）。"""
    chunks = chunk_document(
        _doc([NormalizedBlock(0, "paragraph", "第三节 主营业务", "戊" * 100)]), CFG
    )
    assert chunks[0].content.startswith("第三节 主营业务")


def test_ordinals_are_contiguous_from_zero() -> None:
    chunks = chunk_document(
        _doc(
            [
                NormalizedBlock(0, "paragraph", "第一节", "己" * 900),
                NormalizedBlock(1, "table", "第一节", "| a | b |"),
            ]
        ),
        CFG,
    )
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_boilerplate_is_recognised() -> None:
    assert is_boilerplate("第 12 页 共 24 页")
    assert is_boilerplate("目 录")
    assert is_boilerplate("本公司及董事会全体成员保证信息披露内容的真实、准确和完整")
    assert not is_boilerplate("报告期内营业收入 12,340 万元，同比增长 58.2%。")


def test_boilerplate_blocks_are_dropped_when_enabled() -> None:
    chunks = chunk_document(
        _doc(
            [
                NormalizedBlock(0, "paragraph", "", "第 12 页 共 24 页"),
                NormalizedBlock(1, "paragraph", "第一节", "庚" * 100),
            ]
        ),
        CFG,
    )
    assert len(chunks) == 1
    assert "第 12 页" not in chunks[0].content


def test_boilerplate_kept_when_disabled() -> None:
    cfg = ChunkConfig(drop_boilerplate=False)
    chunks = chunk_document(
        _doc([NormalizedBlock(0, "paragraph", "", "第 12 页 共 24 页")]), cfg
    )
    assert len(chunks) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/parse/test_chunker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.parse.chunker'`

- [ ] **Step 3: 写最小实现**

`packages/ragdemo/src/ragdemo/parse/filters.py`：

```python
"""不进入检索索引的内容。

过滤过度会丢召回，因此规则保守且可配置，变更必须跑检索评测（05 §3.3）。
"""
from __future__ import annotations

import re

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^第\s*\d+\s*页\s*共\s*\d+\s*页$"),
    re.compile(r"^-?\s*\d+\s*-?$"),                      # 孤立页码
    re.compile(r"^目\s*录$"),
    re.compile(r"^释\s*义$"),
    re.compile(r"保证信息披露内容的真实、准确和完整"),      # 各家雷同的免责模板
    re.compile(r"^本报告期内公司不存在.{0,20}情形$"),
)


def is_boilerplate(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return any(p.search(stripped) for p in _PATTERNS)
```

`packages/ragdemo/src/ragdemo/parse/chunker.py`：

```python
"""切块器。纯函数：NormalizedDocument -> list[Chunk]，不碰数据库、不调网络。

三条硬规则（docs/05-document-pipeline.md §3）：
1. 表格块永不切分，无论多长——切开的表格无法理解；
2. 块不跨小节——跨小节会混入不相关内容，稀释检索信号；
3. 块首拼接 section_path——让块自带上下文。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.filters import is_boilerplate

_SENTENCE_END = re.compile(r"(?<=[。！？；\n])")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    block_type: str
    section_path: str
    content: str
    page: int | None
    bbox: tuple[float, float, float, float] | None
    is_leaf: bool
    parent_ordinal: int | None = None


def split_text(text: str, cfg: ChunkConfig) -> list[str]:
    """按句边界切，尽量落在 [leaf_min, leaf_max] 区间，相邻块重叠 overlap_chars。"""
    cfg.validate()
    stripped = text.strip()
    if len(stripped) <= cfg.leaf_max_chars:
        return [stripped] if stripped else []

    sentences = [s for s in _SENTENCE_END.split(stripped) if s]
    parts: list[str] = []
    buffer = ""

    for sentence in sentences:
        while len(sentence) > cfg.leaf_max_chars:
            # 单句超长（长表格行、无标点的长串）：硬切，保证收敛
            head, sentence = sentence[: cfg.leaf_max_chars], sentence[cfg.leaf_max_chars :]
            if buffer:
                parts.append(buffer)
                buffer = ""
            parts.append(head)
        if len(buffer) + len(sentence) > cfg.leaf_max_chars and buffer:
            parts.append(buffer)
            buffer = buffer[-cfg.overlap_chars :] if cfg.overlap_chars else ""
        buffer += sentence

    if buffer.strip():
        parts.append(buffer)
    return [p for p in parts if p.strip()]


def chunk_document(doc: NormalizedDocument, cfg: ChunkConfig) -> list[Chunk]:
    """产出叶子块。父块由 parse.tree 构造。"""
    cfg.validate()
    chunks: list[Chunk] = []
    ordinal = 0

    for block in doc.blocks:
        if cfg.drop_boilerplate and block.block_type != "table" and is_boilerplate(block.content):
            continue

        if block.block_type == "table" and cfg.keep_table_whole:
            chunks.append(
                Chunk(
                    ordinal=ordinal, block_type="table", section_path=block.section_path,
                    content=block.content, page=block.page, bbox=block.bbox, is_leaf=True,
                )
            )
            ordinal += 1
            continue

        if block.block_type == "title":
            continue  # 标题不单独成块，它以 section_path 的形式进入每个块

        for piece in split_text(block.content, cfg):
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    block_type=block.block_type,
                    section_path=block.section_path,
                    content=_with_section_prefix(block.section_path, piece),
                    page=block.page,
                    bbox=block.bbox,
                    is_leaf=True,
                )
            )
            ordinal += 1

    return chunks


def _with_section_prefix(section_path: str, text: str) -> str:
    return f"{section_path}\n{text}" if section_path else text
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/parse/test_chunker.py -v`
Expected: 11 passed

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo/src/ragdemo/parse/chunker.py packages/ragdemo/src/ragdemo/parse/filters.py tests/parse/test_chunker.py
git commit -m "feat(parse): 切块器，表格不切分且块不跨小节"
```

---

## Task 3: 父子块构造与 `is_leaf`

**Files:**
- Create: `packages/ragdemo/src/ragdemo/parse/tree.py`, `tests/parse/test_tree.py`

**Interfaces:**
- Consumes: Task 2 的 `Chunk`
- Produces:
  - `build_tree(leaves: list[Chunk]) -> list[Chunk]`（返回父块在前、叶子块在后的完整列表，`ordinal` 重排且连续，叶子的 `parent_ordinal` 指向父块）

- [ ] **Step 1: 写失败的测试**

`tests/parse/test_tree.py`：

```python
"""父子块：同小节的叶子共享一个父块；表格既是父块也是叶子。"""
from __future__ import annotations

from ragdemo.parse.chunker import Chunk
from ragdemo.parse.tree import build_tree


def _leaf(ordinal: int, section: str, content: str, block_type: str = "paragraph") -> Chunk:
    return Chunk(
        ordinal=ordinal, block_type=block_type, section_path=section,
        content=content, page=1, bbox=None, is_leaf=True,
    )


def test_same_section_leaves_share_one_parent() -> None:
    tree = build_tree([_leaf(0, "第一节", "甲"), _leaf(1, "第一节", "乙")])
    parents = [c for c in tree if not c.is_leaf]
    leaves = [c for c in tree if c.is_leaf]
    assert len(parents) == 1
    assert {leaf.parent_ordinal for leaf in leaves} == {parents[0].ordinal}


def test_different_sections_get_different_parents() -> None:
    tree = build_tree([_leaf(0, "第一节", "甲"), _leaf(1, "第二节", "乙")])
    assert len({c.ordinal for c in tree if not c.is_leaf}) == 2


def test_parent_content_is_the_concatenated_section() -> None:
    tree = build_tree([_leaf(0, "第一节", "甲甲"), _leaf(1, "第一节", "乙乙")])
    parent = next(c for c in tree if not c.is_leaf)
    assert "甲甲" in parent.content and "乙乙" in parent.content


def test_table_is_both_leaf_and_parentless() -> None:
    """表格既是父块也是叶子块——它不能切，也不能只给片段（05 §4.2 规则 3）。"""
    tree = build_tree([_leaf(0, "第一节", "| a | b |", block_type="table")])
    assert len(tree) == 1
    table = tree[0]
    assert table.is_leaf is True
    assert table.parent_ordinal is None


def test_is_leaf_cannot_be_derived_from_parent_ordinal() -> None:
    """这正是需要显式 is_leaf 列的原因：表格的 parent_ordinal 是 None 但它可被召回。"""
    tree = build_tree(
        [_leaf(0, "第一节", "甲"), _leaf(1, "第一节", "乙"), _leaf(2, "第二节", "| a |", "table")]
    )
    parentless = [c for c in tree if c.parent_ordinal is None]
    assert any(c.is_leaf for c in parentless)      # 表格
    assert any(not c.is_leaf for c in parentless)  # 父块


def test_ordinals_are_reassigned_contiguously() -> None:
    tree = build_tree([_leaf(0, "第一节", "甲"), _leaf(1, "第二节", "乙")])
    assert sorted(c.ordinal for c in tree) == list(range(len(tree)))


def test_single_leaf_section_still_gets_a_parent() -> None:
    """即使小节只有一个叶子，也建父块——生成端的接口才能一致。"""
    tree = build_tree([_leaf(0, "第一节", "甲")])
    assert len(tree) == 2
    assert sum(1 for c in tree if not c.is_leaf) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/parse/test_tree.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.parse.tree'`

- [ ] **Step 3: 写最小实现**

`packages/ragdemo/src/ragdemo/parse/tree.py`：

```python
"""父子块构造。

检索与生成对块长度要求相反：检索要短（信号集中），生成要长（上下文完整）。
父子块让两者各取所需（docs/05-document-pipeline.md §4）。

is_leaf 必须是显式的列：表格块既是父块也是叶子块，用「有没有父块」判断
会把所有表格排除出召回。
"""
from __future__ import annotations

from ragdemo.parse.chunker import Chunk


def build_tree(leaves: list[Chunk]) -> list[Chunk]:
    """为每个小节建一个父块，返回父块 + 叶子块的完整列表，ordinal 重排。

    表格块直接透传：它既是叶子（可召回）也没有父块（不能只给片段）。
    """
    out: list[Chunk] = []
    next_ordinal = 0
    section_order: list[str] = []
    by_section: dict[str, list[Chunk]] = {}

    for leaf in leaves:
        if leaf.block_type == "table":
            out.append(_with_ordinal(leaf, next_ordinal, parent=None, is_leaf=True))
            next_ordinal += 1
            continue
        if leaf.section_path not in by_section:
            by_section[leaf.section_path] = []
            section_order.append(leaf.section_path)
        by_section[leaf.section_path].append(leaf)

    for section in section_order:
        members = by_section[section]
        parent_ordinal = next_ordinal
        out.append(
            Chunk(
                ordinal=parent_ordinal,
                block_type="paragraph",
                section_path=section,
                content="\n".join(m.content for m in members),
                page=members[0].page,
                bbox=None,
                is_leaf=False,
                parent_ordinal=None,
            )
        )
        next_ordinal += 1
        for member in members:
            out.append(_with_ordinal(member, next_ordinal, parent=parent_ordinal, is_leaf=True))
            next_ordinal += 1

    return out


def _with_ordinal(chunk: Chunk, ordinal: int, *, parent: int | None, is_leaf: bool) -> Chunk:
    return Chunk(
        ordinal=ordinal,
        block_type=chunk.block_type,
        section_path=chunk.section_path,
        content=chunk.content,
        page=chunk.page,
        bbox=chunk.bbox,
        is_leaf=is_leaf,
        parent_ordinal=parent,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/parse/test_tree.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo/src/ragdemo/parse/tree.py tests/parse/test_tree.py
git commit -m "feat(parse): 父子块构造，表格既是父块也是叶子块"
```

---

## Task 4: 表格描述生成

**Files:**
- Create: `packages/ragdemo/src/ragdemo/parse/describe.py`, `packages/ragdemo/src/ragdemo/agents/prompts/table_describe/v1.md`, `tests/parse/test_describe.py`

**Interfaces:**
- Consumes: Task 2 的 `Chunk`
- Produces:
  - `TableDescriber` Protocol：`model: str`、`prompt_version: str`、`describe(table_markdown: str, *, title: str, section_path: str) -> str`
  - `MockTableDescriber`（确定性输出，从表头推导）
  - `DescriptionRejected(ValueError)`
  - `validate_description(text: str) -> str`（拦截含解读或推断的描述）

- [ ] **Step 1: 写失败的测试**

`tests/parse/test_describe.py`：

```python
"""表格描述：只描述表里有什么，不做任何解读或数值推断。"""
from __future__ import annotations

import pytest

from ragdemo.parse.describe import (
    DescriptionRejected,
    MockTableDescriber,
    validate_description,
)

TABLE = (
    "| 业务分部 | 2024H1 收入(百万元) | 同比 |\n"
    "| 智能计算 | 12,340 | +58.2% |\n"
    "| 通信设备 | 8,120 | -3.1% |"
)


def test_mock_description_mentions_the_columns() -> None:
    desc = MockTableDescriber().describe(TABLE, title="三季报", section_path="第三节 > 分部收入")
    assert "业务分部" in desc
    assert "2 行" in desc


def test_description_is_deterministic() -> None:
    d = MockTableDescriber()
    assert d.describe(TABLE, title="t", section_path="s") == d.describe(
        TABLE, title="t", section_path="s"
    )


def test_interpretation_words_are_rejected() -> None:
    """描述句的作用是让表格能被语义命中，不是替读者下结论。"""
    for bad in ("显示公司业绩大幅改善", "说明增长强劲", "表明景气度回升", "预计将继续增长"):
        with pytest.raises(DescriptionRejected):
            validate_description(f"分部收入表，{bad}。")


def test_compliance_forbidden_words_are_rejected() -> None:
    with pytest.raises(DescriptionRejected):
        validate_description("分部收入表，建议买入。")


def test_plain_description_passes() -> None:
    text = "2024 年上半年分部收入表，含智能计算、通信设备 2 个分部的收入与同比增速。"
    assert validate_description(text) == text


def test_empty_description_is_rejected() -> None:
    with pytest.raises(DescriptionRejected):
        validate_description("   ")


def test_describer_exposes_prompt_version() -> None:
    """prompt 版本要进 tool_call_log，否则无法定位是哪个版本产出的描述。"""
    d = MockTableDescriber()
    assert d.prompt_version
    assert d.model
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/parse/test_describe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.parse.describe'`

- [ ] **Step 3: 写最小实现**

`packages/ragdemo/src/ragdemo/agents/prompts/table_describe/v1.md`：

```markdown
---
version: v1
model_tier: small
expected_output_schema: plain_text_one_sentence
---

你的任务是为一张表格写一句自然语言描述，用于检索召回。

**只描述表里有什么，不做任何解读、比较或推断。**

必须包含：期间（若表中可见）、表格主题、主要维度名、行数。
禁止出现：对趋势的评价（改善、恶化、强劲、疲软）、对原因的推测、
对未来的预期、任何买卖建议或评级用词。

表格标题：{title}
章节路径：{section_path}
表格内容：
{table_markdown}

只输出那一句描述，不要解释。
```

`packages/ragdemo/src/ragdemo/parse/describe.py`：

```python
"""表格描述生成。

纯数字表格的向量表示很弱，这句描述是它被语义检索命中的主要途径
（docs/05-document-pipeline.md §3.2）。

描述必须只陈述表里有什么。让小模型顺手「总结一下」，等于在检索层就
掺进了未经验证、无来源的判断——而输出中每个论断都必须可溯源。
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

INTERPRETATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(显示|说明|表明|反映|意味着|体现出)"),
    re.compile(r"(改善|恶化|强劲|疲软|亮眼|承压|超预期|不及预期)"),
    re.compile(r"(预计|预期|有望|将会|料将)"),
    re.compile(r"(大幅|显著|明显)(增长|下滑|提升|下降)"),
)

FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(买入|卖出|增持|减持|推荐|目标价|评级|建议配置|仓位)"),
)


class DescriptionRejected(ValueError):
    """描述含解读、推断或违禁词。"""


def validate_description(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise DescriptionRejected("描述为空")
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(stripped):
            raise DescriptionRejected(f"描述含违禁词: {pattern.pattern}")
    for pattern in INTERPRETATION_PATTERNS:
        if pattern.search(stripped):
            raise DescriptionRejected(f"描述含解读而非陈述: {pattern.pattern}")
    return stripped


@runtime_checkable
class TableDescriber(Protocol):
    model: str
    prompt_version: str

    def describe(self, table_markdown: str, *, title: str, section_path: str) -> str: ...


class MockTableDescriber:
    """确定性描述器。从表头与行数推导，不调模型——测试与离线开发用。"""

    model = "mock"
    prompt_version = "v1"

    def describe(self, table_markdown: str, *, title: str, section_path: str) -> str:
        rows = [r for r in table_markdown.splitlines() if r.strip().startswith("|")]
        if not rows:
            raise DescriptionRejected("不是 Markdown 表格")
        headers = [c.strip() for c in rows[0].strip("| ").split("|") if c.strip()]
        body_count = max(len(rows) - 1, 0)
        leaf_section = section_path.split(">")[-1].strip() or title
        return validate_description(
            f"{leaf_section}表，列为 {'、'.join(headers)}，共 {body_count} 行。"
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/parse/test_describe.py -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo/src/ragdemo/parse/describe.py packages/ragdemo/src/ragdemo/agents/prompts/table_describe tests/parse/test_describe.py
git commit -m "feat(parse): 表格描述生成，拦截解读性表述"
```

---

## Task 5: 元数据完整性校验

**Files:**
- Create: `packages/ragdemo/src/ragdemo/parse/validate.py`, `tests/parse/test_validate.py`

**Interfaces:**
- Consumes: Task 2/3 的 `Chunk`；P1a Task 8 的 `NormalizedDocument`
- Produces:
  - `MetadataViolation(str)` 类型别名
  - `validate_chunks(doc: NormalizedDocument, chunks: list[Chunk]) -> list[MetadataViolation]`（8 项，返回空列表表示通过）

- [ ] **Step 1: 写失败的测试**

`tests/parse/test_validate.py`：

```python
"""元数据校验 8 项。任一项不过，整份文档回滚——部分入库比没入库更危险。"""
from __future__ import annotations

from datetime import UTC, datetime

from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument
from ragdemo.parse.chunker import Chunk
from ragdemo.parse.validate import validate_chunks


def _doc(page_count: int = 24) -> NormalizedDocument:
    return NormalizedDocument(
        provider_doc_id="D1", entity_ref="688256.SH", doc_type="quarterly",
        title="三季报", period="2024Q3",
        publish_at=datetime(2024, 10, 28, 18, 32, tzinfo=UTC),
        language="zh", source_url=None, raw_bytes_ref=None, content_hash="h",
        is_correction=False, supersedes_provider_doc_id=None, page_count=page_count,
        blocks=[NormalizedBlock(0, "paragraph", "第一节", "内容")],
    )


def _chunk(**kw: object) -> Chunk:
    base = dict(
        ordinal=0, block_type="paragraph", section_path="第一节", content="内容",
        page=1, bbox=None, is_leaf=True, parent_ordinal=None,
    )
    base.update(kw)
    return Chunk(**base)  # type: ignore[arg-type]


def test_valid_chunks_pass() -> None:
    chunks = [
        _chunk(ordinal=0, is_leaf=False),
        _chunk(ordinal=1, is_leaf=True, parent_ordinal=0),
    ]
    assert validate_chunks(_doc(), chunks) == []


def test_non_contiguous_ordinals_are_caught() -> None:
    chunks = [_chunk(ordinal=0, is_leaf=False), _chunk(ordinal=2, parent_ordinal=0)]
    assert any("ordinal" in v for v in validate_chunks(_doc(), chunks))


def test_empty_content_is_caught() -> None:
    assert any("content" in v for v in validate_chunks(_doc(), [_chunk(content="   ")]))


def test_table_without_description_is_caught() -> None:
    """表格块没有 content_desc 就无法被语义命中。"""
    chunks = [_chunk(block_type="table", content="| a |")]
    assert any("content_desc" in v for v in validate_chunks(_doc(), chunks, descriptions={}))


def test_parent_pointing_outside_the_document_is_caught() -> None:
    assert any("parent" in v for v in validate_chunks(_doc(), [_chunk(parent_ordinal=99)]))


def test_nesting_deeper_than_two_levels_is_caught() -> None:
    """只允许两层。父块自己有父块说明构造出错了。"""
    chunks = [
        _chunk(ordinal=0, is_leaf=False, parent_ordinal=None),
        _chunk(ordinal=1, is_leaf=False, parent_ordinal=0),
        _chunk(ordinal=2, is_leaf=True, parent_ordinal=1),
    ]
    assert any("两层" in v for v in validate_chunks(_doc(), chunks))


def test_page_out_of_range_is_caught() -> None:
    assert any("page" in v for v in validate_chunks(_doc(page_count=5), [_chunk(page=99)]))


def test_leaf_with_children_is_caught() -> None:
    """is_leaf = true 的块不能有子块。"""
    chunks = [_chunk(ordinal=0, is_leaf=True), _chunk(ordinal=1, parent_ordinal=0)]
    assert any("is_leaf" in v for v in validate_chunks(_doc(), chunks))


def test_non_leaf_without_children_is_caught() -> None:
    chunks = [_chunk(ordinal=0, is_leaf=False)]
    assert any("is_leaf" in v for v in validate_chunks(_doc(), chunks))


def test_all_violations_are_reported_not_just_the_first() -> None:
    """一次看到全部问题，避免修一个跑一次。"""
    chunks = [_chunk(ordinal=3, content="  ", page=99)]
    assert len(validate_chunks(_doc(page_count=5), chunks)) >= 3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/parse/test_validate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.parse.validate'`

- [ ] **Step 3: 写最小实现**

`packages/ragdemo/src/ragdemo/parse/validate.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/parse/test_validate.py -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo/src/ragdemo/parse/validate.py tests/parse/test_validate.py
git commit -m "feat(parse): 元数据完整性校验 8 项，一次报全部违规"
```

---

## Task 6: 文档与块入库

**Files:**
- Create: `packages/ragdemo/src/ragdemo/ingest/documents.py`, `tests/ingest/test_documents.py`

**Interfaces:**
- Consumes: Task 2/3/4/5；P1a Task 8 的 `NormalizedDocument` 与 `known_at_for`
- Produces:
  - `DocumentWriter(conn, *, ingest_run_id: str, source: str, disclosure_lag: timedelta = timedelta(0))`
    - `write_document(doc, chunks, descriptions, *, artifacts: ParseArtifacts | None = None) -> DocumentWriteResult`
    - `reparse_document(doc, chunks, descriptions, *, supersedes_doc_id: int, artifacts: ParseArtifacts | None = None) -> DocumentWriteResult`
  - `DocumentWriteResult(doc_id: int, block_ids: list[int], skipped: bool)`
  - `ParseArtifacts(engine: str, json_ref: str | None, md_ref: str | None, warnings: list[str])`
  - `MetadataInvalid(RuntimeError)`

`ParseArtifacts` 定义在这里而不是 `parse/` 包里，是为了保持依赖方向：
`ingest` 可以 import `parse`，反过来不行（`01-architecture.md` §6）。
Task 9 的解析器产出 `ParseResult`，由 Task 10 的资产转成 `ParseArtifacts` 交给写入器。

- [ ] **Step 1: 写失败的测试**

`tests/ingest/test_documents.py`：

```python
"""文档入库：反规范化列一致、整份回滚、重解析不改 known_at。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from ragdemo.adapters.base import FetchContext
from ragdemo_core.db.migrate import migrate
from ragdemo.ingest.documents import DocumentWriter, MetadataInvalid
from ragdemo.parse.chunker import Chunk, chunk_document
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.tree import build_tree

MIGRATIONS = Path("db/migrations")
CFG = ChunkConfig()


def _docs() -> list[object]:
    p = MockAnnouncementProvider()
    ctx = FetchContext(ingest_run_id="t", partition_date=datetime.now(UTC).date())
    return [
        p.normalize(raw)
        for raw in p.list_documents(
            ctx, since=datetime(2024, 1, 1, tzinfo=UTC), until=datetime.now(UTC)
        )
    ]


def _prepare(doc: object) -> tuple[list[Chunk], dict[int, str]]:
    chunks = build_tree(chunk_document(doc, CFG))  # type: ignore[arg-type]
    describer = MockTableDescriber()
    descriptions = {
        c.ordinal: describer.describe(c.content, title="t", section_path=c.section_path)
        for c in chunks
        if c.block_type == "table"
    }
    return chunks, descriptions


@pytest.fixture()
def writer(temp_db: str) -> DocumentWriter:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.commit()
    return DocumentWriter(conn, ingest_run_id="r1", source="mock-announcements")


@pytest.mark.db
def test_document_and_blocks_are_written(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    assert result.doc_id > 0
    assert len(result.block_ids) == len(chunks)


@pytest.mark.db
def test_denormalised_columns_match_the_document(writer: DocumentWriter) -> None:
    """02 §5.3 的反规范化列必须与 document 完全一致，否则检索过滤会错。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    (mismatches,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block b JOIN core.document d USING (doc_id) "
        " WHERE b.entity_id IS DISTINCT FROM d.entity_id"
        "    OR b.doc_type IS DISTINCT FROM d.doc_type"
        "    OR b.known_at IS DISTINCT FROM d.known_at"
        "    OR b.publish_at IS DISTINCT FROM d.publish_at"
    ).fetchone()  # type: ignore[misc]
    assert mismatches == 0


@pytest.mark.db
def test_known_at_applies_disclosure_lag(temp_db: str) -> None:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.commit()
    w = DocumentWriter(
        conn, ingest_run_id="r1", source="mock", disclosure_lag=timedelta(days=1)
    )
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    w.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    publish_at, known_at = conn.execute(
        "SELECT publish_at, known_at FROM core.document"
    ).fetchone()  # type: ignore[misc]
    assert known_at - publish_at == timedelta(days=1)


@pytest.mark.db
def test_duplicate_content_hash_is_skipped(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    second = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    assert second.skipped is True
    (n,) = writer.conn.execute("SELECT count(*) FROM core.document").fetchone()  # type: ignore[misc]
    assert n == 1


@pytest.mark.db
def test_invalid_metadata_rolls_back_the_whole_document(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    broken = [*chunks[:-1]]  # 去掉最后一块，制造 ordinal 不连续
    with pytest.raises(MetadataInvalid):
        writer.write_document(doc, broken, desc)  # type: ignore[arg-type]
    (docs, blocks) = writer.conn.execute(
        "SELECT (SELECT count(*) FROM core.document), (SELECT count(*) FROM core.doc_block)"
    ).fetchone()  # type: ignore[misc]
    assert (docs, blocks) == (0, 0)


@pytest.mark.db
def test_reparse_keeps_original_known_at(writer: DocumentWriter) -> None:
    """重解析不改变这份文档在现实中何时可知——改了它就会从历史回测中消失。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    (original_known_at,) = writer.conn.execute(
        "SELECT known_at FROM core.document WHERE doc_id = %s", (first.doc_id,)
    ).fetchone()  # type: ignore[misc]

    second = writer.reparse_document(
        doc, chunks, desc, supersedes_doc_id=first.doc_id  # type: ignore[arg-type]
    )
    new_known_at, version_group, supersedes = writer.conn.execute(
        "SELECT known_at, version_group_id, supersedes_doc_id FROM core.document"
        " WHERE doc_id = %s",
        (second.doc_id,),
    ).fetchone()  # type: ignore[misc]

    assert new_known_at == original_known_at
    assert version_group == first.doc_id
    assert supersedes == first.doc_id


@pytest.mark.db
def test_reparse_marks_old_blocks_superseded_but_keeps_them(writer: DocumentWriter) -> None:
    """旧块必须保留——历史观点的 evidence_blocks 引用着它们。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    writer.reparse_document(doc, chunks, desc, supersedes_doc_id=first.doc_id)  # type: ignore[arg-type]

    (old_alive,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE doc_id = %s", (first.doc_id,)
    ).fetchone()  # type: ignore[misc]
    (old_superseded,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE doc_id = %s AND superseded_at IS NOT NULL",
        (first.doc_id,),
    ).fetchone()  # type: ignore[misc]
    assert old_alive == old_superseded > 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/ingest/test_documents.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.ingest.documents'`

- [ ] **Step 3: 写最小实现**

`packages/ragdemo/src/ragdemo/ingest/documents.py`：

```python
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
            doc_id = self._insert_document(
                doc,
                known_at=original_known_at,
                version_group_id=int(version_group_id),
                supersedes_doc_id=supersedes_doc_id,
                artifacts=artifacts,
            )
            block_ids = self._insert_blocks(doc, doc_id, chunks, descriptions)
            self.conn.execute(
                "UPDATE core.document SET superseded_at = now() WHERE doc_id = %s",
                (supersedes_doc_id,),
            )
            self.conn.execute(
                "UPDATE core.doc_block SET superseded_at = now() WHERE doc_id = %s",
                (supersedes_doc_id,),
            )
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/ingest/test_documents.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo/src/ragdemo/ingest/documents.py tests/ingest/test_documents.py
git commit -m "feat(ingest): 文档与块入库，重解析沿用原 known_at"
```

---

## Task 7: 嵌入器与内容哈希缓存

**Files:**
- Create: `packages/ragdemo/src/ragdemo/embed/__init__.py`, `packages/ragdemo/src/ragdemo/embed/base.py`, `packages/ragdemo/src/ragdemo/embed/mock.py`, `packages/ragdemo/src/ragdemo/embed/cache.py`, `tests/embed/__init__.py`, `tests/embed/test_base.py`, `tests/embed/test_cache.py`

**Interfaces:**
- Consumes: P0 的 `core.embedding_cache`
- Produces:
  - `EMBEDDING_DIM = 1024`
  - `Embedder` Protocol：`model: str`、`embed(texts: Sequence[str]) -> list[list[float]]`
  - `MockEmbedder(model="mock-1024")`（确定性、已归一化）
  - `l2_normalize(vector: Sequence[float]) -> list[float]`
  - `embedding_input(*, doc_title, section_path, content_desc, content, max_chars=2000) -> str`
  - `content_key(text: str) -> str`（sha256）
  - `EmbeddingCache(conn, *, model: str, owner_user: str = "")`：`get_many(keys)`、`put_many(items)`

- [ ] **Step 1: 写失败的测试**

`tests/embed/__init__.py`：空文件。

`tests/embed/test_base.py`：

```python
"""嵌入基础：维度、L2 归一化、输入拼接。"""
from __future__ import annotations

import math

import pytest

from ragdemo.embed.base import EMBEDDING_DIM, embedding_input, l2_normalize
from ragdemo.embed.mock import MockEmbedder


def test_dimension_is_1024() -> None:
    """adr/0004：固定 1024 让 bge-m3 与 Qwen3-Embedding 可互换。"""
    assert EMBEDDING_DIM == 1024


def test_mock_embedder_returns_correct_shape() -> None:
    vectors = MockEmbedder().embed(["甲", "乙"])
    assert len(vectors) == 2
    assert all(len(v) == EMBEDDING_DIM for v in vectors)


def test_mock_embedder_is_deterministic() -> None:
    assert MockEmbedder().embed(["甲"]) == MockEmbedder().embed(["甲"])


def test_different_text_gives_different_vector() -> None:
    a, b = MockEmbedder().embed(["甲", "乙"])
    assert a != b


def test_vectors_are_l2_normalised() -> None:
    """HNSW 用 vector_cosine_ops，两端都必须归一化，否则排序静默出错。"""
    for vector in MockEmbedder().embed(["甲", "乙丙丁"]):
        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, rel_tol=1e-6)


def test_l2_normalize_handles_zero_vector() -> None:
    assert l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


def test_embedding_input_includes_title_and_section() -> None:
    """标题与章节路径提供块本身缺失的上下文（「本期」「上述」无法孤立解析）。"""
    text = embedding_input(
        doc_title="三季报", section_path="第三节 > 分部收入",
        content_desc="分部收入表", content="智能计算 12,340 万元",
    )
    assert "三季报" in text and "第三节" in text and "分部收入表" in text


def test_embedding_input_truncates_content_not_context() -> None:
    """超长时截断正文，保留前面的标题与章节——上下文比尾部正文更值钱。"""
    text = embedding_input(
        doc_title="三季报", section_path="第三节", content_desc="",
        content="甲" * 5000, max_chars=100,
    )
    assert text.startswith("三季报")
    assert len(text) == 100


def test_embedding_input_skips_empty_parts() -> None:
    text = embedding_input(doc_title="三季报", section_path="", content_desc="", content="甲")
    assert text == "三季报\n甲"


def test_embedder_rejects_empty_batch() -> None:
    with pytest.raises(ValueError, match="空"):
        MockEmbedder().embed([])
```

`tests/embed/test_cache.py`：

```python
"""嵌入缓存：主键三列，跨模型与跨用户不串。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate
from ragdemo.embed.base import content_key
from ragdemo.embed.cache import EmbeddingCache
from ragdemo.embed.mock import MockEmbedder

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.commit()
    return c


@pytest.mark.db
def test_put_then_get_roundtrips(conn: psycopg.Connection) -> None:
    cache = EmbeddingCache(conn, model="bge-m3")
    key = content_key("智能计算收入 12,340 万元")
    vector = MockEmbedder().embed(["智能计算收入 12,340 万元"])[0]
    cache.put_many({key: vector})
    got = cache.get_many([key])
    assert len(got[key]) == len(vector)
    assert got[key][:3] == pytest.approx(vector[:3])


@pytest.mark.db
def test_miss_returns_no_entry(conn: psycopg.Connection) -> None:
    assert EmbeddingCache(conn, model="bge-m3").get_many([content_key("没存过")]) == {}


@pytest.mark.db
def test_same_content_different_model_does_not_collide(conn: psycopg.Connection) -> None:
    """换模型后读到旧模型的向量不会报错，只会让检索质量莫名下降。"""
    key = content_key("同一段文本")
    EmbeddingCache(conn, model="bge-m3").put_many({key: MockEmbedder().embed(["a"])[0]})
    assert EmbeddingCache(conn, model="qwen3-embedding").get_many([key]) == {}


@pytest.mark.db
def test_private_content_does_not_leak_into_shared_cache(conn: psycopg.Connection) -> None:
    """09 §3.3：私有内容进共享缓存，可通过哈希命中探测别人上传过什么。"""
    key = content_key("用户私有材料")
    EmbeddingCache(conn, model="bge-m3", owner_user="u1").put_many(
        {key: MockEmbedder().embed(["x"])[0]}
    )
    assert EmbeddingCache(conn, model="bge-m3").get_many([key]) == {}
    assert EmbeddingCache(conn, model="bge-m3", owner_user="u2").get_many([key]) == {}
    assert EmbeddingCache(conn, model="bge-m3", owner_user="u1").get_many([key])


@pytest.mark.db
def test_put_many_is_idempotent(conn: psycopg.Connection) -> None:
    cache = EmbeddingCache(conn, model="bge-m3")
    key = content_key("重复写入")
    vector = MockEmbedder().embed(["y"])[0]
    cache.put_many({key: vector})
    cache.put_many({key: vector})
    (n,) = conn.execute("SELECT count(*) FROM core.embedding_cache").fetchone()  # type: ignore[misc]
    assert n == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/embed -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.embed'`

- [ ] **Step 3: 写最小实现**

`packages/ragdemo/src/ragdemo/embed/__init__.py`：

```python
"""嵌入。维度固定 1024，写入前 L2 归一化。"""
```

`packages/ragdemo/src/ragdemo/embed/base.py`：

```python
"""嵌入协议与工具。

维度固定 1024（adr/0004）：bge-m3 原生 1024，Qwen3-Embedding 经 MRL 降到 1024，
两者可互换而不改表结构。换模型只是数据回填，不是架构变更。
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

EMBEDDING_DIM = 1024
DEFAULT_MAX_EMBED_CHARS = 2000


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """HNSW 索引用 vector_cosine_ops，归一化后余弦距离与内积等价。
    查询端也必须归一化——两端不一致会静默返回错误的排序。"""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return list(vector)
    return [x / norm for x in vector]


def embedding_input(
    *,
    doc_title: str,
    section_path: str,
    content_desc: str,
    content: str,
    max_chars: int = DEFAULT_MAX_EMBED_CHARS,
) -> str:
    """拼接嵌入输入。超长时截断正文而非丢弃前面的上下文——
    「本期」「上述」这类指代在孤立块中无法解析。"""
    parts = [p for p in (doc_title, section_path, content_desc, content) if p]
    return "\n".join(parts)[:max_chars]


def content_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@runtime_checkable
class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """返回与输入等长的向量列表，每个向量 EMBEDDING_DIM 维且已 L2 归一化。"""
```

`packages/ragdemo/src/ragdemo/embed/mock.py`：

```python
"""确定性 Mock 嵌入器。

从内容哈希派生向量，因此同样的文本永远得到同样的向量——
检索评测在 Mock 上可复现，是 P1 能在没有嵌入 API 配额时推进的前提。
"""
from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence

from ragdemo.embed.base import EMBEDDING_DIM, l2_normalize


class MockEmbedder:
    model = "mock-1024"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            raise ValueError("嵌入批次不能为空")
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        raw = b""
        counter = 0
        needed = EMBEDDING_DIM * 4
        while len(raw) < needed:
            raw += hashlib.sha256(f"{text}:{counter}".encode()).digest()
            counter += 1
        floats = struct.unpack(f"<{EMBEDDING_DIM}f", raw[:needed])
        cleaned = [0.0 if (x != x or x in (float("inf"), float("-inf"))) else x for x in floats]
        return l2_normalize(cleaned)
```

`packages/ragdemo/src/ragdemo/embed/cache.py`：

```python
"""内容哈希缓存。

各家公告中大量重复的模板段落只需算一次，对 5000 元/月的预算是实质节省。
主键三列缺一不可，理由见 docs/05-document-pipeline.md §5.3。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import psycopg

from ragdemo.embed.base import EMBEDDING_DIM

PUBLIC_OWNER = ""


class EmbeddingCache:
    def __init__(self, conn: psycopg.Connection, *, model: str, owner_user: str = PUBLIC_OWNER) -> None:
        self.conn = conn
        self.model = model
        self.owner_user = owner_user

    def get_many(self, keys: Sequence[str]) -> dict[str, list[float]]:
        if not keys:
            return {}
        rows = self.conn.execute(
            "SELECT content_hash, embedding FROM core.embedding_cache "
            " WHERE model = %s AND owner_user = %s AND content_hash = ANY(%s)",
            (self.model, self.owner_user, list(keys)),
        ).fetchall()
        return {str(r[0]): _to_floats(r[1]) for r in rows}

    def put_many(self, items: Mapping[str, Sequence[float]]) -> int:
        written = 0
        with self.conn.transaction():
            for key, vector in items.items():
                if len(vector) != EMBEDDING_DIM:
                    raise ValueError(f"向量维度应为 {EMBEDDING_DIM}，收到 {len(vector)}")
                self.conn.execute(
                    "INSERT INTO core.embedding_cache (content_hash, model, owner_user,"
                    " embedding) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (content_hash, model, owner_user) DO NOTHING",
                    (key, self.model, self.owner_user, _to_literal(vector)),
                )
                written += 1
        return written


def _to_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _to_floats(value: object) -> list[float]:
    if isinstance(value, str):
        return [float(x) for x in value.strip("[]").split(",")]
    return [float(x) for x in value]  # type: ignore[union-attr]
```

> **注意**：这里的 `ON CONFLICT DO NOTHING` 是本项目唯一允许的 `ON CONFLICT`
> 用法——`embedding_cache` 不是时点表，缓存命中与否不改变任何事实语义。
> 时点表上仍然禁止 `ON CONFLICT`（Global Constraints）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/embed -v`
Expected: 15 passed

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo/src/ragdemo/embed tests/embed
git commit -m "feat(embed): 嵌入协议、L2 归一化与三列主键的内容哈希缓存"
```

---

## Task 8: 批量嵌入与断点续传

**Files:**
- Create: `packages/ragdemo/src/ragdemo/embed/batch.py`, `tests/embed/test_batch.py`

**Interfaces:**
- Consumes: Task 7 的 `Embedder` / `EmbeddingCache`；P0 的 `core.doc_block`
- Produces:
  - `EmbedStats(pending: int, from_cache: int, computed: int, written: int)`
  - `embed_pending_blocks(conn, embedder, *, batch_size=64, limit=None, owner_user="") -> EmbedStats`

- [ ] **Step 1: 写失败的测试**

`tests/embed/test_batch.py`：

```python
"""批量嵌入：只处理 embedding IS NULL，命中缓存不重算，可中断可续跑。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate
from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.mock import MockEmbedder

MIGRATIONS = Path("db/migrations")


class CountingEmbedder(MockEmbedder):
    """统计实际调用次数，用于验证缓存与批大小。"""

    def __init__(self) -> None:
        self.batches = 0
        self.texts = 0

    def embed(self, texts):  # type: ignore[no-untyped-def]
        self.batches += 1
        self.texts += len(texts)
        return super().embed(texts)


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片')"
    )
    c.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at,"
        " source, content_hash, version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (1,'CN.688256','quarterly','三季报',"
        " '2024-10-28 18:32+08','mock','h1',1,'2024-07-01','2024-10-28 18:32+08','r1')"
    )
    return c


def _add_blocks(conn: psycopg.Connection, contents: list[str], *, is_leaf: bool = True) -> None:
    for i, content in enumerate(contents):
        conn.execute(
            "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal,"
            " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at,"
            " source, ingest_run_id) "
            "VALUES (1,'paragraph','第一节',%s,%s,%s,'CN.688256','quarterly',"
            " '2024-10-28 18:32+08','2024-07-01','2024-10-28 18:32+08','mock','r1')",
            (i, content, is_leaf),
        )
    conn.commit()


@pytest.mark.db
def test_embeds_all_pending_leaf_blocks(conn: psycopg.Connection) -> None:
    _add_blocks(conn, ["甲", "乙", "丙"])
    stats = embed_pending_blocks(conn, MockEmbedder())
    assert stats.written == 3
    (remaining,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE is_leaf AND embedding IS NULL"
    ).fetchone()  # type: ignore[misc]
    assert remaining == 0


@pytest.mark.db
def test_parent_blocks_are_not_embedded(conn: psycopg.Connection) -> None:
    """父块不做嵌入（05 §4.2 规则 4）。"""
    _add_blocks(conn, ["父块内容"], is_leaf=False)
    stats = embed_pending_blocks(conn, MockEmbedder())
    assert stats.pending == 0
    assert stats.written == 0


@pytest.mark.db
def test_rerun_after_interruption_only_handles_remaining(conn: psycopg.Connection) -> None:
    """断点续传：第一次只处理 2 条，第二次处理剩下的。"""
    _add_blocks(conn, ["甲", "乙", "丙", "丁"])
    first = embed_pending_blocks(conn, MockEmbedder(), limit=2)
    second = embed_pending_blocks(conn, MockEmbedder())
    assert (first.written, second.written) == (2, 2)


@pytest.mark.db
def test_identical_content_hits_cache_and_is_computed_once(conn: psycopg.Connection) -> None:
    """各家公告中大量重复的模板段落只算一次。"""
    _add_blocks(conn, ["完全相同的模板段落"] * 5)
    embedder = CountingEmbedder()
    stats = embed_pending_blocks(conn, embedder)
    assert stats.written == 5
    assert embedder.texts == 1, "相同内容只应调用一次嵌入"
    assert stats.from_cache == 4


@pytest.mark.db
def test_batch_size_is_respected(conn: psycopg.Connection) -> None:
    _add_blocks(conn, [f"内容{i}" for i in range(10)])
    embedder = CountingEmbedder()
    embed_pending_blocks(conn, embedder, batch_size=3)
    assert embedder.batches == 4  # 3 + 3 + 3 + 1


@pytest.mark.db
def test_written_vectors_are_normalised(conn: psycopg.Connection) -> None:
    _add_blocks(conn, ["甲"])
    embed_pending_blocks(conn, MockEmbedder())
    (norm,) = conn.execute(
        "SELECT round((embedding <#> embedding * -1)::numeric, 4) FROM core.doc_block"
        " WHERE embedding IS NOT NULL"
    ).fetchone()  # type: ignore[misc]
    assert float(norm) == pytest.approx(1.0, abs=1e-3)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/embed/test_batch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.embed.batch'`

- [ ] **Step 3: 写最小实现**

`packages/ragdemo/src/ragdemo/embed/batch.py`：

```python
"""批量嵌入与断点续传。

待办队列就是 `embedding IS NULL AND is_leaf` —— 不需要额外的状态表，
中断后重跑自然只处理剩余部分（docs/05-document-pipeline.md §5.3）。

P1 首次入库有数十万块，托管 API 有速率限制，所以批处理 + 内容哈希缓存
不是优化，是能否完成 P1 的前提（adr/0004 的后果 1）。
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ragdemo.embed.base import Embedder, content_key, embedding_input
from ragdemo.embed.cache import PUBLIC_OWNER, EmbeddingCache

DEFAULT_BATCH_SIZE = 64


@dataclass(frozen=True)
class EmbedStats:
    pending: int
    from_cache: int
    computed: int
    written: int


def embed_pending_blocks(
    conn: psycopg.Connection,
    embedder: Embedder,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    owner_user: str = PUBLIC_OWNER,
) -> EmbedStats:
    """为尚未嵌入的叶子块生成向量并写回。"""
    rows = conn.execute(
        "SELECT b.block_id, d.title, b.section_path, b.content_desc, b.content "
        "  FROM core.doc_block b JOIN core.document d USING (doc_id) "
        " WHERE b.is_leaf AND b.embedding IS NULL "
        " ORDER BY b.block_id "
        + ("LIMIT %s" if limit is not None else ""),
        (limit,) if limit is not None else (),
    ).fetchall()

    if not rows:
        return EmbedStats(pending=0, from_cache=0, computed=0, written=0)

    inputs: dict[int, str] = {
        int(r[0]): embedding_input(
            doc_title=str(r[1] or ""),
            section_path=str(r[2] or ""),
            content_desc=str(r[3] or ""),
            content=str(r[4]),
        )
        for r in rows
    }
    keys = {block_id: content_key(text) for block_id, text in inputs.items()}

    cache = EmbeddingCache(conn, model=embedder.model, owner_user=owner_user)
    cached = cache.get_many(sorted(set(keys.values())))

    missing_keys = sorted({k for k in keys.values() if k not in cached})
    key_to_text = {keys[bid]: text for bid, text in inputs.items()}

    computed = 0
    for start in range(0, len(missing_keys), batch_size):
        chunk = missing_keys[start : start + batch_size]
        vectors = embedder.embed([key_to_text[k] for k in chunk])
        cache.put_many(dict(zip(chunk, vectors, strict=True)))
        cached.update(dict(zip(chunk, vectors, strict=True)))
        computed += len(chunk)

    written = 0
    with conn.transaction():
        for block_id, key in keys.items():
            conn.execute(
                "UPDATE core.doc_block SET embedding = %s WHERE block_id = %s",
                ("[" + ",".join(repr(float(x)) for x in cached[key]) + "]", block_id),
            )
            written += 1

    return EmbedStats(
        pending=len(rows),
        from_cache=len(rows) - computed,
        computed=computed,
        written=written,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/embed/test_batch.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo/src/ragdemo/embed/batch.py tests/embed/test_batch.py
git commit -m "feat(embed): 批量嵌入与断点续传，相同内容只算一次"
```

---

## Task 9: TextIn xParse 封装与解析产物落盘

路径 B 的解析器。取代原计划的 MinerU，理由与推翻条件见
[`docs/adr/0008`](../../adr/0008-textin-xparse-document-parsing.md)，
调用契约与参数见 [`docs/05-document-pipeline.md`](../../05-document-pipeline.md) §2。

**Files:**
- Create: `packages/ragdemo-core/src/ragdemo_core/blob.py`, `packages/ragdemo/src/ragdemo/parse/textin.py`, `tests/test_blob.py`, `tests/parse/test_textin.py`, `tests/fixtures/xparse/annual_report.json`
- Modify: `packages/ragdemo/pyproject.toml`（加 `httpx`，若 P1a 已加则跳过）

**Interfaces:**
- Consumes: P1a Task 8 的 `NormalizedBlock`
- Produces:
  - `ragdemo_core.blob`：`BlobStore` Protocol（`exists` / `get` / `put`）、`LocalBlobStore(root: Path)`
  - `XPARSE_PARAMS: Mapping[str, str | int]`、`param_fingerprint(params) -> str`、`artifact_keys(content_hash, param_fp) -> tuple[str, str]`
  - `ParseResult(blocks, page_count, engine_version, markdown, json_ref, md_ref, warnings, from_cache)`
  - `DocumentParser` Protocol：`parse(file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult`
  - `MockDocumentParser`、`TextInParser(base_url, blob, *, timeout_s=600.0, allow_private=False, params=XPARSE_PARAMS)`
  - `PageBudget(remaining: int)`
  - 异常：`ParseTimeout` / `ParsePermanent(marker, code)` / `ParseRetryable` / `ParseConfigError` / `ParseBudgetExhausted` / `PrivateDocumentEgressBlocked`
  - 纯函数：`blocks_from_detail(detail, *, page_dims) -> list[NormalizedBlock]`、`table_markdown(cells) -> str`

**给实施者的前置说明**：`parse()` 的签名里**没有 `lang` 参数**——
那是 MinerU 时代的遗留，xParse 自动判断语种。别照着旧代码加回去。

- [ ] **Step 1: 写失败的测试**

`tests/test_blob.py`：

```python
"""对象存储抽象。写入必须原子——半个 JSON 会让缓存命中一份坏数据。"""
from __future__ import annotations

from pathlib import Path

import pytest

from ragdemo_core.blob import BlobStore, LocalBlobStore


def test_put_then_get_roundtrip(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    key = store.put("parse/textin/abc/deadbeef.json", b'{"a":1}')
    assert key == "parse/textin/abc/deadbeef.json"
    assert store.get(key) == b'{"a":1}'


def test_exists_is_false_before_put(tmp_path: Path) -> None:
    assert LocalBlobStore(tmp_path).exists("parse/textin/abc/deadbeef.json") is False


def test_nested_key_creates_directories(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("a/b/c/d.json", b"x")
    assert (tmp_path / "a" / "b" / "c" / "d.json").read_bytes() == b"x"


def test_no_partial_file_is_left_behind(tmp_path: Path) -> None:
    """写入走临时文件 + 原子替换，目录里不留 .part。"""
    store = LocalBlobStore(tmp_path)
    store.put("x/y.json", b"payload")
    assert [p.name for p in (tmp_path / "x").iterdir()] == ["y.json"]


def test_key_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    """key 由 sha256 拼出，但挡一下路径穿越——写到 root 外面去是静默的。"""
    with pytest.raises(ValueError, match="越出存储根目录"):
        LocalBlobStore(tmp_path).put("../../etc/passwd", b"x")


def test_local_store_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(LocalBlobStore(tmp_path), BlobStore)
```

`tests/fixtures/xparse/annual_report.json` —— 一份裁剪过的真实形状响应，
覆盖标题层级、正文、页眉（`content=1`）、含合并单元格的表格：

```json
{
  "code": 200,
  "message": "success",
  "version": "4.2.1",
  "duration": 8123,
  "result": {
    "markdown": "# 第三节 主营业务\n\n公司主营云端训练芯片。\n\n<table>...</table>",
    "total_page_number": 2,
    "success_count": 2,
    "detail": [
      {"page_id": 1, "paragraph_id": 0, "outline_level": -1, "content": 1,
       "type": "paragraph", "text": "寒武纪 2024 年半年度报告", "position": [0, 0, 600, 0, 600, 40, 0, 40]},
      {"page_id": 1, "paragraph_id": 1, "outline_level": 0, "content": 0,
       "type": "paragraph", "text": "第三节 主营业务", "position": [0, 60, 600, 60, 600, 100, 0, 100]},
      {"page_id": 1, "paragraph_id": 2, "outline_level": 1, "content": 0,
       "type": "paragraph", "text": "3.2 分部收入", "position": [0, 120, 600, 120, 600, 160, 0, 160]},
      {"page_id": 1, "paragraph_id": 3, "outline_level": -1, "content": 0,
       "type": "paragraph", "text": "公司主营云端训练芯片与智能计算集群系统。",
       "position": [0, 180, 600, 180, 600, 220, 0, 220]},
      {"page_id": 2, "paragraph_id": 4, "outline_level": -1, "content": 0,
       "type": "table", "text": "分部收入表",
       "position": [0, 100, 600, 100, 600, 400, 0, 400],
       "cells": [
         {"row": 0, "col": 0, "row_span": 1, "col_span": 1, "text": "业务分部"},
         {"row": 0, "col": 1, "row_span": 1, "col_span": 2, "text": "2024H1"},
         {"row": 1, "col": 0, "row_span": 1, "col_span": 1, "text": "智能计算"},
         {"row": 1, "col": 1, "row_span": 1, "col_span": 1, "text": "12,340"},
         {"row": 1, "col": 2, "row_span": 1, "col_span": 1, "text": "+58.2%"}
       ]}
    ],
    "catalog": {"toc": [[{"hierarchy": 1, "title": "第三节 主营业务", "page_id": 1}]]}
  },
  "metrics": [
    {"page_id": 1, "page_image_width": 600, "page_image_height": 850, "dpi": 144},
    {"page_id": 2, "page_image_width": 600, "page_image_height": 850, "dpi": 144}
  ]
}
```

`tests/parse/test_textin.py`：

```python
"""xParse 封装。

最要紧的三条：从 detail[] 切块（markdown 没有页码）、
parse_engine 带参数指纹（否则评测基线静默失效）、私有文档不外送。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ragdemo_core.blob import LocalBlobStore
from ragdemo.parse.textin import (
    XPARSE_PARAMS,
    PageBudget,
    ParseConfigError,
    ParsePermanent,
    ParseRetryable,
    PrivateDocumentEgressBlocked,
    TextInParser,
    artifact_keys,
    blocks_from_detail,
    param_fingerprint,
    table_markdown,
)

FIXTURE = Path("tests/fixtures/xparse/annual_report.json")


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _detail() -> list[dict[str, Any]]:
    return _payload()["result"]["detail"]


# --- 参数指纹 ---------------------------------------------------------------

def test_fingerprint_is_stable_across_key_order() -> None:
    assert param_fingerprint({"a": 1, "b": 2}) == param_fingerprint({"b": 2, "a": 1})


def test_fingerprint_changes_when_a_parameter_changes() -> None:
    """参数一改切块结果就变，评测集的 gold_block_ids 会静默失效（05 §2.2）。"""
    other = {**XPARSE_PARAMS, "table_flavor": "md"}
    assert param_fingerprint(XPARSE_PARAMS) != param_fingerprint(other)


def test_artifact_keys_carry_both_hash_and_fingerprint() -> None:
    j, m = artifact_keys("abc123", "deadbeef")
    assert j == "parse/textin/abc123/deadbeef.json"
    assert m == "parse/textin/abc123/deadbeef.md"


# --- detail[] → NormalizedBlock ---------------------------------------------

def test_headers_and_footers_are_dropped() -> None:
    """content == 1 是供应商标注的非正文（05 §3.3）。"""
    texts = [b.content for b in blocks_from_detail(_detail(), page_dims={})]
    assert "寒武纪 2024 年半年度报告" not in texts


def test_outline_level_drives_block_type() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    by_text = {b.content: b.block_type for b in blocks}
    assert by_text["第三节 主营业务"] == "title"
    assert by_text["公司主营云端训练芯片与智能计算集群系统。"] == "paragraph"


def test_section_path_is_built_from_the_heading_stack() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    body = next(b for b in blocks if b.content.startswith("公司主营"))
    assert body.section_path == "第三节 主营业务 > 3.2 分部收入"


def test_a_heading_gets_its_parents_path_not_its_own() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    sub = next(b for b in blocks if b.content == "3.2 分部收入")
    assert sub.section_path == "第三节 主营业务"


def test_table_becomes_one_block_of_type_table() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    tables = [b for b in blocks if b.block_type == "table"]
    assert len(tables) == 1
    assert "智能计算" in tables[0].content


def test_ordinals_are_contiguous_from_zero() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    assert [b.ordinal for b in blocks] == list(range(len(blocks)))


def test_page_numbers_are_one_based() -> None:
    """元数据校验第 6 条要求 page ∈ [1, page_count]，差一会让整份文档回滚。"""
    blocks = blocks_from_detail(_detail(), page_dims={})
    assert min(b.page for b in blocks if b.page is not None) == 1


def test_zero_based_page_ids_are_shifted_up() -> None:
    detail = [dict(d, page_id=int(d["page_id"]) - 1) for d in _detail()]
    blocks = blocks_from_detail(detail, page_dims={})
    assert min(b.page for b in blocks if b.page is not None) == 1


def test_bbox_is_normalised_by_page_size() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={1: (600, 850), 2: (600, 850)})
    heading = next(b for b in blocks if b.content == "第三节 主营业务")
    assert heading.bbox == pytest.approx((0.0, 60 / 850, 1.0, 100 / 850))


def test_bbox_is_none_when_page_size_is_unknown() -> None:
    """bbox 可空，P1 没有依赖它的功能——为它让整份文档失败不值得。"""
    blocks = blocks_from_detail(_detail(), page_dims={})
    assert all(b.bbox is None for b in blocks)


# --- 表格 -------------------------------------------------------------------

def test_merged_cells_are_expanded_not_left_blank() -> None:
    """留空会让跨列表头匹配不到——BM25 与嵌入都按块的整体文本工作。"""
    md = table_markdown([
        {"row": 0, "col": 0, "row_span": 1, "col_span": 1, "text": "业务分部"},
        {"row": 0, "col": 1, "row_span": 1, "col_span": 2, "text": "2024H1"},
    ])
    assert md.splitlines()[0] == "| 业务分部 | 2024H1 | 2024H1 |"


def test_pipe_inside_a_cell_is_escaped() -> None:
    md = table_markdown([{"row": 0, "col": 0, "row_span": 1, "col_span": 1, "text": "a|b"}])
    assert md.splitlines()[0] == r"| a\|b |"


def test_empty_cells_produce_empty_string() -> None:
    assert table_markdown([]) == ""


# --- 私有材料闸门 -----------------------------------------------------------

def test_private_document_is_not_sent_upstream(tmp_path: Path) -> None:
    """换到托管 API 后新增的风险，MinerU 时代不存在（adr/0008 后果 1）。"""
    parser = TextInParser("http://proxy.invalid", LocalBlobStore(tmp_path))
    with pytest.raises(PrivateDocumentEgressBlocked):
        parser.parse(b"%PDF-1.4", owner_user="u-42")


def test_private_document_passes_when_the_gate_is_open(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    parser = TextInParser("http://proxy.invalid", blob, allow_private=True)
    key, _ = artifact_keys(parser.content_hash(b"%PDF-1.4"), parser.param_fp)
    blob.put(key, json.dumps(_payload(), ensure_ascii=False).encode("utf-8"))
    assert parser.parse(b"%PDF-1.4", owner_user="u-42").from_cache is True


# --- 缓存 -------------------------------------------------------------------

def test_cache_hit_skips_the_billed_api_call(tmp_path: Path) -> None:
    """xParse 按页计费。同一份文档在同一套参数下永远只解析一次（05 §2.4）。"""
    blob = LocalBlobStore(tmp_path)
    parser = TextInParser("http://proxy.invalid", blob)
    json_key, _ = artifact_keys(parser.content_hash(b"%PDF-1.4"), parser.param_fp)
    blob.put(json_key, json.dumps(_payload(), ensure_ascii=False).encode("utf-8"))

    result = parser.parse(b"%PDF-1.4")          # base_url 不可达，命中缓存才不会炸

    assert result.from_cache is True
    assert result.json_ref == json_key
    assert result.blocks


def test_engine_version_carries_vendor_version_and_fingerprint(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    parser = TextInParser("http://proxy.invalid", blob)
    json_key, _ = artifact_keys(parser.content_hash(b"x"), parser.param_fp)
    blob.put(json_key, json.dumps(_payload(), ensure_ascii=False).encode("utf-8"))

    assert parser.parse(b"x").engine_version == f"textin:4.2.1+{parser.param_fp}"


# --- 错误码分类 -------------------------------------------------------------

@pytest.mark.parametrize(("code", "marker"), [
    (40303, "unsupported"), (40301, "unsupported"), (40425, "unsupported"),
    (40302, "too_large"), (40422, "corrupt"), (40423, "encrypted"),
])
def test_permanent_failures_carry_a_marker(code: int, marker: str) -> None:
    """永久失败要留记号，否则下次分区重跑会再拉一遍、再失败一遍。"""
    with pytest.raises(ParsePermanent) as excinfo:
        TextInParser.raise_for_code(code)
    assert excinfo.value.marker == marker


@pytest.mark.parametrize("code", [40004, 40101, 40102, 40103, 40424, 40427])
def test_config_errors_fail_loudly(code: int) -> None:
    """这些是我们的 bug，不是数据问题。重试没有意义。"""
    with pytest.raises(ParseConfigError):
        TextInParser.raise_for_code(code)


def test_insufficient_balance_is_not_retryable() -> None:
    """单独列出来：重试会在没钱的时候把分区反复跑满。"""
    with pytest.raises(ParseConfigError, match="余额"):
        TextInParser.raise_for_code(40003)


@pytest.mark.parametrize("code", [30203, 500])
def test_service_faults_are_retryable(code: int) -> None:
    with pytest.raises(ParseRetryable):
        TextInParser.raise_for_code(code)


def test_unknown_code_is_treated_as_retryable() -> None:
    """退避三次后失败，比永久丢掉一份文档安全。"""
    with pytest.raises(ParseRetryable):
        TextInParser.raise_for_code(49999)


def test_partial_page_failure_is_a_warning_not_an_error(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    parser = TextInParser("http://proxy.invalid", blob)
    payload = _payload()
    payload["code"] = 50207
    payload["result"]["success_count"] = 1
    json_key, _ = artifact_keys(parser.content_hash(b"x"), parser.param_fp)
    blob.put(json_key, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    result = parser.parse(b"x")

    assert result.blocks
    assert any("partial" in w for w in result.warnings)


# --- 页数预算 ---------------------------------------------------------------

def test_budget_reports_exhaustion() -> None:
    budget = PageBudget(remaining=10)
    budget.charge(4)
    assert budget.exhausted is False
    budget.charge(6)
    assert budget.exhausted is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_blob.py tests/parse/test_textin.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo_core.blob'`

- [ ] **Step 3: 写最小实现**

`packages/ragdemo-core/src/ragdemo_core/blob.py`：

```python
"""对象存储抽象。

P1 用本地文件系统，P4 接 MinIO（docs/01-architecture.md §4）。
放在 core 包里是因为它与 db 同层：接入侧存原始 PDF、解析侧存解析产物、
备份侧存 pg_dump，三处都要用，而它不含任何业务语义。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class BlobStore(Protocol):
    def exists(self, key: str) -> bool: ...
    def get(self, key: str) -> bytes: ...
    def put(self, key: str, data: bytes) -> str: ...


class LocalBlobStore:
    """本地文件系统实现。key 里的 '/' 映射成目录层级。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        # key 由固定前缀 + sha256 拼出，理论上安全；但路径穿越的后果是
        # 静默写到 root 外面，挡一下的成本远低于事后发现。
        resolved = (self.root / key).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError(f"key 越出存储根目录: {key!r}")
        return resolved

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件 + 原子替换。直接写的话，进程在中途死掉会留下半个 JSON，
        # 而 exists() 会把它当成缓存命中——那份坏数据会一直被读下去。
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return key
```

`packages/ragdemo/src/ragdemo/parse/textin.py`：

```python
"""TextIn xParse 封装（docs/05-document-pipeline.md §2，docs/adr/0008）。

三件事做错都不会报错，只会让结果慢慢变坏：

1. **从 detail[] 切块，不从 markdown 切**。markdown 是一整个字符串，没有页码，
   而 CLAUDE.md §0 要求每个数字绑定 [block_id | page]。
2. **parse_engine 要带参数指纹**。参数一改切块结果就变，
   评测集的 gold_block_ids 会静默失效。
3. **私有文档不外送**。TEXTIN_ALLOW_PRIVATE 默认 false（adr/0008 后果 1）。

认证头不在这里。按 docs/09-compliance-security.md §4.1，
x-ti-app-id / x-ti-secret-code 由出网代理转发时注入，
业务容器的环境变量里没有任何供应商密钥。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

import httpx

from ragdemo_core.blob import BlobStore
from ragdemo.adapters.announcements import NormalizedBlock

ENDPOINT: Final = "/ai/service/v1/pdf_to_markdown"
DEFAULT_TIMEOUT_S: Final = 600.0

# 参数集合是版本化的一部分（05 §2.2）。改这里必须同步那张表，
# 并且知道它会让全部存量文档的 param_fp 变化 —— 也就是一次全量重解析。
XPARSE_PARAMS: Final[Mapping[str, str | int]] = MappingProxyType({
    "parse_mode": "auto",
    "markdown_details": 1,      # detail[] 是切块的唯一输入
    "apply_document_tree": 1,   # outline_level 的来源
    "apply_merge": 1,           # 合并跨页表格。财报表格跨页是常态
    "table_flavor": "html",     # 只影响存档的 markdown；HTML 能表达合并单元格
    "catalog_details": 1,
    "page_details": 0,          # 关掉 pages[]：逐行 OCR 能把响应撑大一个数量级
    "raw_ocr": 0,
    "char_details": 0,
    "get_image": "none",        # 图片 URL 30 天过期，存下来就是悬空引用
    "apply_image_analysis": 0,  # 会把图片送去大模型解读，越过计算/生成分离的边界
    "formula_level": 0,
    "paratext_mode": "annotation",
    "dpi": 144,
    "page_start": 1,
    "page_count": 1000,
})

# 错误码分类。分错的代价是不对称的：把永久失败当成可重试会无限烧钱，
# 把可重试当成永久失败会静默丢文档。
_PERMANENT: Final[Mapping[int, str]] = MappingProxyType({
    40301: "unsupported", 40303: "unsupported", 40425: "unsupported",
    40302: "too_large", 40422: "corrupt", 40423: "encrypted",
})
_CONFIG_ERRORS: Final[frozenset[int]] = frozenset({40004, 40101, 40102, 40103, 40424, 40427})
_RETRYABLE: Final[frozenset[int]] = frozenset({30203, 500})
_OUT_OF_CREDIT: Final = 40003
_PARTIAL_FAILURE: Final = 50207
_OK: Final[frozenset[int]] = frozenset({0, 200, _PARTIAL_FAILURE})


class ParseTimeout(RuntimeError):
    """解析超时。记 textin:skipped 并告警，不阻塞分区。"""


class ParseRetryable(RuntimeError):
    """供应商侧的临时故障。退避重试。"""


class ParseConfigError(RuntimeError):
    """参数、认证或余额问题。是我们这边的事，重试没有意义。"""


class PrivateDocumentEgressBlocked(RuntimeError):
    """用户私有材料默认不外送（adr/0008 后果 1）。"""


class ParsePermanent(RuntimeError):
    """这份文档永远解析不了。marker 进 parse_engine，作为「别再试了」的记号。"""

    def __init__(self, marker: str, code: int) -> None:
        super().__init__(f"xParse 永久失败 code={code} ({marker})")
        self.marker = marker
        self.code = code


@dataclass
class PageBudget:
    """每次 Dagster run 的页数预算。

    xParse 按页计费，没有闸门的回填能一夜之间烧穿月度预算。
    只能**事后扣减**——页数要等响应回来才知道，所以它拦不住当前这一份，
    拦的是后面还没发出去的那些。回填场景要的正是这个。
    """

    remaining: int

    def charge(self, pages: int) -> None:
        self.remaining -= pages

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


@dataclass(frozen=True)
class ParseResult:
    blocks: list[NormalizedBlock]
    page_count: int
    engine_version: str          # textin:<result.version>+<param_fp>
    markdown: str
    json_ref: str | None
    md_ref: str | None
    warnings: list[str] = field(default_factory=list)
    from_cache: bool = False


@runtime_checkable
class DocumentParser(Protocol):
    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult: ...


def param_fingerprint(params: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(sorted(params.items())), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def artifact_keys(content_hash: str, param_fp: str) -> tuple[str, str]:
    prefix = f"parse/textin/{content_hash}/{param_fp}"
    return f"{prefix}.json", f"{prefix}.md"


def table_markdown(cells: Sequence[Mapping[str, Any]]) -> str:
    """cells[] → Markdown 表格，合并单元格展开成重复值。

    重复而不是留空：BM25 与嵌入都按块的整体文本工作，
    留空会让「智能计算 2024H1 收入」在跨列表头上匹配不到。
    """
    if not cells:
        return ""
    rows = max(int(c["row"]) + int(c.get("row_span", 1)) for c in cells)
    cols = max(int(c["col"]) + int(c.get("col_span", 1)) for c in cells)
    grid = [["" for _ in range(cols)] for _ in range(rows)]
    for cell in cells:
        text = str(cell.get("text", "")).replace("|", r"\|").replace("\n", " ").strip()
        r0, c0 = int(cell["row"]), int(cell["col"])
        for r in range(r0, r0 + int(cell.get("row_span", 1))):
            for c in range(c0, c0 + int(cell.get("col_span", 1))):
                grid[r][c] = text
    lines = ["| " + " | ".join(grid[0]) + " |", "|" + "---|" * cols]
    lines += ["| " + " | ".join(row) + " |" for row in grid[1:]]
    return "\n".join(lines)


def _page_offset(detail: Sequence[Mapping[str, Any]]) -> int:
    """page_id 是否 0 基，首次接入时用真实响应确认，在那之前防御式归一。

    元数据校验第 6 条要求 page ∈ [1, page_count]，差一会让整份文档回滚。
    """
    ids = [int(d["page_id"]) for d in detail if d.get("page_id") is not None]
    return 1 if ids and min(ids) == 0 else 0


def _bbox(
    position: Sequence[float] | None, dims: tuple[int, int] | None
) -> tuple[float, float, float, float] | None:
    """四角八点 → 轴对齐 [x0,y0,x1,y1]，按页宽高归一化到 [0,1]。

    归一化让存量 bbox 在换 dpi 后仍然有效。取不到页尺寸就返回 None——
    bbox 可空，P1 没有依赖它的功能。
    """
    if not position or len(position) != 8 or dims is None:
        return None
    width, height = dims
    if not width or not height:
        return None
    xs, ys = position[0::2], position[1::2]
    return (min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height)


def page_dims_from_metrics(
    metrics: Sequence[Mapping[str, Any]] | None, offset: int
) -> dict[int, tuple[int, int]]:
    """页尺寸取自顶层 metrics[]，而不是 pages[]——后者被 page_details=0 关掉了。"""
    return {
        int(m["page_id"]) + offset: (
            int(m.get("page_image_width") or 0),
            int(m.get("page_image_height") or 0),
        )
        for m in metrics or []
        if m.get("page_id") is not None
    }


def blocks_from_detail(
    detail: Sequence[Mapping[str, Any]],
    *,
    page_dims: Mapping[int, tuple[int, int]],
) -> list[NormalizedBlock]:
    offset = _page_offset(detail)
    stack: list[str] = []
    blocks: list[NormalizedBlock] = []

    for item in detail:
        if int(item.get("content", 0) or 0) == 1:
            continue  # 页眉页脚，供应商标注的非正文（05 §3.3）

        kind = str(item.get("type", "paragraph"))
        level = int(item.get("outline_level", -1))
        text = str(item.get("text", "")).strip()

        if kind == "table":
            block_type, content = "table", table_markdown(item.get("cells") or [])
        elif kind == "image":
            block_type, content = "figure", text
        elif level >= 0:
            block_type, content = "title", text
        else:
            block_type, content = "paragraph", text

        if not content.strip():
            continue  # NormalizedBlock 拒绝空内容

        if block_type == "title":
            # 层级跳跃（0 直接到 2）时标题会落在比名义层级浅的位置上。
            # 这是想要的：section_path 要保持连续，中间不出现空档。
            del stack[level:]
            stack.append(text)
            section_path = " > ".join(stack[:-1])
        else:
            section_path = " > ".join(stack)

        page = int(item["page_id"]) + offset if item.get("page_id") is not None else None
        blocks.append(
            NormalizedBlock(
                ordinal=len(blocks),
                block_type=block_type,
                section_path=section_path,
                content=content,
                page=page,
                bbox=_bbox(item.get("position"), page_dims.get(page) if page else None),
                level=level if level >= 0 else None,
            )
        )
    return blocks


class MockDocumentParser:
    """离线开发与测试用。真实解析走 TextInParser。"""

    def __init__(self, warnings: list[str] | None = None) -> None:
        self._warnings = warnings or []

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        blocks = [
            NormalizedBlock(0, "title", "", "第一节 公司概况", page=1, level=0),
            NormalizedBlock(
                1, "paragraph", "第一节 公司概况",
                "公司主营云端训练芯片与智能计算集群系统。", page=1,
            ),
            NormalizedBlock(
                2, "table", "第一节 公司概况",
                "| 指标 | 数值 |\n|---|---|\n| 营业收入 | 12,340 |", page=2,
            ),
        ]
        return ParseResult(
            blocks=blocks, page_count=2, engine_version="mock:1",
            markdown="# 第一节 公司概况\n", json_ref=None, md_ref=None,
            warnings=list(self._warnings),
        )


class TextInParser:
    """合合信息 TextIn xParse 客户端。"""

    def __init__(
        self,
        base_url: str,
        blob: BlobStore,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        allow_private: bool = False,
        params: Mapping[str, str | int] = XPARSE_PARAMS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.blob = blob
        self.timeout_s = timeout_s
        self.allow_private = allow_private
        self.params = dict(params)
        self.param_fp = param_fingerprint(self.params)

    @staticmethod
    def content_hash(file_bytes: bytes) -> str:
        return hashlib.sha256(file_bytes).hexdigest()

    @staticmethod
    def raise_for_code(code: int) -> None:
        if code in _OK:
            return
        if code == _OUT_OF_CREDIT:
            # 单独一条：重试会在没钱的时候把分区反复跑满。
            raise ParseConfigError("TextIn 账户余额不足，停止解析并告警")
        if code in _CONFIG_ERRORS:
            raise ParseConfigError(f"xParse 参数或认证错误 code={code}，这是我们的 bug")
        marker = _PERMANENT.get(code)
        if marker is not None:
            raise ParsePermanent(marker, code)
        # 未知错误码按可重试处理：退避三次后失败，比永久丢掉一份文档安全。
        raise ParseRetryable(f"xParse 错误 code={code}")

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        if owner_user is not None and not self.allow_private:
            raise PrivateDocumentEgressBlocked(
                "用户私有材料默认不外送。打开 TEXTIN_ALLOW_PRIVATE 之前，"
                "先落实数据处理协议与用户告知（docs/adr/0008 后果 1）。"
            )

        json_key, md_key = artifact_keys(self.content_hash(file_bytes), self.param_fp)
        if self.blob.exists(json_key):
            # 缓存命中就不调计费 API。同一份文档在同一套参数下只解析一次（05 §2.4）。
            return self._to_result(
                json.loads(self.blob.get(json_key)), json_key, md_key, from_cache=True
            )

        payload = self._call(file_bytes)
        self.blob.put(json_key, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        markdown = str((payload.get("result") or {}).get("markdown", ""))
        self.blob.put(md_key, markdown.encode("utf-8"))
        return self._to_result(payload, json_key, md_key, from_cache=False)

    def _call(self, file_bytes: bytes) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}{ENDPOINT}",
                params=self.params,
                content=file_bytes,  # 二进制流，不是 multipart
                headers={"Content-Type": "application/octet-stream"},
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise ParseTimeout(f"xParse 解析超过 {self.timeout_s}s") from exc

        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        self.raise_for_code(int(payload.get("code", 0)))
        return payload

    def _to_result(
        self, payload: Mapping[str, Any], json_key: str, md_key: str, *, from_cache: bool
    ) -> ParseResult:
        code = int(payload.get("code", 200))
        self.raise_for_code(code)  # 缓存里也可能是一份失败响应

        result = payload.get("result") or {}
        detail = result.get("detail") or []
        offset = _page_offset(detail)
        blocks = blocks_from_detail(
            detail, page_dims=page_dims_from_metrics(payload.get("metrics"), offset)
        )
        page_count = int(result.get("total_page_number") or 0)

        warnings: list[str] = []
        if code == _PARTIAL_FAILURE:
            warnings.append(
                f"partial page failure: {result.get('success_count')}/{page_count} pages parsed"
            )

        return ParseResult(
            blocks=blocks,
            page_count=page_count,
            engine_version=f"textin:{payload.get('version', 'unknown')}+{self.param_fp}",
            markdown=str(result.get("markdown", "")),
            json_ref=json_key,
            md_ref=md_key,
            warnings=warnings,
            from_cache=from_cache,
        )
```

依赖（若 P1a 已加 `httpx` 则跳过）——改的是成员包，不是工作区根：

```toml
# packages/ragdemo/pyproject.toml
[project]
dependencies = [
  "httpx>=0.27",
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_blob.py tests/parse/test_textin.py -v`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo-core/src/ragdemo_core/blob.py packages/ragdemo/src/ragdemo/parse/textin.py tests/test_blob.py tests/parse/test_textin.py tests/fixtures/xparse/annual_report.json packages/ragdemo/pyproject.toml
git commit -m "feat(parse): TextIn xParse 封装、解析产物落盘与成本缓存"
```

---

## Task 10: 文档管线 Dagster 资产

**Files:**
- Create: `packages/ragdemo/src/ragdemo/ingest/assets_docs.py`, `tests/ingest/test_assets_docs.py`
- Modify: `db/migrations/007_parse_artifacts.sql`（已随本次变更创建，此处只确认它在迁移序列里）

**Interfaces:**
- Consumes: Task 2–9 全部
- Produces:
  - `PreparedDocument(doc: NormalizedDocument, artifacts: ParseArtifacts | None)`
  - `prepare_documents(docs, parser, blob, budget) -> list[PreparedDocument]`
  - Dagster 资产 `doc_normalized` / `doc_blocks_loaded` / `block_embeddings`

**给实施者的前置说明**：原计划里 `MineruParser` 定义了却从没被任何资产调用过——
路径 B 是断的。这个任务把它接上：`prepare_documents` 是那段接线，
它是纯函数（不碰数据库、不建 Dagster 上下文），所以能被大量用例覆盖。

- [ ] **Step 1: 写失败的测试**

`tests/ingest/test_assets_docs.py`：

```python
"""文档管线端到端：Mock 公告 → 解析 → 切块 → 入库 → 嵌入，全部块可检索。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from dagster import build_asset_context

from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate
from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from ragdemo.embed.mock import MockEmbedder
from ragdemo.ingest.assets_docs import (
    block_embeddings,
    doc_blocks_loaded,
    doc_normalized,
    prepare_documents,
)
from ragdemo.ingest.documents import DocumentWriter
from ragdemo.parse.textin import PageBudget, MockDocumentParser, ParsePermanent

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    c.commit()
    return c


@pytest.mark.db
def test_pipeline_produces_searchable_blocks(conn: psycopg.Connection, tmp_path: Path) -> None:
    ctx = build_asset_context(partition_key="2024-10-28")
    docs = doc_normalized(ctx, MockAnnouncementProvider())
    prepared = prepare_documents(
        docs, MockDocumentParser(), LocalBlobStore(tmp_path), PageBudget(1000)
    )
    writer = DocumentWriter(conn, ingest_run_id="r1", source="mock-announcements")
    doc_blocks_loaded(ctx, prepared, writer)
    stats = block_embeddings(ctx, conn, MockEmbedder())

    assert stats.written > 0
    (leaves_without_vec,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE is_leaf AND embedding IS NULL"
    ).fetchone()  # type: ignore[misc]
    assert leaves_without_vec == 0

    (bm25_hits,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE content @@@ '云端训练芯片'"
    ).fetchone()  # type: ignore[misc]
    assert bm25_hits > 0


@pytest.mark.db
def test_rerunning_the_pipeline_is_idempotent(conn: psycopg.Connection, tmp_path: Path) -> None:
    ctx = build_asset_context(partition_key="2024-10-28")
    docs = doc_normalized(ctx, MockAnnouncementProvider())
    blob = LocalBlobStore(tmp_path)
    prepared = prepare_documents(docs, MockDocumentParser(), blob, PageBudget(1000))

    doc_blocks_loaded(ctx, prepared, DocumentWriter(conn, ingest_run_id="r1", source="mock-announcements"))
    (after_first,) = conn.execute("SELECT count(*) FROM core.doc_block").fetchone()  # type: ignore[misc]

    doc_blocks_loaded(ctx, prepared, DocumentWriter(conn, ingest_run_id="r2", source="mock-announcements"))
    (after_second,) = conn.execute("SELECT count(*) FROM core.doc_block").fetchone()  # type: ignore[misc]

    assert after_first == after_second


@pytest.mark.db
def test_parse_artifact_refs_land_in_the_document_row(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """用户要的「存 JSON + 存 Markdown」在库里的落点就是这两列（05 §2.4）。"""
    ctx = build_asset_context(partition_key="2024-10-28")
    docs = [d for d in doc_normalized(ctx, MockAnnouncementProvider()) if not d.blocks]
    prepared = prepare_documents(
        docs, MockDocumentParser(), LocalBlobStore(tmp_path), PageBudget(1000)
    )
    doc_blocks_loaded(ctx, prepared, DocumentWriter(conn, ingest_run_id="r1", source="mock"))

    rows = conn.execute(
        "SELECT parse_engine, parse_json_ref, parse_md_ref FROM core.document"
    ).fetchall()
    assert rows
    for engine, json_ref, md_ref in rows:
        assert engine.startswith("mock:") or engine.startswith("textin:")
        assert json_ref is not None and md_ref is not None


def test_path_a_documents_are_not_re_parsed(tmp_path: Path) -> None:
    """供应商已经给了结构化块，再送去解析既花钱又不如原件准（05 §1）。"""
    class Exploding(MockDocumentParser):
        def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> object:
            raise AssertionError("路径 A 的文档不应该被解析")

    ctx = build_asset_context(partition_key="2024-10-28")
    docs = [d for d in doc_normalized(ctx, MockAnnouncementProvider()) if d.blocks]
    prepared = prepare_documents(docs, Exploding(), LocalBlobStore(tmp_path), PageBudget(1000))

    assert [p.artifacts for p in prepared] == [None] * len(prepared)


def test_budget_exhaustion_stops_before_the_next_call(tmp_path: Path) -> None:
    """页数预算耗尽就停，不静默烧钱（05 §2.7）。"""
    class Counting(MockDocumentParser):
        calls = 0

        def parse(self, file_bytes: bytes, *, owner_user: str | None = None):
            type(self).calls += 1
            return super().parse(file_bytes, owner_user=owner_user)

    ctx = build_asset_context(partition_key="2024-10-28")
    docs = [d for d in doc_normalized(ctx, MockAnnouncementProvider()) if not d.blocks]
    parser = Counting()
    prepare_documents(docs, parser, LocalBlobStore(tmp_path), PageBudget(1))

    assert Counting.calls == 1  # 第一份用掉 2 页，预算见底，第二份不再发


def test_permanent_failure_still_records_the_document(tmp_path: Path) -> None:
    """不留记号的话，下次分区重跑会再拉一遍、再失败一遍（05 §2.6）。"""
    class Unsupported(MockDocumentParser):
        def parse(self, file_bytes: bytes, *, owner_user: str | None = None):
            raise ParsePermanent("unsupported", 40303)

    ctx = build_asset_context(partition_key="2024-10-28")
    docs = [d for d in doc_normalized(ctx, MockAnnouncementProvider()) if not d.blocks]
    prepared = prepare_documents(docs, Unsupported(), LocalBlobStore(tmp_path), PageBudget(1000))

    assert prepared
    for item in prepared:
        assert item.doc.blocks == []
        assert item.artifacts is not None
        assert item.artifacts.engine == "textin:unsupported"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/ingest/test_assets_docs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.ingest.assets_docs'`

- [ ] **Step 3: 写最小实现**

`packages/ragdemo/src/ragdemo/ingest/assets_docs.py`：

```python
"""文档管线的 Dagster 资产。

路径 A（供应商结构化接口）的文档自带块，直接入库。
路径 B 的文档只有一个 raw_bytes_ref，要先送 xParse 解析（docs/05-document-pipeline.md §1）。
分流在 prepare_documents 里，它是纯函数——不碰数据库、不要 Dagster 上下文。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

import psycopg
from dagster import AssetExecutionContext, asset

from ragdemo_core.blob import BlobStore
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

UTC = timezone.utc
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
    ctx: AssetExecutionContext, announcements: AnnouncementProvider
) -> list[NormalizedDocument]:
    partition = date.fromisoformat(ctx.partition_key)
    fetch_ctx = FetchContext(ingest_run_id=ctx.run_id, partition_date=partition)
    until = datetime.combine(partition, datetime.max.time(), tzinfo=UTC)
    since = until - LOOKBACK
    docs = [
        announcements.normalize(raw)
        for raw in announcements.list_documents(fetch_ctx, since=since, until=until)
    ]
    ctx.log.info("normalized documents", extra={"run_id": ctx.run_id, "count": len(docs)})
    return docs


@asset(partitions_def=DAILY, group_name="documents")
def doc_blocks_loaded(
    ctx: AssetExecutionContext,
    prepared: Sequence[PreparedDocument],
    writer: DocumentWriter,
) -> int:
    cfg = ChunkConfig()
    describer = MockTableDescriber()
    total = 0
    for item in prepared:
        doc = item.doc
        chunks = build_tree(chunk_document(doc, cfg)) if doc.blocks else []
        descriptions = {
            c.ordinal: describer.describe(
                c.content, title=doc.title, section_path=c.section_path
            )
            for c in chunks
            if c.block_type == "table"
        }
        result = writer.write_document(doc, chunks, descriptions, artifacts=item.artifacts)
        if not result.skipped:
            total += len(result.block_ids)
    ctx.log.info("loaded blocks", extra={"run_id": ctx.run_id, "count": total})
    return total


@asset(partitions_def=DAILY, group_name="documents")
def block_embeddings(
    ctx: AssetExecutionContext, conn: psycopg.Connection, embedder: Embedder
) -> EmbedStats:
    stats = embed_pending_blocks(conn, embedder)
    ctx.log.info(
        "embedded",
        extra={
            "run_id": ctx.run_id,
            "pending": stats.pending,
            "from_cache": stats.from_cache,
            "computed": stats.computed,
        },
    )
    return stats
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/parse tests/ingest -v`
Expected: 全部通过（含端到端的 BM25 命中）

- [ ] **Step 5: 提交**

```bash
git add packages/ragdemo/src/ragdemo/ingest/assets_docs.py tests/ingest/test_assets_docs.py
git commit -m "feat(ingest): 文档管线 Dagster 资产，接通路径 B 解析"
```

---

## Self-Review

**Spec 覆盖检查**（对照 [`docs/11-sdlc.md`](../../11-sdlc.md) §3 的 W2）：

| 工作流 | 出口条件 | 任务 | 覆盖 |
|---|---|---|---|
| W2.1 切块器、父子块、`is_leaf` | 表格不切分；`is_leaf` 与父子关系一致 | Task 2, 3 | ✅ |
| W2.2 xParse 封装 + 产物落盘 | 超时不阻塞分区；`parse_warnings` 落库；缓存命中不重复调用计费 API；私有文档被拦下 | Task 9, 10 | ✅ |
| W2.3 `content_desc` | 表格块 100% 有描述；不含解读 | Task 4 | ✅ |
| W2.4 嵌入器 | 断点续传；缓存主键含 `model` 与 `owner_user` | Task 7, 8 | ✅ |
| W2.5 元数据校验与幂等重建 | 任一项不过整份回滚；重解析沿用原 `known_at` | Task 5, 6 | ✅ |
| 切块长度规范冲突 | 文档与实现一致 | Task 1 | ✅ |

**类型一致性检查**：`Chunk` 在 Task 2 定义，Task 3/5/6/10 使用，字段一致；
`ChunkConfig` 在 Task 1 定义，Task 2/3/10 使用；`validate_chunks(doc, chunks, descriptions)`
在 Task 5 定义、Task 6 调用，三参数一致；`ParseArtifacts` 在 Task 6 定义
（不在 `parse/` 里，以保持 `ingest → parse` 的单向依赖），Task 9 的 `ParseResult`
由 Task 10 转成它；`DocumentWriter.write_document(doc, chunks, descriptions, *, artifacts=None)`
在 Task 6 定义、Task 10 调用，签名一致；`Embedder.embed` 在 Task 7 定义，
Task 8/10 使用；`EmbedStats` 在 Task 8 定义，Task 10 返回；`BlobStore` 在 Task 9
定义，Task 10 使用。

**与 MinerU 方案的差异**（本计划在 [adr/0008](../../adr/0008-textin-xparse-document-parsing.md)
之后重写过 Task 9，读旧代码或旧文档的人注意）：

- `DocumentParser.parse()` **没有 `lang` 参数**了——xParse 自动判断语种；
- 不再需要 `mineru` 容器，`infra/docker-compose.yml` 不加服务；
- 不再有 `ParseTooLarge` / `enforce_limits`：托管 API 的页数上限是成本问题不是内存问题，
  换成 `PageBudget`（而且事后扣减——页数要等响应回来才知道，
  它拦的是后面那些还没发出去的）；
- 新增 `ragdemo_core.blob`，因为解析产物要落盘。

**已经修掉的前一版缺口**：`parse_warnings` 此前只活在 `ParseResult` 里、没有落库位置，
现在有了 `core.document.parse_warnings`（迁移 `007_parse_artifacts.sql`）。

**已知缺口（有意留给 P1c / P4）**：

- `LocalBlobStore` 是本地文件系统实现。多副本部署时解析缓存不共享，
  会重复付费。P4 接 MinIO 时换实现，`BlobStore` 协议不变。
- `page_id` 是否 0 基尚未用真实响应确认。封装里做了防御式归一，
  首次接入后把实测结论回写 `docs/05-document-pipeline.md` §2.5。
- `parse_warnings` 的**消费方**（检索降权、抽取置信度降档）在 P1c/P2。

---

## 完成之后

1. 在 [`docs/10-roadmap.md`](../../10-roadmap.md) P1 的「TextIn xParse 封装（含解析产物落盘）、
   切块器、父子块构造、元数据校验」与「嵌入器 + 批处理 + 缓存 + 断点续传」两项上打勾。
2. 把实测结论回写 [`docs/05-document-pipeline.md`](../../05-document-pipeline.md)——
   特别是 §2.5 的 `page_id` 基数，以及 §2.6 里实际遇到的错误码。
3. 确认 `api.textin.com` 已在出网代理白名单里，且密钥只在代理层
   （[`docs/09-compliance-security.md`](../../09-compliance-security.md) §4.1）。
4. 执行 [P1c 检索与验收](2026-09-21-p1c-retrieval-and-acceptance.md)。
