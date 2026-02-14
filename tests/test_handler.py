"""Tests for the message handler (business logic, no WebSocket needed)."""

import asyncio
import contextlib

import pytest
import pytest_asyncio

from ncat.handler import MessageHandler
from ncat.opencode import OpenCodeResponse, SubprocessOpenCodeBackend
from ncat.prompt import PromptBuilder
from ncat.session import SessionManager
from tests.mock_napcat import MockNapCat

pytestmark = pytest.mark.asyncio

BOT_ID = MockNapCat.BOT_ID


class FakeBackend(SubprocessOpenCodeBackend):
    """Fake backend for handler tests with configurable delay."""

    def __init__(self) -> None:
        super().__init__(command="echo", work_dir=".", max_concurrent=1)
        self.calls: list[tuple[str | None, str]] = []
        self.response = OpenCodeResponse(
            session_id="ses_handler_test",
            content="Handler test reply",
            success=True,
            error=None,
        )
        # Configurable delay before returning response (for timeout/cancel tests)
        self.delay: float = 0

    async def _run(self, session_id: str | None, message: str) -> OpenCodeResponse:
        self.calls.append((session_id, message))
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.response


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
async def handler_env(tmp_path):
    """Create a MessageHandler with fake backend, prompt builder, and reply collector."""
    sm = SessionManager(str(tmp_path / "handler_test.db"))
    await sm.init()

    backend = FakeBackend()
    prompt_builder = PromptBuilder(tmp_path / "prompts")
    replies = ReplyCollector()
    handler = MessageHandler(
        session_manager=sm,
        opencode_backend=backend,
        prompt_builder=prompt_builder,
        reply_fn=replies,
    )

    yield handler, backend, replies, sm

    await sm.close()


@pytest_asyncio.fixture
async def timeout_env(tmp_path):
    """Create a handler with short timeout thresholds for testing notifications."""
    sm = SessionManager(str(tmp_path / "timeout_test.db"))
    await sm.init()

    backend = FakeBackend()
    prompt_builder = PromptBuilder(tmp_path / "prompts")
    replies = ReplyCollector()
    handler = MessageHandler(
        session_manager=sm,
        opencode_backend=backend,
        prompt_builder=prompt_builder,
        reply_fn=replies,
        thinking_notify_seconds=0.3,
        thinking_long_notify_seconds=0.8,
    )

    yield handler, backend, replies, sm

    await sm.close()


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


# --- Existing tests (updated) ---


async def test_private_message_calls_opencode(handler_env) -> None:
    """Test that a private message goes through the full AI pipeline."""
    handler, backend, replies, sm = handler_env

    await handler.handle_message(_private_event(111, "Alice", "hello"), BOT_ID)

    assert len(backend.calls) == 1
    assert "hello" in backend.calls[0][1]
    assert replies.last_text == "Handler test reply"


async def test_group_without_at_ignored(handler_env) -> None:
    """Test that group messages without @bot produce no reply."""
    handler, backend, replies, _ = handler_env

    await handler.handle_message(_group_event(222, "G", 111, "Bob", "hi", at_bot=False), BOT_ID)

    assert len(backend.calls) == 0
    assert len(replies.replies) == 0


async def test_group_with_at_processed(handler_env) -> None:
    """Test that group messages with @bot are processed."""
    handler, backend, replies, _ = handler_env

    await handler.handle_message(_group_event(222, "G", 111, "Bob", " hi", at_bot=True), BOT_ID)

    assert len(backend.calls) == 1
    assert replies.last_text == "Handler test reply"


async def test_command_new_creates_session(handler_env) -> None:
    """Test /new command via handler."""
    handler, _, replies, sm = handler_env

    # First, trigger a session creation with a normal message
    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)
    s1 = await sm.get_active_session("private:111")
    assert s1 is not None

    # Then send /new
    await handler.handle_message(_private_event(111, "A", "/new"), BOT_ID)
    assert "新会话" in replies.last_text

    # Session should be different
    s2 = await sm.get_active_session("private:111")
    assert s2 is not None
    assert s2.id != s1.id


async def test_command_help(handler_env) -> None:
    """Test /help command via handler."""
    handler, _, replies, _ = handler_env

    await handler.handle_message(_private_event(111, "A", "/help"), BOT_ID)
    assert "/new" in replies.last_text
    assert "/help" in replies.last_text
    assert "/stop" in replies.last_text


async def test_command_unknown(handler_env) -> None:
    """Test unknown command returns help text."""
    handler, _, replies, _ = handler_env

    await handler.handle_message(_private_event(111, "A", "/xyz"), BOT_ID)
    assert "/new" in replies.last_text


async def test_opencode_error_sends_error(handler_env) -> None:
    """Test that OpenCode failure produces user-facing error."""
    handler, backend, replies, _ = handler_env

    backend.response = OpenCodeResponse(
        session_id="ses_err", content="", success=False, error="boom"
    )

    await handler.handle_message(_private_event(111, "A", "crash"), BOT_ID)
    assert "出错" in replies.last_text


async def test_opencode_empty_response(handler_env) -> None:
    """Test that empty AI content produces a warning reply."""
    handler, backend, replies, _ = handler_env

    backend.response = OpenCodeResponse(
        session_id="ses_empty", content="", success=True, error=None
    )

    await handler.handle_message(_private_event(111, "A", "test"), BOT_ID)
    assert "未返回有效回复" in replies.last_text


async def test_session_continuation(handler_env) -> None:
    """Test that second message reuses the OpenCode session ID."""
    handler, backend, replies, _ = handler_env

    await handler.handle_message(_private_event(111, "A", "first"), BOT_ID)
    assert backend.calls[0][0] is None  # first call, no session

    await handler.handle_message(_private_event(111, "A", "second"), BOT_ID)
    assert backend.calls[1][0] == "ses_handler_test"  # reuses session


