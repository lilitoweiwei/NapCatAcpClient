"""Shared pytest fixtures for ncat tests."""

import asyncio
from pathlib import Path

import pytest


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Create a temporary config.toml for testing."""
    config = tmp_path / "config.toml"
    config.write_text(
        "[server]\n"
        'host = "127.0.0.1"\n'
        "port = 0\n\n"  # port 0 = auto-assign
        "[agent]\n"
        'command = "echo"\n'
        "args = []\n"
        'cwd = "' + str(tmp_path).replace("\\", "/") + '"\n\n'
        "[ux]\n"
        "thinking_notify_seconds = 10\n"
        "thinking_long_notify_seconds = 30\n\n"
        "[logging]\n"
        'level = "DEBUG"\n'
        'dir = "' + str(tmp_path / "logs").replace("\\", "/") + '"\n'
        "keep_days = 7\n"
    )
    return config


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

    @property
    def is_running(self) -> bool:
        return True

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
        # Busy tracking is done by AiProcessor, not AgentManager
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
