"""Test doubles for the ACP agent layer.

This module provides lightweight mocks that simulate the public API used by
`ncat.dispatcher`, `ncat.prompt_runner`, and `ncat.napcat_server` without
spawning a real ACP agent subprocess.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ncat.agent_manager import (
    MSG_AGENT_NOT_CONNECTED,
    AgentErrorWithPartialContent,
)
from ncat.models import ContentPart, VisibleTurnEvent


class MockAgentManager:
    """Mock AgentManager for testing without a real ACP agent subprocess.

    Provides configurable response text, delay, and call tracking.
    """

    def __init__(self) -> None:
        # Recorded prompt calls: list of (chat_id, text)
        self.calls: list[tuple[str, str]] = []
        # Recorded prompt blocks (ACP ContentBlocks) for assertions in image tests
        self.calls_blocks: list[tuple[str, list[Any]]] = []
        # Text to return from send_prompt
        self.response_text: str = "Mock AI response"
        # Optional richer response for image tests
        self.response_parts: list[ContentPart] | None = None
        # Delay in seconds before returning response (for timeout/cancel tests)
        self.delay: float = 0
        # Chat IDs that have been cancelled
        self.cancelled: set[str] = set()
        self.cancel_calls: list[str] = []
        # Chat IDs whose sessions have been closed
        self.closed_sessions: set[str] = set()
        self.disconnect_calls: list[str | None] = []
        # Whether all sessions have been closed
        self.all_sessions_closed: bool = False
        # Whether send_prompt should raise RuntimeError (simulates agent crash)
        self.should_crash: bool = False
        # When set, send_prompt raises AgentErrorWithPartialContent(cause, parts)
        self.raise_error_with_parts: tuple[BaseException, list[ContentPart]] | None = None
        # Whether agent is connected (False simulates "agent not connected")
        self._is_running: bool = True
        self.ensure_connection_error: BaseException | None = None
        # Agent capability flag (mirrors real AgentManager.supports_image)
        self._supports_image: bool = False
        # One-shot workspace selected by /new for the next session
        self.next_session_cwds: dict[str, str | None] = {}
        self.workspace_cwds: dict[str, str] = {}
        # Connection establishment bookkeeping
        self.ensure_connection_calls: list[str] = []
        # Session lifecycle bookkeeping
        self._session_counter: int = 0
        self.session_ids_by_chat: dict[str, str] = {}
        self.new_session_calls: list[tuple[str, str | None]] = []
        self.prompt_session_ids: list[tuple[str, str]] = []
        self._visible_event_notifiers: dict[str, Callable[[], Awaitable[None]]] = {}
        self._pending_visible_flushes: dict[
            str,
            list[tuple[list[ContentPart], VisibleTurnEvent]],
        ] = {}
        self.stream_steps: list[tuple[float, list[ContentPart], VisibleTurnEvent]] = []

    def is_running(self, chat_id: str) -> bool:
        return self._is_running

    def supports_image(self, chat_id: str) -> bool:
        return self._supports_image

    def get_chat_id(self, session_id: str) -> str | None:
        """Reverse lookup (mock): extract chat_id from mock session_id format."""
        prefix = "mock_session_"
        if session_id.startswith(prefix):
            parts = session_id.split("_", 2)
            if len(parts) == 3:
                return parts[2]
        return None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def ensure_connection(self, chat_id: str) -> None:
        self.ensure_connection_calls.append(chat_id)
        if self.ensure_connection_error is not None:
            raise self.ensure_connection_error
        self._is_running = True

    async def disconnect(self, chat_id: str | None = None) -> None:
        """Disconnect specific chat or all chats (if chat_id is None)."""
        self.disconnect_calls.append(chat_id)
        if chat_id is None:
            self.session_ids_by_chat.clear()
        else:
            self.session_ids_by_chat.pop(chat_id, None)

    async def get_or_create_session(self, chat_id: str) -> str:
        session_id = self.session_ids_by_chat.get(chat_id)
        if session_id is None:
            self._session_counter += 1
            session_id = f"mock_session_{self._session_counter}_{chat_id}"
            self.session_ids_by_chat[chat_id] = session_id
            self.new_session_calls.append((chat_id, self.next_session_cwds.get(chat_id)))
        return session_id

    def set_next_session_cwd(self, chat_id: str, dir_or_none: str | None) -> None:
        """Record the requested workspace for the next session."""
        self.next_session_cwds[chat_id] = dir_or_none

    def get_workspace_cwd(self, chat_id: str) -> str:
        return self.workspace_cwds.get(chat_id, f"/workspace/{self.next_session_cwds.get(chat_id) or 'default'}")

    async def close_session(self, chat_id: str) -> None:
        self.closed_sessions.add(chat_id)
        self.session_ids_by_chat.pop(chat_id, None)

    async def close_all_sessions(self) -> None:
        self.all_sessions_closed = True

    def set_visible_event_notifier(
        self,
        chat_id: str,
        notifier: Callable[[], Awaitable[None]] | None,
    ) -> None:
        if notifier is None:
            self._visible_event_notifiers.pop(chat_id, None)
            return
        self._visible_event_notifiers[chat_id] = notifier

    def drain_visible_event_flushes(
        self,
        chat_id: str,
        sent_part_count: int,
    ) -> tuple[list[tuple[list[ContentPart], VisibleTurnEvent]], int]:
        flushes = self._pending_visible_flushes.pop(chat_id, [])
        next_sent = sent_part_count + sum(len(parts) for parts, _ in flushes)
        return flushes, next_sent

    def clear_completed_turn_state(self, chat_id: str) -> None:
        self._pending_visible_flushes.pop(chat_id, None)

    def queue_visible_flush(
        self,
        chat_id: str,
        parts: list[ContentPart],
        visible_event: VisibleTurnEvent,
    ) -> None:
        self._pending_visible_flushes.setdefault(chat_id, []).append((parts, visible_event))

    def accumulate_text(self, session_id: str, text: str) -> None:
        pass

    def is_busy(self, chat_id: str) -> bool:
        # Busy tracking is done by PromptRunner, not AgentManager
        return False

    async def send_prompt(self, chat_id: str, prompt: Any) -> list[ContentPart]:
        """Simulate sending a prompt. Records call, waits for delay, returns response."""
        if not self._is_running:
            raise RuntimeError(MSG_AGENT_NOT_CONNECTED)
        session_id = await self.get_or_create_session(chat_id)
        self.prompt_session_ids.append((chat_id, session_id))
        text = prompt if isinstance(prompt, str) else ""
        if isinstance(prompt, list):
            self.calls_blocks.append((chat_id, prompt))
            if prompt:
                # The first block is expected to be a TextContentBlock.
                first = prompt[0]
                text = str(getattr(first, "text", first))

        self.calls.append((chat_id, text))
        if self.should_crash:
            raise RuntimeError("Agent crashed")
        if self.raise_error_with_parts is not None:
            cause, parts = self.raise_error_with_parts
            raise AgentErrorWithPartialContent(cause, parts)
        streamed_parts: list[ContentPart] = []
        for delay, parts, visible_event in self.stream_steps:
            if delay > 0:
                await asyncio.sleep(delay)
            streamed_parts.extend(parts)
            self.queue_visible_flush(chat_id, parts, visible_event)
            notifier = self._visible_event_notifiers.get(chat_id)
            if notifier is not None:
                await notifier()
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.response_parts is not None:
            return [*streamed_parts, *self.response_parts]
        return [*streamed_parts, ContentPart(type="text", text=self.response_text)]

    async def cancel(self, chat_id: str) -> bool:
        self.cancelled.add(chat_id)
        self.cancel_calls.append(chat_id)
        return True