async def test_prompt_includes_context(handler_env) -> None:
    """Test that the prompt sent to OpenCode includes sender context."""
    handler, backend, replies, _ = handler_env

    await handler.handle_message(_private_event(111, "Alice", "写个函数"), BOT_ID)
    _, prompt = backend.calls[0]
    assert "[私聊，用户 Alice(111)]" in prompt
    assert "写个函数" in prompt


async def test_exception_in_handler_sends_error(handler_env) -> None:
    """Test that unexpected exceptions produce a user-facing error message."""
    handler, backend, replies, _ = handler_env

    # Make backend raise an exception
    async def exploding_run(session_id, message):
        raise RuntimeError("unexpected crash")

    backend._run = exploding_run  # type: ignore

    await handler.handle_message(_private_event(111, "A", "boom"), BOT_ID)
    assert "内部错误" in replies.last_text


# --- New tests: /stop command ---


async def test_stop_no_active_task(handler_env) -> None:
    """Test that /stop when no AI is running gives appropriate message."""
    handler, _, replies, _ = handler_env

    await handler.handle_message(_private_event(111, "A", "/stop"), BOT_ID)
    assert "没有进行中" in replies.last_text


async def test_stop_cancels_active_task(timeout_env) -> None:
    """Test that /stop cancels an active AI processing task."""
    handler, backend, replies, _ = timeout_env
    backend.delay = 5.0  # Long delay to ensure task is still running when /stop arrives

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


async def test_stop_for_group_chat(timeout_env) -> None:
    """Test that /stop works for group chats (per-chat cancellation)."""
    handler, backend, replies, _ = timeout_env
    backend.delay = 5.0

    # Start AI task for a group
    ai_task = asyncio.create_task(
        handler.handle_message(_group_event(222, "G", 111, "Bob", " hello", at_bot=True), BOT_ID)
    )
    await asyncio.sleep(0.05)

    # Send /stop from the same group
    await handler.handle_message(
        _group_event(222, "G", 333, "Carol", " /stop", at_bot=True), BOT_ID
    )

    with contextlib.suppress(asyncio.CancelledError):
        await ai_task

    assert ai_task.cancelled()
    assert any("已中断" in text for text in replies.texts)


# --- New tests: busy rejection ---


async def test_busy_rejection(timeout_env) -> None:
    """Test that a second message while AI is thinking is rejected with a hint."""
    handler, backend, replies, _ = timeout_env
    backend.delay = 5.0

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
    handler, backend, replies, _ = timeout_env
    backend.delay = 0.3

    # Start tasks for two different users
    task1 = asyncio.create_task(handler.handle_message(_private_event(111, "A", "msg1"), BOT_ID))
    task2 = asyncio.create_task(handler.handle_message(_private_event(222, "B", "msg2"), BOT_ID))

    await asyncio.gather(task1, task2)

    # Both should get AI replies (no busy rejection)
    assert len(backend.calls) == 2
    ai_replies = [t for t in replies.texts if t == "Handler test reply"]
    assert len(ai_replies) == 2


# --- New tests: timeout notifications ---


async def test_thinking_notification_fires(timeout_env) -> None:
    """Test that the 'thinking' notification fires when AI takes longer than threshold."""
    handler, backend, replies, _ = timeout_env
    # thinking_notify_seconds = 0.3, delay = 0.5 → first notification should fire
    backend.delay = 0.5

    await handler.handle_message(_private_event(111, "A", "think"), BOT_ID)

    texts = replies.texts
    # Should have the thinking notification and the AI reply
    assert any("正在思考" in t for t in texts)
    assert "Handler test reply" in texts
    # Should NOT have the long-thinking notification (0.5 < 0.8)
    assert not any("/stop" in t for t in texts)


async def test_thinking_long_notification_fires(timeout_env) -> None:
    """Test that both thinking notifications fire when AI takes very long."""
    handler, backend, replies, _ = timeout_env
    # thinking_notify_seconds = 0.3, thinking_long_notify_seconds = 0.8, delay = 1.0
    backend.delay = 1.0

    await handler.handle_message(_private_event(111, "A", "long think"), BOT_ID)

    texts = replies.texts
    # Both notifications and the AI reply
    assert any("正在思考" in t for t in texts)
    assert any("/stop" in t for t in texts)
    assert "Handler test reply" in texts


async def test_no_notification_on_fast_response(handler_env) -> None:
    """Test that no notification is sent when AI responds quickly."""
    handler, backend, replies, _ = handler_env
    # Default thresholds (10s, 30s), no delay → no notifications

    await handler.handle_message(_private_event(111, "A", "fast"), BOT_ID)

    # Only the AI reply, no notifications
    assert len(replies.replies) == 1
    assert replies.last_text == "Handler test reply"


async def test_notifications_cancelled_after_stop(timeout_env) -> None:
    """Test that pending notification timers are cancelled when task is stopped."""
    handler, backend, replies, _ = timeout_env
    backend.delay = 5.0  # Long enough that notifications would fire

    ai_task = asyncio.create_task(handler.handle_message(_private_event(111, "A", "hello"), BOT_ID))
    # Cancel quickly, before any notification threshold
    await asyncio.sleep(0.05)
    await handler.handle_message(_private_event(111, "A", "/stop"), BOT_ID)

    with contextlib.suppress(asyncio.CancelledError):
        await ai_task

    # Wait a bit to ensure no late notifications fire
    await asyncio.sleep(0.5)

    # Should only have the "已中断" message, no thinking notifications
    texts = replies.texts
    assert any("已中断" in t for t in texts)
    assert not any("正在思考" in t for t in texts)
