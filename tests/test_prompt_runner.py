"""Unit tests for the prompt lifecycle manager (PromptRunner)."""

import asyncio
import contextlib

import pytest

from ncat.models import ParsedMessage
from ncat.prompt_runner import PromptRunner
from tests.mock_agent import MockAgentManager

pytestmark = pytest.mark.asyncio


class ReplyQueue:
    """Collect outgoing replies for assertions."""

    def __init__(self) -> None:
        self.items: asyncio.Queue[str] = asyncio.Queue()
        self.texts: list[str] = []

    async def __call__(self, event: dict, text: str) -> None:
        self.texts.append(text)
        self.items.put_nowait(text)

    async def get_text(self, timeout: float = 1.0) -> str:
        """Get the next reply text with a timeout."""
        return await asyncio.wait_for(self.items.get(), timeout=timeout)


class PermissionBrokerSpy:
    """Spy that records cancel_pending() calls made by PromptRunner.cancel()."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_pending(self, chat_id: str) -> None:
        self.cancelled.append(chat_id)


def _parsed_private(chat_id: str, sender_id: int, sender_name: str, text: str) -> ParsedMessage:
    """Build a ParsedMessage for a private chat."""
    return ParsedMessage(
        chat_id=chat_id,
        text=text,
        is_at_bot=False,
        sender_name=sender_name,
        sender_id=sender_id,
        group_name=None,
        message_type="private",
    )


async def test_process_sends_agent_reply() -> None:
    """process() should send the agent response as a QQ reply."""
    agent = MockAgentManager()
    replies = ReplyQueue()
    perm = PermissionBrokerSpy()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        permission_broker=perm,  # not used in process(), only in cancel()
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    parsed = _parsed_private("private:111", 111, "Alice", "hello")
    await runner.process(parsed, event={"user_id": 111})

    text = await replies.get_text()
    assert text == "Mock AI response"

    assert len(agent.calls) == 1
    _, prompt = agent.calls[0]
    assert "[Private chat, user Alice(111)]" in prompt
    assert "hello" in prompt


async def test_process_empty_response_sends_warning() -> None:
    """Empty agent response should produce a warning reply."""
    agent = MockAgentManager()
    agent.response_text = ""
    replies = ReplyQueue()
    perm = PermissionBrokerSpy()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        permission_broker=perm,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "x"), event={})
    assert await replies.get_text() == "AI 未返回有效回复"


async def test_process_runtime_error_closes_session_and_replies_error() -> None:
    """RuntimeError from the agent should close the session and notify the user."""
    agent = MockAgentManager()
    agent.should_crash = True
    replies = ReplyQueue()
    perm = PermissionBrokerSpy()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        permission_broker=perm,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "x"), event={})

    text = await replies.get_text()
    assert "Agent 异常" in text
    assert "Agent crashed" in text
    assert "private:111" in agent.closed_sessions


async def test_is_busy_true_while_processing() -> None:
    """is_busy() should be True while an AI task is running for a chat."""
    agent = MockAgentManager()
    agent.delay = 0.2
    replies = ReplyQueue()
    perm = PermissionBrokerSpy()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        permission_broker=perm,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    parsed = _parsed_private("private:111", 111, "Alice", "hello")
    task = asyncio.create_task(runner.process(parsed, event={}))

    for _ in range(200):
        if runner.is_busy("private:111"):
            break
        await asyncio.sleep(0)
    assert runner.is_busy("private:111") is True

    await task
    assert runner.is_busy("private:111") is False


async def test_cancel_cancels_active_task_and_permission_pending() -> None:
    """cancel() should cancel the active task, cancel pending permission, and notify the agent."""
    agent = MockAgentManager()
    agent.delay = 5.0
    replies = ReplyQueue()
    perm = PermissionBrokerSpy()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        permission_broker=perm,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    parsed = _parsed_private("private:111", 111, "Alice", "hello")
    task = asyncio.create_task(runner.process(parsed, event={}))

    for _ in range(200):
        if runner.is_busy("private:111"):
            break
        await asyncio.sleep(0)

    assert runner.cancel("private:111") is True

    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()

    # cancel() should also propagate to PermissionBroker and agent cancel().
    assert perm.cancelled == ["private:111"]

    # agent.cancel() runs in a background task; yield control once.
    await asyncio.sleep(0)
    assert "private:111" in agent.cancelled

    assert runner.is_busy("private:111") is False


async def test_thinking_notifications_fire_when_slow() -> None:
    """Thinking notification timers should fire before the agent response."""
    agent = MockAgentManager()
    agent.delay = 0.15
    replies = ReplyQueue()
    perm = PermissionBrokerSpy()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        permission_broker=perm,
        thinking_notify_seconds=0.05,
        thinking_long_notify_seconds=0,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "hello"), event={})

    # First message should be the "thinking" notification, followed by the final response.
    first = await replies.get_text()
    second = await replies.get_text()
    assert "正在思考" in first
    assert second == "Mock AI response"


async def test_long_thinking_notification_fires_when_very_slow() -> None:
    """Long-thinking notification should fire when agent is very slow."""
    agent = MockAgentManager()
    agent.delay = 0.25
    replies = ReplyQueue()
    perm = PermissionBrokerSpy()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        permission_broker=perm,
        thinking_notify_seconds=0.05,
        thinking_long_notify_seconds=0.1,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "hello"), event={})

    texts = replies.texts
    assert any("正在思考" in t for t in texts)
    assert any("/stop" in t for t in texts)
    assert "Mock AI response" in texts
