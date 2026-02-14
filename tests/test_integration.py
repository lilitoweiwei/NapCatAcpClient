"""Integration tests: full pipeline from mock NapCat through ACP agent mock and back."""

import asyncio

import pytest
import pytest_asyncio
import websockets
from acp.schema import ImageContentBlock, TextContentBlock

import ncat.prompt_runner as prompt_runner_module
from ncat.converter import ContentPart
from ncat.napcat_server import NcatNapCatServer
from tests.mock_agent import MockAgentManager
from tests.mock_napcat import MockNapCat

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def full_stack():
    """Set up the full ncat stack with mock NapCat and mock agent."""
    mock_agent = MockAgentManager()
    mock_agent.response_text = "Integration AI response"
    server = NcatNapCatServer(
        host="127.0.0.1",
        port=0,
        agent_manager=mock_agent,
    )

    ws_server = await websockets.serve(server._handler_ws, "127.0.0.1", 0)
    port = ws_server.sockets[0].getsockname()[1]

    mock = MockNapCat(f"ws://127.0.0.1:{port}")
    await mock.connect()
    await asyncio.sleep(0.2)

    yield server, mock, mock_agent

    await mock.close()
    ws_server.close()
    await ws_server.wait_closed()


async def test_full_private_conversation(full_stack) -> None:
    """Test a full private chat: message -> agent -> reply."""
    server, mock, mock_agent = full_stack

    # Send first message
    await mock.send_private_message(111, "Alice", "hello")
    api_call = await mock.recv_api_call(timeout=5.0)

    assert api_call is not None
    assert api_call["action"] == "send_private_msg"
    assert api_call["params"]["message"][0]["data"]["text"] == "Integration AI response"

    # Verify agent was called with context
    assert len(mock_agent.calls) == 1
    chat_id, prompt = mock_agent.calls[0]
    assert chat_id == "private:111"
    assert "[Private chat, user Alice(111)]" in prompt
    assert "hello" in prompt


async def test_private_image_forwarded_to_agent_when_supported(full_stack, monkeypatch) -> None:
    """If agent supports images, ncat should forward an ACP ImageContentBlock."""
    _, mock, mock_agent = full_stack
    mock_agent._supports_image = True  # test hook

    async def _fake_download_image(url: str, timeout_seconds: float) -> tuple[str, str] | None:
        assert url == "http://example.com/a.png"
        assert timeout_seconds > 0
        return ("aGVsbG8=", "image/png")

    monkeypatch.setattr(prompt_runner_module, "download_image", _fake_download_image)

    # Send a private message event with an image segment (url field).
    await mock._send_event(
        {
            "self_id": mock.BOT_ID,
            "user_id": 111,
            "time": 0,
            "message_id": mock._next_message_id(),
            "message_type": "private",
            "sub_type": "friend",
            "sender": {"user_id": 111, "nickname": "Alice", "card": ""},
            "message": [
                {"type": "text", "data": {"text": "see"}},
                {"type": "image", "data": {"url": "http://example.com/a.png"}},
            ],
            "message_format": "array",
            "raw_message": "see[CQ:image]",
            "font": 14,
            "post_type": "message",
        }
    )

    # Consume the QQ reply API call.
    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    assert api_call["action"] == "send_private_msg"

    # Verify prompt blocks sent to agent include an image block.
    assert mock_agent.calls_blocks
    _, blocks = mock_agent.calls_blocks[0]
    assert isinstance(blocks[0], TextContentBlock)
    assert "[图片]" in blocks[0].text
    assert any(isinstance(b, ImageContentBlock) for b in blocks)


async def test_private_image_download_failed_falls_back_to_url(full_stack, monkeypatch) -> None:
    """If image download fails, ncat should send '[图片 url=...]' to the agent."""
    _, mock, mock_agent = full_stack
    mock_agent._supports_image = True  # test hook

    async def _fake_download_image(_: str, timeout_seconds: float) -> tuple[str, str] | None:
        assert timeout_seconds > 0
        return None

    monkeypatch.setattr(prompt_runner_module, "download_image", _fake_download_image)

    await mock._send_event(
        {
            "self_id": mock.BOT_ID,
            "user_id": 111,
            "time": 0,
            "message_id": mock._next_message_id(),
            "message_type": "private",
            "sub_type": "friend",
            "sender": {"user_id": 111, "nickname": "Alice", "card": ""},
            "message": [
                {"type": "text", "data": {"text": "see"}},
                {"type": "image", "data": {"url": "http://example.com/a.png"}},
            ],
            "message_format": "array",
            "raw_message": "see[CQ:image]",
            "font": 14,
            "post_type": "message",
        }
    )

    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    assert api_call["action"] == "send_private_msg"

    assert mock_agent.calls_blocks
    _, blocks = mock_agent.calls_blocks[0]
    assert isinstance(blocks[0], TextContentBlock)
    assert "[图片 url=http://example.com/a.png]" in blocks[0].text
    assert not any(isinstance(b, ImageContentBlock) for b in blocks)


