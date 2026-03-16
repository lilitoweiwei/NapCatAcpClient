"""Unit tests for the prompt lifecycle manager (PromptRunner)."""

import asyncio
import contextlib
import io

import pytest
from PIL import Image

import ncat.prompt_runner as prompt_runner_module
from ncat.agent_manager import AcpMessageTooLargeError
from ncat.models import ContentPart, DownloadedImage, ImageAttachment, ParsedMessage, VisibleTurnEvent
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
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
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
    assert agent.ensure_connection_calls == ["private:111"]


async def test_process_empty_response_sends_warning() -> None:
    """Empty agent response should produce a warning reply."""
    agent = MockAgentManager()
    agent.response_text = ""
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
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
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "x"), event={})

    text = await replies.get_text()
    assert "Agent 异常" in text
    assert "Agent crashed" in text
    assert "private:111" in agent.closed_sessions


async def test_cancelled_request_does_not_close_session() -> None:
    agent = MockAgentManager()
    agent.delay = 5.0
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
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

    assert "private:111" not in agent.closed_sessions


async def test_first_message_ensures_connection_before_image_capability_check() -> None:
    agent = MockAgentManager()
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "hello"), event={})

    assert agent.ensure_connection_calls == ["private:111"]


async def test_process_error_with_partial_sends_partial_then_error() -> None:
    """When agent raises with partial content, user gets partial reply then error message."""
    from ncat.models import ContentPart

    agent = MockAgentManager()
    agent.raise_error_with_parts = (
        Exception("Internal error"),
        [ContentPart(type="text", text="Half of the answer here.")],
    )
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "x"), event={})

    assert len(replies.texts) >= 2
    assert replies.texts[0] == "Half of the answer here."
    assert "Agent 发生错误" in replies.texts[1]
    assert "以上为已生成的部分内容" in replies.texts[1]
    assert "Internal error" in replies.texts[1]
    assert "private:111" in agent.closed_sessions


async def test_process_error_with_empty_partial_sends_only_error() -> None:
    """When agent raises with no partial content, user gets only the error message."""
    agent = MockAgentManager()
    agent.raise_error_with_parts = (Exception("Stream failed"), [])
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "x"), event={})

    assert len(replies.texts) == 1
    assert "Agent 异常" in replies.texts[0]
    assert "Stream failed" in replies.texts[0]
    assert "private:111" in agent.closed_sessions


async def test_process_oversized_acp_message_uses_friendly_error() -> None:
    agent = MockAgentManager()
    agent.raise_error_with_parts = (AcpMessageTooLargeError(128), [])
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "x"), event={})

    assert len(replies.texts) == 1
    assert "128MB" in replies.texts[0]
    assert "acp_stdio_read_limit_mb" in replies.texts[0]
    assert "Separator is not found" not in replies.texts[0]


async def test_is_busy_true_while_processing() -> None:
    """is_busy() should be True while an AI task is running for a chat."""
    agent = MockAgentManager()
    agent.delay = 0.2
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
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


async def test_cancel_cancels_active_task_and_notifies_agent() -> None:
    """cancel() should cancel the active task and notify the agent."""
    agent = MockAgentManager()
    agent.delay = 5.0
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
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

    # agent.cancel() runs in a background task; yield control once.
    await asyncio.sleep(0)
    assert "private:111" in agent.cancelled

    assert runner.is_busy("private:111") is False


async def test_thinking_notifications_fire_when_slow() -> None:
    """Thinking notification timers should fire before the agent response."""
    agent = MockAgentManager()
    agent.delay = 0.15
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
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
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        thinking_notify_seconds=0.05,
        thinking_long_notify_seconds=0.1,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "hello"), event={})

    texts = replies.texts
    assert any("正在思考" in t for t in texts)
    assert any("/stop" in t for t in texts)
    assert "Mock AI response" in texts


