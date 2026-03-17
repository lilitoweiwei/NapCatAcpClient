"""Tests for the message dispatcher (business logic, no WebSocket needed)."""

import asyncio
import contextlib
from pathlib import Path

import pytest
import pytest_asyncio

from ncat.agent_manager import MSG_AGENT_NOT_CONNECTED
from ncat.dispatcher import MessageDispatcher
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
    handler = MessageDispatcher(
        agent_manager=mock_agent,
        reply_fn=replies,
    )

    yield handler, mock_agent, replies


@pytest_asyncio.fixture
async def timeout_env():
    """Create a handler with short timeout thresholds for testing notifications."""
    mock_agent = MockAgentManager()
    replies = ReplyCollector()
    handler = MessageDispatcher(
        agent_manager=mock_agent,
        reply_fn=replies,
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


def _private_segments_event(user_id: int, name: str, segments: list[dict]) -> dict:
    return {
        "self_id": BOT_ID,
        "user_id": user_id,
        "message_type": "private",
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
    assert mock_agent.ensure_connection_calls == ["private:111"]


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


async def test_file_only_message_is_buffered(handler_env, monkeypatch, tmp_path: Path) -> None:
    handler, mock_agent, replies = handler_env
    mock_agent.workspace_cwds["private:111"] = str(tmp_path / "default")

    async def _fake_download_private_file(**kwargs):
        inbox = Path(kwargs["workspace_cwd"]) / ".qqfiles"
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / "foo.pdf"
        target.write_text("pdf")
        from ncat.models import SavedFileAttachment

        return SavedFileAttachment(
            name="foo.pdf",
            saved_path=str(target),
            original_file_id="f-1",
            size=3,
        )

    monkeypatch.setattr("ncat.dispatcher.best_effort_download_private_file", _fake_download_private_file)

    await handler.handle_message(
        _private_segments_event(
            111,
            "Alice",
            [
                {
                    "type": "file",
                    "data": {"file": "foo.pdf", "file_id": "f-1", "url": "http://x/foo.pdf"},
                }
            ],
        ),
        BOT_ID,
    )

    assert len(mock_agent.calls) == 0
    assert "已收到文件" in replies.last_text
    pending = handler._pending_inputs.peek("private:111")
    assert pending is not None
    assert len(pending.files) == 1


async def test_image_only_message_is_buffered(handler_env) -> None:
    handler, mock_agent, replies = handler_env

    await handler.handle_message(
        _private_segments_event(
            111,
            "Alice",
            [{"type": "image", "data": {"url": "http://example.com/a.png"}}],
        ),
        BOT_ID,
    )

    assert len(mock_agent.calls) == 0
    assert replies.last_text == "已收到文件/图片，请继续发送说明。"
    pending = handler._pending_inputs.peek("private:111")
    assert pending is not None
    assert len(pending.images) == 1


async def test_next_text_consumes_large_buffered_image_as_file(
    handler_env, monkeypatch, tmp_path: Path
) -> None:
    handler, mock_agent, replies = handler_env
    mock_agent._supports_image = True
    mock_agent.workspace_cwds["private:111"] = str(tmp_path / "default")

    class _DownloadedImage:
        def __init__(self) -> None:
            self.url = "http://example.com/huge.png"
            self.data = b"x" * (6 * 1024 * 1024)
            self.mime_type = "image/png"

    async def _fake_download_image(url: str, timeout_seconds: float):
        assert url == "http://example.com/huge.png"
        assert timeout_seconds > 0
        return _DownloadedImage()

    def _fake_prepare_image_for_inline(downloaded_image, *, max_inline_bytes: int):
        assert max_inline_bytes == 2 * 1024 * 1024
        return _DownloadedImage()

    monkeypatch.setattr("ncat.prompt_runner.download_image", _fake_download_image)
    monkeypatch.setattr("ncat.prompt_runner.prepare_image_for_inline", _fake_prepare_image_for_inline)

    await handler.handle_message(
        _private_segments_event(
            111,
            "Alice",
            [{"type": "image", "data": {"url": "http://example.com/huge.png"}}],
        ),
        BOT_ID,
    )
    await handler.handle_message(_private_event(111, "Alice", "please check"), BOT_ID)

    assert len(mock_agent.calls) == 1
    _, prompt = mock_agent.calls[0]
    assert "[图片]" in prompt


async def test_next_text_consumes_buffered_files_and_images(handler_env, monkeypatch, tmp_path: Path) -> None:
    handler, mock_agent, replies = handler_env
    mock_agent._supports_image = True
    mock_agent.workspace_cwds["private:111"] = str(tmp_path / "default")

    async def _fake_download_private_file(**kwargs):
        inbox = Path(kwargs["workspace_cwd"]) / ".qqfiles"
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / "foo.pdf"
        target.write_text("pdf")
        from ncat.models import SavedFileAttachment

        return SavedFileAttachment(
            name="foo.pdf",
            saved_path=str(target),
            original_file_id="f-1",
            size=3,
        )

    async def _fake_get_file_data(file_id: str):
        return {"file": str(tmp_path / "unused")}

    async def _fake_download_image(url: str, timeout_seconds: float):
        assert url == "http://example.com/a.png"
        assert timeout_seconds > 0
        return ("aGVsbG8=", "image/png")

    monkeypatch.setattr("ncat.dispatcher.best_effort_download_private_file", _fake_download_private_file)
    monkeypatch.setattr(handler, "_get_file_data", _fake_get_file_data)
    monkeypatch.setattr("ncat.prompt_runner.download_image", _fake_download_image)

    await handler.handle_message(
        _private_segments_event(
            111,
            "Alice",
            [{"type": "file", "data": {"file": "foo.pdf", "file_id": "f-1"}}],
        ),
        BOT_ID,
    )
    await handler.handle_message(
        _private_segments_event(
            111,
            "Alice",
            [{"type": "image", "data": {"url": "http://example.com/a.png"}}],
        ),
        BOT_ID,
    )
    await handler.handle_message(_private_event(111, "Alice", "please check"), BOT_ID)

    assert len(mock_agent.calls) == 1
    _, prompt = mock_agent.calls[0]
    assert "please check" in prompt
    assert prompt.count("[图片]") >= 1
    assert "[SYSTEM: The user attached a file. It has been saved at" in prompt
    assert handler._pending_inputs.peek("private:111") is None


async def test_new_clears_pending_attachments(handler_env, monkeypatch, tmp_path: Path) -> None:
    handler, mock_agent, replies = handler_env
    mock_agent.workspace_cwds["private:111"] = str(tmp_path / "default")

    async def _fake_download_private_file(**kwargs):
        inbox = Path(kwargs["workspace_cwd"]) / ".qqfiles"
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / "foo.pdf"
        target.write_text("pdf")
        from ncat.models import SavedFileAttachment

        return SavedFileAttachment(
            name="foo.pdf",
            saved_path=str(target),
            original_file_id="f-1",
            size=3,
        )

    monkeypatch.setattr("ncat.dispatcher.best_effort_download_private_file", _fake_download_private_file)

    await handler.handle_message(
        _private_segments_event(
            111,
            "Alice",
            [{"type": "file", "data": {"file": "foo.pdf", "file_id": "f-1"}}],
        ),
        BOT_ID,
    )
    assert handler._pending_inputs.peek("private:111") is not None

    await handler.handle_message(_private_event(111, "Alice", "/new"), BOT_ID)

    assert handler._pending_inputs.peek("private:111") is None
    assert "新会话" in replies.last_text


# --- Command tests ---


async def test_agent_not_connected_message_reply(handler_env) -> None:
    """Test that when agent is not connected, a normal message gets MSG_AGENT_NOT_CONNECTED."""
    handler, mock_agent, replies = handler_env
    mock_agent.ensure_connection_error = RuntimeError(MSG_AGENT_NOT_CONNECTED)

    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)
    assert replies.last_text == MSG_AGENT_NOT_CONNECTED
    assert len(mock_agent.calls) == 0


async def test_agent_not_connected_new_reply(handler_env) -> None:
    """Test that when agent is not connected, /new still succeeds."""
    handler, mock_agent, replies = handler_env
    mock_agent._is_running = False

    await handler.handle_message(_private_event(111, "A", "/new"), BOT_ID)
    assert "新会话" in replies.last_text
    assert "private:111" in mock_agent.closed_sessions
    assert "private:111" not in mock_agent.next_session_modes


async def test_command_new_closes_session(handler_env) -> None:
    """Test /new command closes current session via agent manager."""
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/new"), BOT_ID)
    assert "新会话" in replies.last_text
    assert "private:111" in mock_agent.closed_sessions
    assert mock_agent.disconnect_calls == ["private:111"]


async def test_command_new_invalid_agent(handler_env) -> None:
    """Test /new reports agent validation errors from the agent manager."""
    handler, mock_agent, replies = handler_env

    def _fail(chat_id: str, mode_id_or_none: str | None) -> None:
        raise ValueError("未找到 agent：ghost。可用 agents: build, reviewer")

    mock_agent.set_next_session_mode = _fail

    await handler.handle_message(_private_event(111, "A", "/new ghost"), BOT_ID)

    assert replies.last_text == "未找到 agent：ghost。可用 agents: build, reviewer\n\n当前 Agent: 未知（首次创建会话后可见）\n可用 Agents: 暂无（首次创建会话后可见）"
    assert "private:111" not in mock_agent.closed_sessions


async def test_command_help(handler_env) -> None:
    """Test /help command via handler."""
    handler, _, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/help"), BOT_ID)
    assert "/agent" in replies.last_text
    assert "/status" in replies.last_text
    assert "/new" in replies.last_text
    assert "/help" in replies.last_text
    assert "/stop" in replies.last_text


async def test_command_unknown(handler_env) -> None:
    """Test unknown command returns help text."""
    handler, _, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/xyz"), BOT_ID)
    assert "/new" in replies.last_text


async def test_status_without_session_reports_unknowns(handler_env) -> None:
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/status"), BOT_ID)

    assert len(mock_agent.calls) == 0
    assert "工作区: default" in replies.last_text
    assert "连接: 已连接" in replies.last_text
    assert "会话: 未创建" in replies.last_text
    assert "未知（首次创建会话后可见）" in replies.last_text


async def test_status_after_session_shows_agent_and_usage(handler_env) -> None:
    from ncat.models import UsageSnapshot

    handler, mock_agent, replies = handler_env
    mock_agent.usage_by_chat["private:111"] = UsageSnapshot(
        used=200,
        size=1000,
        cost_amount=0.1234,
        cost_currency="USD",
    )

    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)
    await handler.handle_message(_private_event(111, "A", "/status"), BOT_ID)

    assert "会话: 已创建" in replies.last_text
    assert "Agent: build" in replies.last_text
    assert "上下文: 200 / 1000 (20.0%)" in replies.last_text
    assert "累计成本: 0.1234 USD" in replies.last_text


