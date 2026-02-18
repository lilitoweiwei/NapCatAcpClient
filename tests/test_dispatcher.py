"""Tests for the message dispatcher (business logic, no WebSocket needed)."""

import asyncio
import contextlib

import pytest
import pytest_asyncio
from acp.schema import PermissionOption

from ncat.agent_manager import MSG_AGENT_NOT_CONNECTED
from ncat.dispatcher import MessageDispatcher
from ncat.permission import PendingPermission, PermissionBroker
from tests.mock_agent import MockAgentManager
from tests.mock_napcat import MockNapCat

pytestmark = pytest.mark.asyncio

BOT_ID = MockNapCat.BOT_ID


class ReplyCollector:
    """Collects reply texts sent by the handler (mock for reply_fn)."""

    def __init__(self) -> None:
        self.replies: list[tuple[dict, str]] = []

    async def __call__(self, event: dict, text: str) -> None:
        self.replies.append((event, text))

    @property
    def last_text(self) -> str:
        return self.replies[-1][1] if self.replies else ""

    @property
    def texts(self) -> list[str]:
        """All reply texts in order."""
        return [text for _, text in self.replies]


@pytest_asyncio.fixture
async def handler_env():
    """Create a MessageDispatcher with mock agent manager and reply collector."""
    mock_agent = MockAgentManager()
    replies = ReplyCollector()
    # PermissionBroker with long timeout (won't fire in normal tests)
    broker = PermissionBroker(reply_fn=replies, timeout=300)
    mock_agent.permission_broker = broker
    handler = MessageDispatcher(
        agent_manager=mock_agent,
        reply_fn=replies,
        permission_broker=broker,
    )

    yield handler, mock_agent, replies


@pytest_asyncio.fixture
async def timeout_env():
    """Create a handler with short timeout thresholds for testing notifications."""
    mock_agent = MockAgentManager()
    replies = ReplyCollector()
    broker = PermissionBroker(reply_fn=replies, timeout=300)
    mock_agent.permission_broker = broker
    handler = MessageDispatcher(
        agent_manager=mock_agent,
        reply_fn=replies,
        permission_broker=broker,
        thinking_notify_seconds=0.3,
        thinking_long_notify_seconds=0.8,
    )

    yield handler, mock_agent, replies


def _private_event(user_id: int, name: str, text: str) -> dict:
    """Build a minimal private message event."""
    return {
        "self_id": BOT_ID,
        "user_id": user_id,
        "message_type": "private",
        "sender": {"user_id": user_id, "nickname": name, "card": ""},
        "message": [{"type": "text", "data": {"text": text}}],
        "post_type": "message",
    }


def _group_event(
    group_id: int,
    group_name: str,
    user_id: int,
    name: str,
    text: str,
    at_bot: bool = False,
) -> dict:
    """Build a minimal group message event."""
    segments: list[dict] = []
    if at_bot:
        segments.append({"type": "at", "data": {"qq": str(BOT_ID)}})
    segments.append({"type": "text", "data": {"text": text}})
    return {
        "self_id": BOT_ID,
        "user_id": user_id,
        "message_type": "group",
        "group_id": group_id,
        "group_name": group_name,
        "sender": {"user_id": user_id, "nickname": name, "card": ""},
        "message": segments,
        "post_type": "message",
    }


# --- Basic message routing tests ---


async def test_private_message_calls_agent(handler_env) -> None:
    """Test that a private message goes through the full AI pipeline."""
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "Alice", "hello"), BOT_ID)

    assert len(mock_agent.calls) == 1
    assert "hello" in mock_agent.calls[0][1]
    assert replies.last_text == "Mock AI response"


async def test_group_without_at_ignored(handler_env) -> None:
    """Test that group messages without @bot produce no reply."""
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_group_event(222, "G", 111, "Bob", "hi", at_bot=False), BOT_ID)

    assert len(mock_agent.calls) == 0
    assert len(replies.replies) == 0


async def test_group_with_at_processed(handler_env) -> None:
    """Test that group messages with @bot are processed."""
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_group_event(222, "G", 111, "Bob", " hi", at_bot=True), BOT_ID)

    assert len(mock_agent.calls) == 1
    assert replies.last_text == "Mock AI response"


# --- Command tests ---


async def test_agent_not_connected_message_reply(handler_env) -> None:
    """Test that when agent is not connected, a normal message gets MSG_AGENT_NOT_CONNECTED."""
    handler, mock_agent, replies = handler_env
    mock_agent._is_running = False

    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)
    assert replies.last_text == MSG_AGENT_NOT_CONNECTED
    assert len(mock_agent.calls) == 0


