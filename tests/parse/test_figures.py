from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from ragdemo.parse import figures as fig_mod
from ragdemo.parse.figures import (
    FIGURE_KEY_PREFIX,
    FigureRef,
    FigureRegion,
    FigureSkip,
    crop_figures,
    figure_from_dict,
    figure_to_dict,
)
from ragdemo_core.blob import LocalBlobStore


class CountingBlob(LocalBlobStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.puts = 0

    def put(self, key: str, data: bytes) -> str:
        self.puts += 1
        return super().put(key, data)


def _pdf(size: tuple[float, float] = (595.0, 842.0), rotate: int = 0) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    if rotate:
        c.setPageRotation(rotate)
    c.setFillColorRGB(0.2, 0.4, 0.8)
    c.rect(60, size[1] - 280, 260, 160, stroke=0, fill=1)
    c.showPage()
    c.save()
    return buf.getvalue()


REGION = FigureRegion(
    page=1,
    bbox=(60.0, 120.0, 320.0, 280.0),
    detection="vector",
    caption="图1：测试",
    stripped_words=7,
)
DOC = hashlib.sha256(b"doc").hexdigest()


def _crop(
    pdf: bytes, blob: LocalBlobStore, sizes: dict[int, tuple[float, float]] | None = None
) -> tuple[list[FigureRef], list[FigureSkip]]:
    return crop_figures(
        pdf,
        [REGION],
        doc_sha256=DOC,
        blob=blob,
        source="local",
        detector_version="chart-v1",
        page_sizes=sizes or {1: (595.0, 842.0)},
    )


def test_crop_renders_the_region_as_a_content_addressed_png(tmp_path: Path) -> None:
    blob = CountingBlob(tmp_path)
    (ref,), skips = _crop(_pdf(), blob)
    assert skips == []
    png = blob.get(ref.image_key)
    assert ref.image_key == f"{FIGURE_KEY_PREFIX}/{DOC}/{hashlib.sha256(png).hexdigest()}.png"
    assert ref.image_sha256 == hashlib.sha256(png).hexdigest() and ref.png_bytes == len(png)
    img = Image.open(BytesIO(png))
    assert img.format == "PNG" and img.size == (ref.width_px, ref.height_px)
    assert ref.dpi == 120 and abs(ref.width_px - (264 * 120 / 72)) <= 2  # 含两侧各 2pt 留白
    assert ref.bbox_pt == pytest.approx((58.0, 118.0, 322.0, 282.0))
    assert ref.bbox_norm == pytest.approx((58 / 595, 118 / 842, 322 / 595, 282 / 842))
    assert (ref.caption, ref.source, ref.detection, ref.stripped_words) == (
        "图1：测试",
        "local",
        "vector",
        7,
    )


def test_cropping_twice_is_idempotent(tmp_path: Path) -> None:
    blob = CountingBlob(tmp_path)
    first, _ = _crop(_pdf(), blob)
    second, _ = _crop(_pdf(), blob)
    assert first == second and blob.puts == 1


def test_large_regions_are_capped_at_the_long_edge(tmp_path: Path) -> None:
    big = FigureRegion(page=1, bbox=(0.0, 0.0, 1190.0, 1684.0), detection="embedded_image")
    (ref,), _ = crop_figures(
        _pdf((1190.0, 1684.0)),
        [big],
        doc_sha256=DOC,
        blob=LocalBlobStore(tmp_path),
        source="local",
        detector_version="chart-v1",
        page_sizes={1: (1190.0, 1684.0)},
    )
    assert max(ref.width_px, ref.height_px) <= 1600 and ref.dpi < 120


def test_oversized_pngs_fall_back_then_are_skipped_not_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fig_mod, "FIGURE_MAX_BYTES", 10)
    refs, skips = _crop(_pdf(), LocalBlobStore(tmp_path))
    assert refs == [] and len(skips) == 1 and "上限" in skips[0].reason
    assert not any(tmp_path.rglob("*.png"))


def test_rotated_pages_and_size_mismatches_are_skipped_not_misaligned(tmp_path: Path) -> None:
    refs, skips = _crop(_pdf(rotate=90), LocalBlobStore(tmp_path))
    assert refs == [] and "Rotate" in skips[0].reason
    refs, skips = _crop(_pdf(), LocalBlobStore(tmp_path), sizes={1: (612.0, 792.0)})
    assert refs == [] and "尺寸" in skips[0].reason


def test_figure_dicts_round_trip(tmp_path: Path) -> None:
    (ref,), _ = _crop(_pdf(), LocalBlobStore(tmp_path))
    d = figure_to_dict(ref)
    assert set(d) >= {
        "doc_sha256",
        "page",
        "bbox_pt",
        "bbox_norm",
        "image_key",
        "image_sha256",
        "width_px",
        "height_px",
        "dpi",
        "png_bytes",
        "caption",
        "source_note",
        "source",
        "detection",
        "detector_version",
        "stripped_words",
    }
    assert figure_from_dict(d) == ref


# ---------------------------------------------------------------- T4 评审修复（I1 与小项）
def test_i1_a_media_box_with_a_non_zero_origin_is_skipped_not_cropped_offset(
    tmp_path: Path,
) -> None:
    raw = _pdf()
    assert b"/MediaBox [ 0 0 595 842 ]" in raw
    shifted = raw.replace(b"/MediaBox [ 0 0 595 842 ]", b"/MediaBox [ 9 9 604 851 ]")
    refs, skips = _crop(shifted, LocalBlobStore(tmp_path))
    assert refs == [] and len(skips) == 1 and "原点" in skips[0].reason
    assert not any(tmp_path.rglob("*.png"))


@pytest.mark.parametrize("bad", ["../../escape", "abc", "G" * 64, DOC.upper()])
def test_figure_key_requires_a_64_hex_doc_sha256(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="doc_sha256"):
        fig_mod.figure_key(bad, b"png")
    with pytest.raises(ValueError, match="doc_sha256"):
        crop_figures(
            _pdf(),
            [REGION],
            doc_sha256=bad,
            blob=LocalBlobStore(tmp_path),
            source="local",
            detector_version="chart-v1",
            page_sizes={1: (595.0, 842.0)},
        )
    assert not any(tmp_path.rglob("*"))


def test_degenerate_regions_out_of_range_pages_and_missing_page_sizes_are_skips(
    tmp_path: Path,
) -> None:
    def crop(region: FigureRegion, sizes: dict[int, tuple[float, float]]) -> list[FigureSkip]:
        refs, skips = crop_figures(
            _pdf(),
            [region],
            doc_sha256=DOC,
            blob=LocalBlobStore(tmp_path),
            source="local",
            detector_version="chart-v1",
            page_sizes=sizes,
        )
        assert refs == []
        return skips

    sizes = {1: (595.0, 842.0)}
    (tiny,) = crop(FigureRegion(1, (100.0, 100.0, 100.5, 100.5), "vector"), sizes)
    assert "过小" in tiny.reason
    (oor,) = crop(FigureRegion(5, REGION.bbox, "vector"), sizes)
    assert "页码" in oor.reason
    (zero,) = crop(FigureRegion(0, REGION.bbox, "vector"), sizes)
    assert "页码" in zero.reason
    (nosize,) = crop(REGION, {})
    assert "页面尺寸" in nosize.reason