async def test_agent_without_name_lists_current_and_available_modes(handler_env) -> None:
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)
    await handler.handle_message(_private_event(111, "A", "/agent"), BOT_ID)

    assert "当前 Agent: build" in replies.last_text
    assert "- reviewer - Review changes" in replies.last_text
    assert "/new 后会恢复为默认 agent，也可以用 /new <agent> 为新 session 指定 agent" in replies.last_text


async def test_agent_switches_current_session_mode(handler_env) -> None:
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/agent reviewer"), BOT_ID)

    assert mock_agent.set_mode_calls == [("private:111", "reviewer")]
    assert mock_agent.new_session_calls == [("private:111", None)]
    assert replies.last_text.startswith("已切换到 agent：reviewer")


async def test_agent_unknown_name_shows_available_modes(handler_env) -> None:
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/agent no-such-agent"), BOT_ID)

    assert "未找到 agent：no-such-agent" in replies.last_text
    assert "- reviewer - Review changes" in replies.last_text


async def test_agent_rejected_while_busy(timeout_env) -> None:
    handler, mock_agent, replies = timeout_env
    mock_agent.delay = 5.0

    task = asyncio.create_task(handler.handle_message(_private_event(111, "A", "first"), BOT_ID))
    await asyncio.sleep(0.05)
    await handler.handle_message(_private_event(111, "A", "/agent reviewer"), BOT_ID)

    with contextlib.suppress(asyncio.CancelledError):
        task.cancel()
        await task

    assert "请等待完成或先发送 /stop" in replies.last_text
    assert mock_agent.set_mode_calls == []


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