async def test_agent_not_connected_new_reply(handler_env) -> None:
    """Test that when agent is not connected, /new gets MSG_AGENT_NOT_CONNECTED."""
    handler, mock_agent, replies = handler_env
    mock_agent._is_running = False

    await handler.handle_message(_private_event(111, "A", "/new"), BOT_ID)
    assert replies.last_text == MSG_AGENT_NOT_CONNECTED
    assert "private:111" not in mock_agent.closed_sessions


async def test_command_new_closes_session(handler_env) -> None:
    """Test /new command closes current session via agent manager."""
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/new"), BOT_ID)
    assert "新会话" in replies.last_text
    assert "private:111" in mock_agent.closed_sessions


async def test_command_help(handler_env) -> None:
    """Test /help command via handler."""
    handler, _, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/help"), BOT_ID)
    assert "/new" in replies.last_text
    assert "/help" in replies.last_text
    assert "/stop" in replies.last_text


async def test_command_unknown(handler_env) -> None:
    """Test unknown command returns help text."""
    handler, _, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/xyz"), BOT_ID)
    assert "/new" in replies.last_text


# --- /send forwarding tests ---


async def test_send_prefix_forwards_to_agent(handler_env) -> None:
    """Test that /send forwards text to the agent without triggering ncat commands."""
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/send /help"), BOT_ID)

    assert replies.last_text == "Mock AI response"
    assert len(mock_agent.calls) == 1
    _, prompt = mock_agent.calls[0]
    assert "/help" in prompt


async def test_send_without_payload_shows_help(handler_env) -> None:
    """Test that bare /send shows help text (usage hint)."""
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/send"), BOT_ID)

    assert len(mock_agent.calls) == 0
    assert "/send <text>" in replies.last_text


async def test_send_prefix_not_matched_when_no_space(handler_env) -> None:
    """Test that /sendxxx is treated as an unknown command, not a forwarded message."""
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/sendxxx"), BOT_ID)

    assert len(mock_agent.calls) == 0
    assert "/new" in replies.last_text


# --- AI response tests ---


async def test_empty_response(handler_env) -> None:
    """Test that empty AI content produces a warning reply."""
    handler, mock_agent, replies = handler_env
    mock_agent.response_text = ""

    await handler.handle_message(_private_event(111, "A", "test"), BOT_ID)
    assert "未返回有效回复" in replies.last_text


async def test_agent_crash_sends_error(handler_env) -> None:
    """Test that agent crash produces user-facing error and closes session."""
    handler, mock_agent, replies = handler_env
    mock_agent.should_crash = True

    await handler.handle_message(_private_event(111, "A", "crash"), BOT_ID)
    assert "Agent 异常" in replies.last_text
    assert "private:111" in mock_agent.closed_sessions


async def test_prompt_includes_context(handler_env) -> None:
    """Test that the prompt sent to the agent includes sender context."""
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "Alice", "write code"), BOT_ID)
    _, prompt = mock_agent.calls[0]
    assert "[Private chat, user Alice(111)]" in prompt
    assert "write code" in prompt


# --- /stop command tests ---


async def test_stop_no_active_task(handler_env) -> None:
    """Test that /stop when no AI is running gives appropriate message."""
    handler, _, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/stop"), BOT_ID)
    assert "没有进行中" in replies.last_text


async def test_stop_cancels_active_task(timeout_env) -> None:
    """Test that /stop cancels an active AI processing task."""
    handler, mock_agent, replies = timeout_env
    mock_agent.delay = 5.0  # Long delay to ensure task is still running when /stop arrives

    # Start AI task in the background
    ai_task = asyncio.create_task(handler.handle_message(_private_event(111, "A", "hello"), BOT_ID))
    # Give the handler time to register the active task
    await asyncio.sleep(0.05)

    # Send /stop command
    await handler.handle_message(_private_event(111, "A", "/stop"), BOT_ID)

    # Wait for the AI task to finish (it should be cancelled)
    with contextlib.suppress(asyncio.CancelledError):
        await ai_task

    assert ai_task.cancelled()
    assert any("已中断" in text for text in replies.texts)


# --- Busy rejection tests ---