async def test_agent_image_reply_sends_image_segment(full_stack) -> None:
    """If agent returns an image ContentPart, ncat should send an image segment to QQ."""
    _, mock, mock_agent = full_stack
    mock_agent.response_parts = [
        ContentPart(type="image", image_base64="aGVsbG8=", image_mime="image/png")
    ]

    await mock.send_private_message(111, "Alice", "send image please")
    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    assert api_call["action"] == "send_private_msg"

    seg = api_call["params"]["message"][0]
    assert seg["type"] == "image"
    assert seg["data"]["file"] == "base64://aGVsbG8="


async def test_full_group_conversation(full_stack) -> None:
    """Test a full group chat flow with @bot."""
    server, mock, mock_agent = full_stack

    await mock.send_group_message(222, "DevGroup", 333, "Bob", " write a function", at_bot=True)
    api_call = await mock.recv_api_call(timeout=5.0)

    assert api_call is not None
    assert api_call["action"] == "send_group_msg"
    assert api_call["params"]["group_id"] == 222

    # Verify prompt includes group context
    _, prompt = mock_agent.calls[0]
    assert "[Group chat DevGroup(222)" in prompt
    assert "user Bob(333)]" in prompt


async def test_new_command_closes_session(full_stack) -> None:
    """Test that /new closes session and next message creates a new one."""
    server, mock, mock_agent = full_stack

    # Create initial session by sending a message
    await mock.send_private_message(555, "Dave", "hello")
    await mock.recv_api_call(timeout=5.0)

    # Send /new command
    await mock.send_private_message(555, "Dave", "/new")
    api_call = await mock.recv_api_call(timeout=3.0)
    assert "新会话" in api_call["params"]["message"][0]["data"]["text"]

    # Session should have been closed
    assert "private:555" in mock_agent.closed_sessions


async def test_multiple_users_isolated(full_stack) -> None:
    """Test that different users get separate agent calls."""
    server, mock, mock_agent = full_stack

    # User A sends message
    await mock.send_private_message(111, "UserA", "msg from A")
    await mock.recv_api_call(timeout=5.0)

    # User B sends message
    await mock.send_private_message(222, "UserB", "msg from B")
    await mock.recv_api_call(timeout=5.0)

    assert len(mock_agent.calls) == 2
    assert mock_agent.calls[0][0] == "private:111"
    assert mock_agent.calls[1][0] == "private:222"


async def test_group_message_ignored_without_at(full_stack) -> None:
    """Integration test: group messages without @bot are silently dropped."""
    server, mock, mock_agent = full_stack

    await mock.send_group_message(222, "G", 111, "X", "no at", at_bot=False)
    api_call = await mock.recv_api_call(timeout=1.0)
    assert api_call is None
    assert len(mock_agent.calls) == 0


async def test_empty_response(full_stack) -> None:
    """Test that empty AI response sends appropriate error message."""
    server, mock, mock_agent = full_stack
    mock_agent.response_text = ""

    await mock.send_private_message(111, "Alice", "test empty")
    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "未返回有效回复" in msg_text


async def test_agent_crash_sends_error(full_stack) -> None:
    """Test that agent crash sends user-friendly error message."""
    server, mock, mock_agent = full_stack
    mock_agent.should_crash = True

    await mock.send_private_message(111, "Alice", "crash test")
    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "Agent 异常" in msg_text


async def test_stop_command_cancels_ai(full_stack) -> None:
    """Integration test: /stop command cancels an active AI task."""
    server, mock, mock_agent = full_stack
    mock_agent.delay = 5.0  # Long delay to keep AI "thinking"

    # Send a message that will take long
    await mock.send_private_message(111, "Alice", "think hard")
    await asyncio.sleep(0.2)  # Let the handler register the active task

    # Send /stop
    await mock.send_private_message(111, "Alice", "/stop")

    # Should receive the "已中断" message
    api_call = await mock.recv_api_call(timeout=3.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "已中断" in msg_text


async def test_busy_rejection_integration(full_stack) -> None:
    """Integration test: concurrent messages for the same chat are rejected."""
    server, mock, mock_agent = full_stack
    mock_agent.delay = 5.0

    # Send first message (will be long-running)
    await mock.send_private_message(111, "Alice", "first")
    await asyncio.sleep(0.2)

    # Send second message — should be rejected
    await mock.send_private_message(111, "Alice", "second")

    # Should receive the "busy" rejection message
    api_call = await mock.recv_api_call(timeout=3.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "正在思考" in msg_text
    assert "/stop" in msg_text

    # Clean up: cancel the first task by sending /stop
    await mock.send_private_message(111, "Alice", "/stop")
    stop_call = await mock.recv_api_call(timeout=3.0)
    assert stop_call is not None
    assert "已中断" in stop_call["params"]["message"][0]["data"]["text"]


async def test_help_includes_stop(full_stack) -> None:
    """Integration test: /help includes /stop command."""
    server, mock, mock_agent = full_stack

    await mock.send_private_message(111, "Alice", "/help")
    api_call = await mock.recv_api_call(timeout=3.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "/stop" in msg_text
