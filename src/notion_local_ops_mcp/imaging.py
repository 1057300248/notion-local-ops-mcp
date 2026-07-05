"""Image loading, compression, and screen-capture helpers.

These helpers back the ``read_image``/``screenshot`` MCP tools. Images are
re-encoded (and optionally downscaled) before being returned so tool results
stay well under MCP payload limits.
"""

from __future__ import annotations

import io
from pathlib import Path

DEFAULT_MAX_WIDTH = 1400
DEFAULT_QUALITY = 80

SUPPORTED_FORMATS = {"jpeg", "png", "webp"}


def _pil_image():
    try:
        from PIL import Image as pil_image
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "Pillow is required for image tools. Install it with: pip install pillow"
        ) from exc
    return pil_image


def normalize_format(format: str | None) -> str:
    """Normalize an output format name to one of jpeg/png/webp."""
    fmt = (format or "jpeg").strip().lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported image format {format!r}; use one of: jpeg, png, webp"
        )
    return fmt


def encode_pil_image(
    img,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    format: str = "jpeg",
    quality: int = DEFAULT_QUALITY,
) -> tuple[bytes, str, int, int]:
    """Downscale and encode a PIL image.

    Returns ``(data, format, width, height)`` for the encoded image.
    """
    pil_image = _pil_image()
    fmt = normalize_format(format)
    if max_width and img.width > int(max_width):
        new_height = max(1, round(img.height * int(max_width) / img.width))
        resampling = getattr(pil_image, "Resampling", pil_image)
        img = img.resize((int(max_width), new_height), resampling.LANCZOS)
    if fmt == "jpeg" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buffer = io.BytesIO()
    save_kwargs: dict = {}
    if fmt in ("jpeg", "webp"):
        save_kwargs["quality"] = max(1, min(100, int(quality)))
    if fmt == "png":
        save_kwargs["optimize"] = True
    img.save(buffer, format=fmt.upper(), **save_kwargs)
    return buffer.getvalue(), fmt, img.width, img.height


def compress_image_bytes(
    raw: bytes,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    format: str = "jpeg",
    quality: int = DEFAULT_QUALITY,
) -> tuple[bytes, str, int, int]:
    """Decode raw image bytes, downscale, and re-encode."""
    pil_image = _pil_image()
    img = pil_image.open(io.BytesIO(raw))
    img.load()
    return encode_pil_image(img, max_width=max_width, format=format, quality=quality)


def load_image_file(
    path: Path,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    format: str = "jpeg",
    quality: int = DEFAULT_QUALITY,
) -> dict:
    """Read an image file from disk and return compressed image data."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {path}")
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")
    raw = path.read_bytes()
    data, fmt, width, height = compress_image_bytes(
        raw, max_width=max_width, format=format, quality=quality
    )
    return {
        "data": data,
        "format": fmt,
        "width": width,
        "height": height,
        "original_bytes": len(raw),
        "encoded_bytes": len(data),
        "path": str(path),
    }


def capture_screen(
    *,
    monitor: str = "all",
    max_width: int = 1600,
    format: str = "jpeg",
    quality: int = DEFAULT_QUALITY,
) -> dict:
    """Capture the local screen and return compressed image data.

    ``monitor="all"`` captures every attached display; anything else captures
    only the primary display.
    """
    _pil_image()  # dependency guard with a clear message
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=(str(monitor).strip().lower() == "all"))
    data, fmt, width, height = encode_pil_image(
        img, max_width=max_width, format=format, quality=quality
    )
    return {
        "data": data,
        "format": fmt,
        "width": width,
        "height": height,
        "encoded_bytes": len(data),
    }