async def test_busy_rejection(timeout_env) -> None:
    """Test that a second message while AI is thinking is rejected with a hint."""
    handler, mock_agent, replies = timeout_env
    mock_agent.delay = 5.0

    # Start first message (will be long-running)
    task = asyncio.create_task(handler.handle_message(_private_event(111, "A", "first"), BOT_ID))
    await asyncio.sleep(0.05)

    # Send second message — should be rejected
    await handler.handle_message(_private_event(111, "A", "second"), BOT_ID)
    assert "正在思考" in replies.last_text
    assert "/stop" in replies.last_text

    # Clean up the first task
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_busy_rejection_different_chats_ok(timeout_env) -> None:
    """Test that different chats can process concurrently (no busy rejection)."""
    handler, mock_agent, replies = timeout_env
    mock_agent.delay = 0.3

    # Start tasks for two different users
    task1 = asyncio.create_task(handler.handle_message(_private_event(111, "A", "msg1"), BOT_ID))
    task2 = asyncio.create_task(handler.handle_message(_private_event(222, "B", "msg2"), BOT_ID))

    await asyncio.gather(task1, task2)

    # Both should get AI replies (no busy rejection)
    assert len(mock_agent.calls) == 2
    ai_replies = [t for t in replies.texts if t == "Mock AI response"]
    assert len(ai_replies) == 2


# --- Timeout notification tests ---


async def test_thinking_notification_fires(timeout_env) -> None:
    """Test that the 'thinking' notification fires when AI takes longer than threshold."""
    handler, mock_agent, replies = timeout_env
    # thinking_notify_seconds = 0.3, delay = 0.5 → first notification should fire
    mock_agent.delay = 0.5

    await handler.handle_message(_private_event(111, "A", "think"), BOT_ID)

    texts = replies.texts
    # Should have the thinking notification and the AI reply
    assert any("正在思考" in t for t in texts)
    assert "Mock AI response" in texts
    # Should NOT have the long-thinking notification (0.5 < 0.8)
    assert not any("/stop" in t for t in texts)


async def test_thinking_long_notification_fires(timeout_env) -> None:
    """Test that both thinking notifications fire when AI takes very long."""
    handler, mock_agent, replies = timeout_env
    # thinking_notify_seconds = 0.3, thinking_long_notify_seconds = 0.8, delay = 1.0
    mock_agent.delay = 1.0

    await handler.handle_message(_private_event(111, "A", "long think"), BOT_ID)

    texts = replies.texts
    # Both notifications and the AI reply
    assert any("正在思考" in t for t in texts)
    assert any("/stop" in t for t in texts)
    assert "Mock AI response" in texts


async def test_no_notification_on_fast_response(handler_env) -> None:
    """Test that no notification is sent when AI responds quickly."""
    handler, mock_agent, replies = handler_env
    # Default thresholds (10s, 30s), no delay → no notifications

    await handler.handle_message(_private_event(111, "A", "fast"), BOT_ID)

    # Only the AI reply, no notifications
    assert len(replies.replies) == 1
    assert replies.last_text == "Mock AI response"


# --- Pending permission interception tests ---


async def test_pending_permission_invalid_input_shows_hint(handler_env) -> None:
    """If permission is pending, non-numeric input should be intercepted and hinted."""
    handler, mock_agent, replies = handler_env
    broker = mock_agent.permission_broker
    assert broker is not None

    chat_id = "private:111"
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    options = [PermissionOption(kind="allow_once", name="Allow once", optionId="o1")]
    broker._pending[chat_id] = PendingPermission(future=future, options=options, event={})

    await handler.handle_message(_private_event(111, "A", "not a number"), BOT_ID)

    assert "待处理的权限请求" in replies.last_text
    assert future.done() is False
    assert len(mock_agent.calls) == 0

    broker.cancel_pending(chat_id)


async def test_pending_permission_valid_reply_resolves_without_reply(handler_env) -> None:
    """If permission is pending, a numeric reply should resolve without extra replies."""
    handler, mock_agent, replies = handler_env
    broker = mock_agent.permission_broker
    assert broker is not None

    chat_id = "private:111"
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    options = [PermissionOption(kind="allow_once", name="Allow once", optionId="o1")]
    broker._pending[chat_id] = PendingPermission(future=future, options=options, event={})

    before = len(replies.replies)
    await handler.handle_message(_private_event(111, "A", "1"), BOT_ID)
    after = len(replies.replies)

    assert after == before
    assert future.done() is True
    assert future.result().option_id == "o1"
    assert len(mock_agent.calls) == 0

    broker.cancel_pending(chat_id)


# --- Internal error handling tests ---


async def test_internal_exception_sends_error_reply(monkeypatch) -> None:
    """Unexpected exceptions in handle_message() should send a user-facing error."""
    import ncat.dispatcher as dispatcher_module

    def _boom(_: dict, __: int):
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatcher_module, "onebot_to_internal", _boom)

    mock_agent = MockAgentManager()
    replies = ReplyCollector()
    broker = PermissionBroker(reply_fn=replies, timeout=300)
    mock_agent.permission_broker = broker
    handler = MessageDispatcher(
        agent_manager=mock_agent,
        reply_fn=replies,
        permission_broker=broker,
    )

    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)
    assert replies.last_text == "处理消息时发生内部错误"
