"""Tests for inline image preprocessing."""

import io

from PIL import Image

import ncat.image_utils as image_utils
from ncat.image_utils import ImagePreparationError, prepare_image_for_inline
from ncat.models import DownloadedImage


def _image_bytes(mode: str, size: tuple[int, int], color, fmt: str) -> bytes:
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def test_prepare_opaque_image_transcodes_to_jpeg() -> None:
    downloaded = DownloadedImage(
        url="http://example.com/opaque.png",
        data=_image_bytes("RGB", (64, 64), (200, 20, 20), "PNG"),
        mime_type="image/png",
    )

    prepared = prepare_image_for_inline(downloaded, max_inline_bytes=128 * 1024)

    assert prepared.mime_type == "image/jpeg"
    assert len(prepared.data) <= 128 * 1024


def test_prepare_transparent_image_keeps_png_when_small() -> None:
    downloaded = DownloadedImage(
        url="http://example.com/alpha.png",
        data=_image_bytes("RGBA", (32, 32), (0, 255, 0, 80), "PNG"),
        mime_type="image/png",
    )

    prepared = prepare_image_for_inline(downloaded, max_inline_bytes=128 * 1024)

    assert prepared.mime_type == "image/png"
    assert len(prepared.data) <= 128 * 1024


def test_prepare_transparent_image_falls_back_to_webp(monkeypatch) -> None:
    downloaded = DownloadedImage(
        url="http://example.com/alpha-big.png",
        data=_image_bytes("RGBA", (64, 64), (0, 255, 0, 80), "PNG"),
        mime_type="image/png",
    )

    def _fake_save_png_candidate(image):
        return b"x" * (300 * 1024)

    monkeypatch.setattr(image_utils, "_save_png_candidate", _fake_save_png_candidate)

    prepared = prepare_image_for_inline(downloaded, max_inline_bytes=128 * 1024)

    assert prepared.mime_type == "image/webp"
    assert len(prepared.data) <= 128 * 1024


def test_prepare_image_raises_when_budget_cannot_be_met(monkeypatch) -> None:
    downloaded = DownloadedImage(
        url="http://example.com/impossible.png",
        data=_image_bytes("RGB", (64, 64), (255, 255, 255), "PNG"),
        mime_type="image/png",
    )

    def _fake_save_jpeg_candidate(image, quality):
        return b"x" * 4096

    monkeypatch.setattr(image_utils, "_save_jpeg_candidate", _fake_save_jpeg_candidate)

    try:
        prepare_image_for_inline(downloaded, max_inline_bytes=1024)
    except ImagePreparationError as exc:
        assert "无法发送给 Agent" in str(exc)
    else:
        raise AssertionError("expected ImagePreparationError")