async def test_visible_event_flushes_buffered_text_before_final_reply() -> None:
    """Visible turn events should flush buffered text before the final reply."""
    agent = MockAgentManager()
    agent.stream_steps = [
        (
            0,
            [ContentPart(type="text", text="你好，我先看一下。")],
            VisibleTurnEvent(key="thinking", status_text="<AI 正在思考中>"),
        ),
        (
            0,
            [],
            VisibleTurnEvent(key="tool:glob:in_progress", status_text="<AI 正在调用：glob>"),
        ),
    ]
    agent.response_parts = [ContentPart(type="text", text="最终结论")]
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    await runner.process(_parsed_private("private:111", 111, "Alice", "hello"), event={})

    assert replies.texts == [
        "你好，我先看一下。\n<AI 正在思考中>",
        "<AI 正在调用：glob>",
        "最终结论",
    ]


async def test_image_is_prepared_inline_in_prompt() -> None:
    agent = MockAgentManager()
    agent._supports_image = True
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
        max_inline_image_mb=2,
    )

    image = Image.new("RGB", (12, 12), (255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    downloaded = DownloadedImage(
        url="http://example.com/red.png",
        data=buffer.getvalue(),
        mime_type="image/png",
    )

    async def _fake_download_image(url: str, timeout_seconds: float):
        assert url == "http://example.com/red.png"
        assert timeout_seconds > 0
        return downloaded

    def _fake_prepare_image_for_inline(downloaded_image: DownloadedImage, *, max_inline_bytes: int):
        assert downloaded_image is downloaded
        assert max_inline_bytes == 2 * 1024 * 1024
        return DownloadedImage(
            url=downloaded_image.url,
            data=b"jpeg-bytes",
            mime_type="image/jpeg",
        )

    original = prompt_runner_module.download_image
    original_prepare = prompt_runner_module.prepare_image_for_inline
    prompt_runner_module.download_image = _fake_download_image
    prompt_runner_module.prepare_image_for_inline = _fake_prepare_image_for_inline
    try:
        parsed = _parsed_private("private:111", 111, "Alice", "please check")
        parsed.images = [ImageAttachment(url="http://example.com/red.png")]
        await runner.process(parsed, event={"user_id": 111})
    finally:
        prompt_runner_module.download_image = original
        prompt_runner_module.prepare_image_for_inline = original_prepare

    assert len(agent.calls) == 1
    _, prompt = agent.calls[0]
    assert "[图片]" in prompt
    assert len(agent.calls_blocks) == 1
    _, blocks = agent.calls_blocks[0]
    assert len(blocks) == 2
    assert getattr(blocks[1], "mimeType") == "image/jpeg"


async def test_image_prepare_failure_replies_user() -> None:
    agent = MockAgentManager()
    agent._supports_image = True
    replies = ReplyQueue()
    runner = PromptRunner(
        agent_manager=agent,
        reply_fn=replies,
        thinking_notify_seconds=0,
        thinking_long_notify_seconds=0,
    )

    async def _fake_download_image(url: str, timeout_seconds: float):
        return DownloadedImage(url=url, data=b"png", mime_type="image/png")

    def _fake_prepare_image_for_inline(downloaded_image: DownloadedImage, *, max_inline_bytes: int):
        raise prompt_runner_module.ImagePreparationError("图片过大，压缩后仍超过 2 MiB，无法发送给 Agent。")

    original = prompt_runner_module.download_image
    original_prepare = prompt_runner_module.prepare_image_for_inline
    prompt_runner_module.download_image = _fake_download_image
    prompt_runner_module.prepare_image_for_inline = _fake_prepare_image_for_inline
    try:
        parsed = _parsed_private("private:111", 111, "Alice", "please check")
        parsed.images = [ImageAttachment(url="http://example.com/red.png")]
        await runner.process(parsed, event={"user_id": 111})
    finally:
        prompt_runner_module.download_image = original
        prompt_runner_module.prepare_image_for_inline = original_prepare

    assert agent.calls == []
    assert replies.texts == ["图片过大，压缩后仍超过 2 MiB，无法发送给 Agent。"]
