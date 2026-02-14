"""Test doubles for the ACP agent layer.

This module provides lightweight mocks that simulate the public API used by
`ncat.dispatcher`, `ncat.prompt_runner`, and `ncat.napcat_server` without
spawning a real ACP agent subprocess.
"""

import asyncio
from typing import Any

from ncat.permission import PermissionBroker


class MockAgentManager:
    """Mock AgentManager for testing without a real ACP agent subprocess.

    Provides configurable response text, delay, and call tracking.
    """

    def __init__(self) -> None:
        # Recorded prompt calls: list of (chat_id, text)
        self.calls: list[tuple[str, str]] = []
        # Text to return from send_prompt
        self.response_text: str = "Mock AI response"
        # Delay in seconds before returning response (for timeout/cancel tests)
        self.delay: float = 0
        # Chat IDs that have been cancelled
        self.cancelled: set[str] = set()
        # Chat IDs whose sessions have been closed
        self.closed_sessions: set[str] = set()
        # Whether all sessions have been closed
        self.all_sessions_closed: bool = False
        # Whether send_prompt should raise RuntimeError (simulates agent crash)
        self.should_crash: bool = False
        # Permission broker (set externally, mirrors real AgentManager)
        self._permission_broker: PermissionBroker | None = None
        # Last event per chat (for permission reply routing)
        self._last_events: dict[str, dict[str, Any]] = {}

    @property
    def is_running(self) -> bool:
        return True

    @property
    def permission_broker(self) -> PermissionBroker | None:
        return self._permission_broker

    @permission_broker.setter
    def permission_broker(self, broker: PermissionBroker) -> None:
        self._permission_broker = broker

    def get_chat_id(self, session_id: str) -> str | None:
        """Reverse lookup (mock): extract chat_id from mock session_id format."""
        # Mock session IDs have the format "mock_session_{chat_id}"
        prefix = "mock_session_"
        if session_id.startswith(prefix):
            return session_id[len(prefix) :]
        return None

    def get_last_event(self, chat_id: str) -> dict[str, Any] | None:
        return self._last_events.get(chat_id)

    def set_last_event(self, chat_id: str, event: dict[str, Any]) -> None:
        self._last_events[chat_id] = event

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def get_or_create_session(self, chat_id: str) -> str:
        return f"mock_session_{chat_id}"

    async def close_session(self, chat_id: str) -> None:
        self.closed_sessions.add(chat_id)

    async def close_all_sessions(self) -> None:
        self.all_sessions_closed = True

    def accumulate_text(self, session_id: str, text: str) -> None:
        pass

    def is_busy(self, chat_id: str) -> bool:
        # Busy tracking is done by PromptRunner, not AgentManager
        return False

    async def send_prompt(self, chat_id: str, text: str) -> str:
        """Simulate sending a prompt. Records call, waits for delay, returns response."""
        self.calls.append((chat_id, text))
        if self.should_crash:
            raise RuntimeError("Agent crashed")
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.response_text

    async def cancel(self, chat_id: str) -> bool:
        self.cancelled.add(chat_id)
        return True
