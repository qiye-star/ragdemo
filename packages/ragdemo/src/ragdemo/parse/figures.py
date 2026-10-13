"""图表区域 → PNG 图片 + 元数据（两条解析路径共用）。

只做"按外接框渲染裁剪、内容寻址存入对象存储、产出元数据"。图片**不送任何模型、不做 OCR**，
图里的数据不被解读（与 parse/textin.py 关闭 apply_image_analysis 同一理由：不越过计算 / 生成分离）。
页面带 /Rotate、或 pdfium 的页面尺寸与版面提取的尺寸不一致（CropBox 偏移）时不裁、记 FigureSkip——
宁可少一张图，也不存一张错位的图。"""

from __future__ import annotations

import dataclasses
import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import pypdfium2 as pdfium
from PIL import Image

from ragdemo_core.blob import BlobStore

BBox = tuple[
    float, float, float, float
]  # (x0, top, x1, bottom)，pt，原点页面左上角，x 向右、y 向下
Detection = Literal["embedded_image", "vector"]
FigureSource = Literal[
    "local", "textin"
]  # 该文档 / 该页的正文由谁解析；检测与裁图两条路径都是本地检测器

FIGURE_DPI: Final = (
    120  # 实测：100 dpi 研报均值约 121 KB / 张、150 dpi 约 218 KB；120 dpi 下刻度字可读
)
FIGURE_FALLBACK_DPI: Final = 96  # 超过体积上限时降到这个 dpi 再试一次
FIGURE_MAX_EDGE_PX: Final = (
    1600  # 长边上限：A4 整页 842pt@120dpi = 1403 px 不触发，A3 / 超大图按比例降 dpi
)
FIGURE_MAX_BYTES: Final = 1_000_000  # 单张上限：实测 150 dpi 最大 890 KB，120 dpi 下正常图远低于此
FIGURE_PAD_PT: Final = 2.0
FIGURE_KEY_PREFIX: Final = (
    "parse/figures"  # 与 parse/textin/<hash>/... 并列，在 blob_root（默认 data/blob）下
)
PAGE_SIZE_TOL_PT: Final = 0.5


@dataclass(frozen=True)
class FigureRegion:
    page: int  # 从 1 起
    bbox: BBox
    detection: Detection
    caption: str = ""  # 图注原文（文字层逐字），没有为空
    source_note: str = ""  # "资料来源：…" 原文，没有为空
    stripped_words: int = 0  # 从正文流剥离的词数（内容在 PageResult.stripped_text）


@dataclass(frozen=True)
class FigureRef:
    doc_sha256: str  # 原始 PDF 字节的 sha256（= core.document.content_hash）；doc_id 在入库时绑定
    page: int
    bbox_pt: BBox  # 实际裁剪框（含 FIGURE_PAD_PT 留白，已裁到页面内）
    bbox_norm: BBox  # 同一框按页宽高归一化到 [0,1]，与 core.doc_block.bbox 的口径一致（左上原点）
    image_key: str  # 对象存储 key：parse/figures/<doc_sha256>/<image_sha256>.png
    image_sha256: str
    width_px: int
    height_px: int
    dpi: int
    png_bytes: int
    caption: str
    source_note: str
    source: FigureSource
    detection: Detection
    detector_version: str
    stripped_words: int


@dataclass(frozen=True)
class FigureSkip:
    page: int
    bbox: BBox
    reason: str


def figure_key(doc_sha256: str, png: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(png).hexdigest()
    return f"{FIGURE_KEY_PREFIX}/{doc_sha256}/{digest}.png", digest


def _render(
    page: Any,  # noqa: ANN401 -- pypdfium2 无类型标注
    box: BBox,
    height: float,
    width: float,
    dpi: int,
) -> Image.Image:
    x0, top, x1, bottom = box
    # pypdfium2 的 crop 是"从四边各裁掉多少"（左、下、右、上，PDF 单位）
    bitmap = page.render(scale=dpi / 72, crop=(x0, height - bottom, width - x1, top))
    image: Image.Image = bitmap.to_pil()
    return image


def _encode(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)  # 同一像素 → 同一字节（内容寻址的前提）
    return buf.getvalue()


def crop_figures(
    pdf_bytes: bytes,
    regions: Sequence[FigureRegion],
    *,
    doc_sha256: str,
    blob: BlobStore,
    source: FigureSource,
    detector_version: str,
    page_sizes: Mapping[int, tuple[float, float]],
) -> tuple[list[FigureRef], list[FigureSkip]]:
    refs: list[FigureRef] = []
    skips: list[FigureSkip] = []
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        for r in regions:
            page = pdf[r.page - 1]
            try:
                width, height = page.get_size()
                if page.get_rotation() % 360 != 0:
                    skips.append(FigureSkip(r.page, r.bbox, "页面带 /Rotate，裁图坐标不可信"))
                    continue
                ew, eh = page_sizes[r.page]
                if abs(width - ew) > PAGE_SIZE_TOL_PT or abs(height - eh) > PAGE_SIZE_TOL_PT:
                    skips.append(
                        FigureSkip(
                            r.page,
                            r.bbox,
                            f"页面尺寸不一致（pdfium {width:.1f}×{height:.1f}，"
                            f"版面 {ew:.1f}×{eh:.1f}）",
                        )
                    )
                    continue
                x0, top, x1, bottom = r.bbox
                box = (
                    max(0.0, x0 - FIGURE_PAD_PT),
                    max(0.0, top - FIGURE_PAD_PT),
                    min(width, x1 + FIGURE_PAD_PT),
                    min(height, bottom + FIGURE_PAD_PT),
                )
                if box[2] - box[0] < 1 or box[3] - box[1] < 1:
                    skips.append(FigureSkip(r.page, r.bbox, "区域为空"))
                    continue
                longest = max(box[2] - box[0], box[3] - box[1])
                dpi = min(FIGURE_DPI, int(FIGURE_MAX_EDGE_PX * 72 / longest))
                img = _render(page, box, height, width, dpi)
                png = _encode(img)
                if len(png) > FIGURE_MAX_BYTES and dpi > FIGURE_FALLBACK_DPI:
                    dpi = FIGURE_FALLBACK_DPI
                    img = _render(page, box, height, width, dpi)
                    png = _encode(img)
                if len(png) > FIGURE_MAX_BYTES:
                    skips.append(
                        FigureSkip(
                            r.page, r.bbox, f"PNG {len(png)} 字节超过上限 {FIGURE_MAX_BYTES}"
                        )
                    )
                    continue
                key, digest = figure_key(doc_sha256, png)
                if not blob.exists(key):
                    blob.put(key, png)
                refs.append(
                    FigureRef(
                        doc_sha256,
                        r.page,
                        box,
                        (box[0] / width, box[1] / height, box[2] / width, box[3] / height),
                        key,
                        digest,
                        img.width,
                        img.height,
                        dpi,
                        len(png),
                        r.caption,
                        r.source_note,
                        source,
                        r.detection,
                        detector_version,
                        r.stripped_words,
                    )
                )
            finally:
                page.close()
    finally:
        pdf.close()
    return refs, skips


def figure_to_dict(ref: FigureRef) -> dict[str, object]:
    d = dataclasses.asdict(ref)
    d["bbox_pt"], d["bbox_norm"] = list(ref.bbox_pt), list(ref.bbox_norm)
    return d


def figure_from_dict(d: Mapping[str, Any]) -> FigureRef:
    vals = dict(d)
    vals["bbox_pt"] = tuple(float(x) for x in d["bbox_pt"])
    vals["bbox_norm"] = tuple(float(x) for x in d["bbox_norm"])
    return FigureRef(**vals)
