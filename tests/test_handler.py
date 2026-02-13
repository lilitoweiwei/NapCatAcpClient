"""Tests for the message handler (business logic, no WebSocket needed)."""

import pytest
import pytest_asyncio

from nochan.handler import MessageHandler
from nochan.opencode import OpenCodeResponse, SubprocessOpenCodeBackend
from nochan.session import SessionManager
from tests.mock_napcat import MockNapCat


pytestmark = pytest.mark.asyncio

BOT_ID = MockNapCat.BOT_ID


class FakeBackend(SubprocessOpenCodeBackend):
    """Fake backend for handler tests."""

    def __init__(self) -> None:
        super().__init__(command="echo", work_dir=".", max_concurrent=1)
        self.calls: list[tuple[str | None, str]] = []
        self.response = OpenCodeResponse(
            session_id="ses_handler_test",
            content="Handler test reply",
            success=True,
            error=None,
        )

    async def _run(self, session_id: str | None, message: str) -> OpenCodeResponse:
        self.calls.append((session_id, message))
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


@pytest_asyncio.fixture
async def handler_env(tmp_path):
    """Create a MessageHandler with fake backend and reply collector."""
    sm = SessionManager(str(tmp_path / "handler_test.db"))
    await sm.init()

    backend = FakeBackend()
    replies = ReplyCollector()
    handler = MessageHandler(
        session_manager=sm,
        opencode_backend=backend,
        reply_fn=replies,
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
    group_id: int, group_name: str,
    user_id: int, name: str, text: str,
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

    await handler.handle_message(
        _group_event(222, "G", 111, "Bob", "hi", at_bot=False), BOT_ID
    )

    assert len(backend.calls) == 0
    assert len(replies.replies) == 0


async def test_group_with_at_processed(handler_env) -> None:
    """Test that group messages with @bot are processed."""
    handler, backend, replies, _ = handler_env

    await handler.handle_message(
        _group_event(222, "G", 111, "Bob", " hi", at_bot=True), BOT_ID
    )

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
