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

from ragdemo.adapters.announcements import NormalizedBlock
from ragdemo_core.blob import BlobStore

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