async def test_agent_error_with_partial_sends_partial_then_error(handler_env) -> None:
    """When agent errors with partial content, user gets partial reply then error message."""
    from ncat.models import ContentPart

    handler, mock_agent, replies = handler_env
    mock_agent.raise_error_with_parts = (
        Exception("Internal error"),
        [ContentPart(type="text", text="Partial reply.")],
    )

    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)

    assert len(replies.texts) >= 2
    assert replies.texts[0] == "Partial reply."
    assert "Agent 发生错误" in replies.texts[1]
    assert "以上为已生成的部分内容" in replies.texts[1]
    assert "Internal error" in replies.texts[1]
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


async def test_same_chat_reuses_session_without_new(handler_env) -> None:
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "Alice", "hello"), BOT_ID)
    await handler.handle_message(_private_event(111, "Alice", "again"), BOT_ID)

    assert len(mock_agent.new_session_calls) == 1
    assert mock_agent.prompt_session_ids == [
        ("private:111", mock_agent.prompt_session_ids[0][1]),
        ("private:111", mock_agent.prompt_session_ids[0][1]),
    ]
    assert replies.texts.count("Mock AI response") == 2


async def test_stop_does_not_force_new_session_on_next_prompt(timeout_env) -> None:
    handler, mock_agent, replies = timeout_env
    mock_agent.delay = 5.0

    task = asyncio.create_task(handler.handle_message(_private_event(111, "A", "first"), BOT_ID))
    await asyncio.sleep(0.05)
    first_session_id = mock_agent.prompt_session_ids[0][1]

    await handler.handle_message(_private_event(111, "A", "/stop"), BOT_ID)
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert mock_agent.prompt_session_ids
    mock_agent.delay = 0
    await handler.handle_message(_private_event(111, "A", "second"), BOT_ID)

    assert mock_agent.prompt_session_ids[-1] == ("private:111", first_session_id)
    assert "private:111" not in mock_agent.closed_sessions


