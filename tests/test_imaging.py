from __future__ import annotations

import io

import pytest

pytest.importorskip("PIL")
from PIL import Image as PILImage

from notion_local_ops_mcp.imaging import (
    compress_image_bytes,
    load_image_file,
    normalize_format,
)


def _png_bytes(width: int = 200, height: int = 100) -> bytes:
    img = PILImage.new("RGB", (width, height), (255, 0, 0))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def test_normalize_format_aliases() -> None:
    assert normalize_format("jpg") == "jpeg"
    assert normalize_format(None) == "jpeg"
    assert normalize_format("WEBP") == "webp"
    with pytest.raises(ValueError):
        normalize_format("tiff")


def test_compress_keeps_small_image_dimensions() -> None:
    raw = _png_bytes(200, 100)
    data, fmt, width, height = compress_image_bytes(raw, max_width=1400, format="png")
    assert fmt == "png"
    assert (width, height) == (200, 100)
    decoded = PILImage.open(io.BytesIO(data))
    assert decoded.size == (200, 100)


def test_compress_downscales_wide_image_to_jpeg() -> None:
    raw = _png_bytes(2800, 1400)
    data, fmt, width, height = compress_image_bytes(
        raw, max_width=1400, format="jpeg", quality=70
    )
    assert fmt == "jpeg"
    assert (width, height) == (1400, 700)
    decoded = PILImage.open(io.BytesIO(data))
    assert decoded.format == "JPEG"
    assert decoded.size == (1400, 700)


def test_load_image_file_roundtrip(tmp_path) -> None:
    path = tmp_path / "sample.png"
    path.write_bytes(_png_bytes(64, 32))
    info = load_image_file(path, format="webp")
    assert info["format"] == "webp"
    assert info["width"] == 64
    assert info["height"] == 32
    assert info["encoded_bytes"] == len(info["data"])
    assert info["original_bytes"] > 0


def test_load_image_file_missing(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_image_file(tmp_path / "missing.png")
