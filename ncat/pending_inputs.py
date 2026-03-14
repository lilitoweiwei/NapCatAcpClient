"""Per-chat buffering for attachment-only inputs."""

from __future__ import annotations

import time

from ncat.models import ImageAttachment, PendingChatInput, SavedFileAttachment


class PendingInputStore:
    """Store files and images until the chat sends a text-bearing message."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._items: dict[str, PendingChatInput] = {}

    def _now(self) -> float:
        return time.monotonic()

    def _fresh_item(self) -> PendingChatInput:
        now = self._now()
        return PendingChatInput(created_at=now, updated_at=now)

    def _get_or_create(self, chat_id: str) -> PendingChatInput:
        item = self._items.get(chat_id)
        if item is None:
            item = self._fresh_item()
            self._items[chat_id] = item
        return item

    def cleanup_expired(self) -> None:
        if self._ttl_seconds <= 0:
            return
        now = self._now()
        expired = [
            chat_id
            for chat_id, item in self._items.items()
            if now - item.updated_at > self._ttl_seconds
        ]
        for chat_id in expired:
            self._items.pop(chat_id, None)

    def add_files(self, chat_id: str, files: list[SavedFileAttachment]) -> None:
        if not files:
            return
        item = self._get_or_create(chat_id)
        item.files.extend(files)
        item.updated_at = self._now()

    def add_images(self, chat_id: str, images: list[ImageAttachment]) -> None:
        if not images:
            return
        item = self._get_or_create(chat_id)
        item.images.extend(images)
        item.updated_at = self._now()

    def peek(self, chat_id: str) -> PendingChatInput | None:
        return self._items.get(chat_id)

    def pop_all(self, chat_id: str) -> PendingChatInput | None:
        return self._items.pop(chat_id, None)

    def clear(self, chat_id: str) -> None:
        self._items.pop(chat_id, None)

    def clear_all(self) -> None:
        self._items.clear()