async def test_new_forces_new_session_after_next_message(handler_env) -> None:
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)
    first_session_id = mock_agent.prompt_session_ids[-1][1]
    await handler.handle_message(_private_event(111, "A", "/agent reviewer"), BOT_ID)
    assert mock_agent.current_mode_ids["private:111"] == "reviewer"

    await handler.handle_message(_private_event(111, "A", "/new build"), BOT_ID)
    await handler.handle_message(_private_event(111, "A", "hello again"), BOT_ID)
    second_session_id = mock_agent.prompt_session_ids[-1][1]

    assert first_session_id != second_session_id
    assert mock_agent.current_mode_ids["private:111"] == "build"
    assert mock_agent.new_session_calls == [
        ("private:111", None),
        ("private:111", "build"),
    ]


async def test_new_with_agent_starts_next_session_in_requested_mode(handler_env) -> None:
    handler, mock_agent, replies = handler_env

    await handler.handle_message(_private_event(111, "A", "/new reviewer"), BOT_ID)
    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)

    assert mock_agent.new_session_calls == [("private:111", "reviewer")]
    assert mock_agent.current_mode_ids["private:111"] == "reviewer"
    assert "下次将使用 agent：reviewer" in replies.texts[0]


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


# --- Internal error handling tests ---


async def test_internal_exception_sends_error_reply(monkeypatch) -> None:
    """Unexpected exceptions in handle_message() should send a user-facing error."""
    import ncat.dispatcher as dispatcher_module

    def _boom(_: dict, __: int):
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatcher_module, "onebot_to_internal", _boom)

    mock_agent = MockAgentManager()
    replies = ReplyCollector()
    handler = MessageDispatcher(
        agent_manager=mock_agent,
        reply_fn=replies,
    )

    await handler.handle_message(_private_event(111, "A", "hello"), BOT_ID)
    assert replies.last_text == "处理消息时发生内部错误"
