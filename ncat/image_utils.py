"""Image helpers for ncat."""

from __future__ import annotations

import base64
import io
import logging

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from ncat.file_ingress import guess_mime_type_from_name
from ncat.log import info_event, warning_event
from ncat.models import DownloadedImage

logger = logging.getLogger("ncat.image_utils")

_DEFAULT_MAX_INLINE_BYTES = 2 * 1024 * 1024
_SCALE_STEPS = (1.0, 0.85, 0.7, 0.55, 0.4, 0.3, 0.2)
_JPEG_QUALITIES = (85, 75, 65, 55, 45, 35)
_WEBP_QUALITIES = (85, 75, 65, 55, 45, 35)


class ImagePreparationError(ValueError):
    """Raised when an image cannot be safely prepared for inline delivery."""


def _normalize_mime_type(content_type: str | None) -> str | None:
    """Normalize a Content-Type header value into a MIME type string."""
    if not content_type:
        return None
    mime_type = content_type.split(";", 1)[0].strip().lower()
    return mime_type or None


def _has_alpha(image: Image.Image) -> bool:
    """Return True if the image contains transparency information."""
    if "A" in image.getbands():
        return True
    return image.mode == "P" and "transparency" in image.info


def _resize(image: Image.Image, scale: float) -> Image.Image:
    """Return a scaled copy of the image."""
    if scale >= 0.999:
        return image.copy()
    width = max(1, int(image.width * scale))
    height = max(1, int(image.height * scale))
    if width == image.width and height == image.height:
        return image.copy()
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _save_png_candidate(image: Image.Image) -> bytes:
    """Encode an image as optimized PNG bytes."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _save_webp_candidate(image: Image.Image, quality: int) -> bytes:
    """Encode an image as WebP bytes."""
    buffer = io.BytesIO()
    source = image if image.mode in ("RGB", "RGBA") else image.convert("RGBA")
    source.save(buffer, format="WEBP", quality=quality, method=6)
    return buffer.getvalue()


def _save_jpeg_candidate(image: Image.Image, quality: int) -> bytes:
    """Encode an image as JPEG bytes."""
    buffer = io.BytesIO()
    image.convert("RGB").save(
        buffer,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
    )
    return buffer.getvalue()


def _open_image(data: bytes) -> Image.Image:
    """Open image bytes via Pillow and normalize orientation."""
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            return ImageOps.exif_transpose(opened)
    except UnidentifiedImageError as exc:
        raise ImagePreparationError("无法识别图片格式，无法发送给 Agent。") from exc
    except OSError as exc:
        raise ImagePreparationError("图片读取失败，无法发送给 Agent。") from exc


def _prepare_transparent_image(image: Image.Image, max_inline_bytes: int) -> tuple[bytes, str]:
    """Prepare a transparent image, preferring PNG and falling back to WebP."""
    best_png: bytes | None = None
    best_webp: bytes | None = None

    for scale in _SCALE_STEPS:
        resized = _resize(image, scale)
        png_bytes = _save_png_candidate(resized)
        if best_png is None or len(png_bytes) < len(best_png):
            best_png = png_bytes
        if len(png_bytes) <= max_inline_bytes:
            return png_bytes, "image/png"

        for quality in _WEBP_QUALITIES:
            webp_bytes = _save_webp_candidate(resized, quality)
            if best_webp is None or len(webp_bytes) < len(best_webp):
                best_webp = webp_bytes
            if len(webp_bytes) <= max_inline_bytes:
                return webp_bytes, "image/webp"

    smallest = best_webp if best_webp and (best_png is None or len(best_webp) < len(best_png)) else best_png
    if smallest is not None:
        raise ImagePreparationError(
            f"图片过大，压缩后仍超过 {max_inline_bytes // (1024 * 1024)} MiB，无法发送给 Agent。"
        )
    raise ImagePreparationError("图片压缩失败，无法发送给 Agent。")


def _prepare_opaque_image(image: Image.Image, max_inline_bytes: int) -> tuple[bytes, str]:
    """Prepare an opaque image as JPEG."""
    best_jpeg: bytes | None = None
    for scale in _SCALE_STEPS:
        resized = _resize(image, scale)
        for quality in _JPEG_QUALITIES:
            jpeg_bytes = _save_jpeg_candidate(resized, quality)
            if best_jpeg is None or len(jpeg_bytes) < len(best_jpeg):
                best_jpeg = jpeg_bytes
            if len(jpeg_bytes) <= max_inline_bytes:
                return jpeg_bytes, "image/jpeg"
    raise ImagePreparationError(
        f"图片过大，压缩后仍超过 {max_inline_bytes // (1024 * 1024)} MiB，无法发送给 Agent。"
    )


def prepare_image_for_inline(
    image: DownloadedImage,
    *,
    max_inline_bytes: int = _DEFAULT_MAX_INLINE_BYTES,
) -> DownloadedImage:
    """Normalize and compress an image for inline ACP delivery."""
    opened = _open_image(image.data)
    has_alpha = _has_alpha(opened)
    info_event(
        logger,
        "image_preprocess_start",
        "Preparing image for inline ACP delivery",
        url=image.url,
        original_mime=image.mime_type,
        original_size_bytes=len(image.data),
        max_inline_bytes=max_inline_bytes,
        has_alpha=has_alpha,
        width=opened.width,
        height=opened.height,
    )

    if has_alpha:
        final_bytes, final_mime = _prepare_transparent_image(opened, max_inline_bytes)
    else:
        final_bytes, final_mime = _prepare_opaque_image(opened, max_inline_bytes)

    info_event(
        logger,
        "image_preprocess_complete",
        "Prepared image for inline ACP delivery",
        url=image.url,
        original_mime=image.mime_type,
        final_mime=final_mime,
        original_size_bytes=len(image.data),
        final_size_bytes=len(final_bytes),
        max_inline_bytes=max_inline_bytes,
        has_alpha=has_alpha,
    )
    return DownloadedImage(
        url=image.url,
        data=final_bytes,
        mime_type=final_mime,
    )


async def download_image(url: str, timeout_seconds: float) -> DownloadedImage | None:
    """Download an image and return bytes plus metadata."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            mime_type = (
                _normalize_mime_type(resp.headers.get("Content-Type"))
                or guess_mime_type_from_name(url)
                or "image/png"
            )
            return DownloadedImage(
                url=url,
                data=resp.content,
                mime_type=mime_type,
            )
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        warning_event(
            logger,
            "image_download_fail",
            "Failed to download image for inline delivery",
            url=url,
            err=str(e),
        )
        return None


def encode_image_base64(image: DownloadedImage) -> tuple[str, str]:
    """Encode a prepared image for ACP image blocks."""
    return base64.b64encode(image.data).decode("ascii"), image.mime_type
