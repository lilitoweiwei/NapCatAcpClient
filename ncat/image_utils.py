"""Image helpers for ncat.

Currently used to download images from NapCat-provided URLs and encode them as
base64 for ACP `ImageContentBlock`.
"""

from __future__ import annotations

import base64
import logging
import mimetypes

import httpx

logger = logging.getLogger("ncat.image_utils")


def _normalize_mime_type(content_type: str | None) -> str | None:
    """Normalize a Content-Type header value into a MIME type string."""
    if not content_type:
        return None
    mime_type = content_type.split(";", 1)[0].strip().lower()
    return mime_type or None


def _guess_mime_type_from_url(url: str) -> str | None:
    """Best-effort MIME type guess based on URL path/extension."""
    mime_type, _ = mimetypes.guess_type(url)
    return mime_type


async def download_image(url: str, timeout_seconds: float) -> tuple[str, str] | None:
    """Download an image and return (base64_data, mime_type).

    Returns None on failure. Callers should fall back to sending the URL to the agent.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            # Prefer the server's Content-Type; fall back to a URL-based guess.
            mime_type = (
                _normalize_mime_type(resp.headers.get("Content-Type"))
                or _guess_mime_type_from_url(url)
                or "image/png"
            )
            data_b64 = base64.b64encode(resp.content).decode("ascii")
            return data_b64, mime_type
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        logger.warning("Failed to download image, will fall back to URL: %s (%s)", url, e)
        return None
