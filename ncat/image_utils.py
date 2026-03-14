"""Image helpers for ncat."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from ncat.file_ingress import guess_mime_type_from_name
from ncat.log import warning_event
from ncat.models import DownloadedImage

logger = logging.getLogger("ncat.image_utils")


def _normalize_mime_type(content_type: str | None) -> str | None:
    """Normalize a Content-Type header value into a MIME type string."""
    if not content_type:
        return None
    mime_type = content_type.split(";", 1)[0].strip().lower()
    return mime_type or None


def _suggest_filename(url: str, mime_type: str) -> str:
    """Best-effort filename for a downloaded QQ image."""
    raw_name = Path(url.split("?", 1)[0]).name or "qq-image"
    if "." in raw_name:
        return raw_name
    guessed = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime_type, "")
    return f"{raw_name}{guessed}"


async def download_image(url: str, timeout_seconds: float) -> DownloadedImage | None:
    """Download an image and return bytes plus metadata.

    Returns None on failure. Callers should fall back to sending the URL to the agent.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            # Prefer the server's Content-Type; fall back to a URL-based guess.
            mime_type = (
                _normalize_mime_type(resp.headers.get("Content-Type"))
                or guess_mime_type_from_name(url)
                or "image/png"
            )
            return DownloadedImage(
                url=url,
                data=resp.content,
                mime_type=mime_type,
                suggested_name=_suggest_filename(url, mime_type),
            )
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        warning_event(
            logger,
            "image_download_fail",
            "Failed to download image; falling back to URL",
            url=url,
            err=str(e),
        )
        return None


def encode_image_base64(image: DownloadedImage) -> tuple[str, str]:
    """Encode a downloaded image for ACP image blocks."""
    return base64.b64encode(image.data).decode("ascii"), image.mime_type
