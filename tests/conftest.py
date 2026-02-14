"""Shared pytest fixtures for ncat tests."""

from pathlib import Path

import pytest
import pytest_asyncio

from ncat.session import SessionManager


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Create a temporary config.toml for testing."""
    config = tmp_path / "config.toml"
    config.write_text(
        "[server]\n"
        'host = "127.0.0.1"\n'
        "port = 0\n\n"  # port 0 = auto-assign
        "[opencode]\n"
        'command = "echo"\n'
        'work_dir = "' + str(tmp_path).replace("\\", "/") + '"\n'
        "max_concurrent = 1\n\n"
        "[database]\n"
        'path = "' + str(tmp_path / "test.db").replace("\\", "/") + '"\n\n'
        "[prompt]\n"
        'dir = "prompts"\n'
        'session_init_file = "session_init.md"\n'
        'message_prefix_file = "message_prefix.md"\n\n'
        "[ux]\n"
        "thinking_notify_seconds = 10\n"
        "thinking_long_notify_seconds = 30\n\n"
        "[logging]\n"
        'level = "DEBUG"\n'
        'dir = "' + str(tmp_path / "logs").replace("\\", "/") + '"\n'
        "keep_days = 7\n"
    )
    return config


@pytest_asyncio.fixture
async def session_manager(tmp_path: Path) -> SessionManager:
    """Create a SessionManager with a temporary database."""
    db_path = str(tmp_path / "test.db")
    sm = SessionManager(db_path)
    await sm.init()
    yield sm
    await sm.close()
