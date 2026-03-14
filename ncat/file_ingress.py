"""Helpers for receiving and saving QQ files into chat workspaces."""

from __future__ import annotations

import logging
import mimetypes
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ncat.log import warning_event
from ncat.models import DownloadedImage, FileAttachment, SavedFileAttachment

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    GetFileFn = Callable[[str], Awaitable[dict[str, Any] | None]]
else:
    GetFileFn = Any

logger = logging.getLogger("ncat.file_ingress")


def sanitize_filename(name: str) -> str:
    """Return a filesystem-safe filename while preserving a readable stem."""
    candidate = (name or "").strip().replace("\\", "_").replace("/", "_")
    candidate = candidate.replace("\x00", "").strip(" .")
    return candidate or "qq-file"


def ensure_inbox_dir(workspace_cwd: str, inbox_dirname: str) -> Path:
    """Create and return the per-workspace QQ inbox directory."""
    inbox = Path(workspace_cwd).expanduser().resolve() / inbox_dirname
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def allocate_target_path(inbox_dir: Path, raw_name: str) -> Path:
    """Choose a non-conflicting filename inside the inbox directory."""
    safe_name = sanitize_filename(raw_name)
    candidate = inbox_dir / safe_name
    if not candidate.exists():
        return candidate

    stem = candidate.stem or "qq-file"
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = inbox_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def file_hint_text(saved_files: list[SavedFileAttachment]) -> str:
    """Build the system hint appended to prompts for saved files."""
    if not saved_files:
        return ""
    if len(saved_files) == 1:
        note = saved_files[0].prompt_note or "The user attached a file."
        return (
            f"[SYSTEM: {note} It has been saved at "
            f"{saved_files[0].saved_path}]"
        )

    lines = [
        "[SYSTEM: The user attached files. They have been saved at:",
        *[f"- {item.saved_path}" for item in saved_files],
    ]
    return "\n".join(lines) + "]"


def guess_mime_type_from_name(name: str) -> str | None:
    """Best-effort MIME guess from a filename or URL path."""
    mime_type, _ = mimetypes.guess_type(name)
    return mime_type


def build_saved_image_note(reason: str = "") -> str:
    """Build the system hint used when an image is surfaced as a saved file."""
    base = "The user attached an image"
    if reason:
        base += f". {reason}"
    else:
        base += "."
    return base


def save_downloaded_image(
    image: DownloadedImage,
    *,
    workspace_cwd: str,
    inbox_dirname: str,
    prompt_note: str,
) -> SavedFileAttachment:
    """Persist a downloaded image into the workspace inbox."""
    inbox_dir = ensure_inbox_dir(workspace_cwd, inbox_dirname)
    target_path = allocate_target_path(inbox_dir, image.suggested_name)
    target_path.write_bytes(image.data)
    return SavedFileAttachment(
        name=target_path.name,
        saved_path=str(target_path),
        original_file_id=image.url,
        size=len(image.data),
        kind="image",
        prompt_note=prompt_note,
    )


async def _download_to_path(url: str, target_path: Path, timeout_seconds: float) -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds) as client:
        response = await client.get(url)
        response.raise_for_status()
        target_path.write_bytes(response.content)


async def download_private_file(
    attachment: FileAttachment,
    *,
    workspace_cwd: str,
    inbox_dirname: str,
    timeout_seconds: float,
    max_file_size_mb: int | None,
    get_file: GetFileFn | None = None,
) -> SavedFileAttachment:
    """Persist a private QQ file into the chat workspace and return its local metadata."""
    inbox_dir = ensure_inbox_dir(workspace_cwd, inbox_dirname)
    target_path = allocate_target_path(inbox_dir, attachment.name)

    resolved_url = attachment.url.strip()
    local_source: Path | None = None
    if not resolved_url and get_file is not None and attachment.file_id:
        api_data = await get_file(attachment.file_id)
        if isinstance(api_data, dict):
            maybe_url = str(api_data.get("url") or api_data.get("file") or "").strip()
            if maybe_url.startswith(("http://", "https://")):
                resolved_url = maybe_url
            elif maybe_url:
                local_source = Path(maybe_url).expanduser()

    if local_source is not None:
        if not local_source.exists():
            raise FileNotFoundError(f"NapCat returned missing file path: {local_source}")
        shutil.copy2(local_source, target_path)
    elif resolved_url:
        await _download_to_path(resolved_url, target_path, timeout_seconds)
    else:
        raise ValueError(f"Cannot resolve download source for file_id={attachment.file_id!r}")

    if max_file_size_mb is not None:
        max_bytes = max_file_size_mb * 1024 * 1024
        file_size = target_path.stat().st_size
        if file_size > max_bytes:
            target_path.unlink(missing_ok=True)
            raise ValueError(f"File exceeds configured size limit ({max_file_size_mb} MiB)")

    return SavedFileAttachment(
        name=target_path.name,
        saved_path=str(target_path),
        original_file_id=attachment.file_id,
        size=attachment.size,
    )


async def best_effort_download_private_file(**kwargs) -> SavedFileAttachment | None:
    """Download a QQ private file, logging and swallowing recoverable failures."""
    try:
        return await download_private_file(**kwargs)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        attachment: FileAttachment = kwargs["attachment"]
        warning_event(
            logger,
            "file_download_fail",
            "Failed to save QQ file into workspace inbox",
            file_id=attachment.file_id,
            file_name=attachment.name,
            err=str(exc),
        )
        return None
