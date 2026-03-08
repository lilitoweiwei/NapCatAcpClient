"""Tests for the unified command registry and handlers."""

import pytest

from ncat.command import command_registry, get_help_text
from tests.mock_agent import MockAgentManager

pytestmark = pytest.mark.asyncio


class ReplyCollector:
    """Collect reply texts emitted by command handlers."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, event: dict, text: str) -> None:
        self.texts.append(text)


async def test_new_command_sets_workspace_and_replies() -> None:
    replies = ReplyCollector()
    agent = MockAgentManager()

    matched = await command_registry.execute(
        "/new demo",
        chat_id="private:1",
        event={},
        reply_fn=replies,
        agent_manager=agent,
    )

    assert matched is True
    assert replies.texts == ["已创建新会话，AI 上下文已清空。"]
    assert agent.next_session_cwds["private:1"] == "demo"
    assert "private:1" in agent.closed_sessions
    assert agent.disconnect_calls == ["private:1"]


async def test_new_command_without_workspace_keeps_default_selection() -> None:
    replies = ReplyCollector()
    agent = MockAgentManager()

    matched = await command_registry.execute(
        "/new",
        chat_id="private:1",
        event={},
        reply_fn=replies,
        agent_manager=agent,
    )

    assert matched is True
    assert replies.texts == ["已创建新会话，AI 上下文已清空。"]
    assert agent.next_session_cwds["private:1"] is None
    assert agent.disconnect_calls == ["private:1"]


async def test_new_command_cancels_active_turn_before_disconnect() -> None:
    replies = ReplyCollector()
    agent = MockAgentManager()
    cancelled: list[str] = []

    matched = await command_registry.execute(
        "/new demo",
        chat_id="private:1",
        event={},
        reply_fn=replies,
        agent_manager=agent,
        cancel_fn=lambda chat_id: cancelled.append(chat_id) or True,
    )

    assert matched is True
    assert cancelled == ["private:1"]
    assert agent.disconnect_calls == ["private:1"]


async def test_new_command_reports_workspace_validation_errors() -> None:
    replies = ReplyCollector()
    agent = MockAgentManager()

    def _raise(chat_id: str, dir_or_none: str | None) -> None:
        raise ValueError("工作区名称不能逃逸出 workspace_root。")

    agent.set_next_session_cwd = _raise

    matched = await command_registry.execute(
        "/new ../bad",
        chat_id="private:1",
        event={},
        reply_fn=replies,
        agent_manager=agent,
    )

    assert matched is True
    assert replies.texts == ["工作区无效：工作区名称不能逃逸出 workspace_root。"]
    assert "private:1" not in agent.closed_sessions


async def test_stop_command_replies_when_cancelled() -> None:
    replies = ReplyCollector()

    matched = await command_registry.execute(
        "/stop",
        chat_id="private:1",
        event={},
        reply_fn=replies,
        cancel_fn=lambda chat_id: True,
    )

    assert matched is True
    assert replies.texts == ["已中断当前 AI 思考。"]


async def test_stop_command_replies_when_idle() -> None:
    replies = ReplyCollector()

    matched = await command_registry.execute(
        "/stop",
        chat_id="private:1",
        event={},
        reply_fn=replies,
        cancel_fn=lambda chat_id: False,
    )

    assert matched is True
    assert replies.texts == ["当前没有进行中的 AI 思考。"]


async def test_send_command_forwards_body_verbatim() -> None:
    replies = ReplyCollector()

    matched = await command_registry.execute(
        "/send /help",
        chat_id="private:1",
        event={},
        reply_fn=replies,
    )

    assert matched is True
    assert replies.texts == ["/help"]


async def test_send_command_without_payload_returns_help() -> None:
    replies = ReplyCollector()

    matched = await command_registry.execute(
        "/send",
        chat_id="private:1",
        event={},
        reply_fn=replies,
    )

    assert matched is True
    assert len(replies.texts) == 1
    assert "/send <text>" in replies.texts[0]


async def test_help_command_returns_generated_help() -> None:
    replies = ReplyCollector()

    matched = await command_registry.execute(
        "/help",
        chat_id="private:1",
        event={},
        reply_fn=replies,
    )

    assert matched is True
    assert replies.texts == [get_help_text()]


async def test_unknown_command_is_not_matched() -> None:
    replies = ReplyCollector()

    matched = await command_registry.execute(
        "/unknown",
        chat_id="private:1",
        event={},
        reply_fn=replies,
    )

    assert matched is False
    assert replies.texts == []


async def test_non_command_text_is_not_matched() -> None:
    replies = ReplyCollector()

    matched = await command_registry.execute(
        "hello",
        chat_id="private:1",
        event={},
        reply_fn=replies,
    )

    assert matched is False
    assert replies.texts == []
