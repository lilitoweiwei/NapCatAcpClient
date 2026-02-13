"""Tests for the WebSocket server module."""

import asyncio

import pytest
import pytest_asyncio
import websockets

from nochan.opencode import OpenCodeResponse, SubprocessOpenCodeBackend
from nochan.prompt import PromptBuilder
from nochan.server import NochanServer
from nochan.session import SessionManager
from tests.mock_napcat import MockNapCat

pytestmark = pytest.mark.asyncio


class FakeOpenCodeBackend(SubprocessOpenCodeBackend):
    """Fake backend that returns canned responses without calling opencode."""

    def __init__(self) -> None:
        super().__init__(command="echo", work_dir=".", max_concurrent=1)
        self.last_message: str | None = None
        self.response = OpenCodeResponse(
            session_id="ses_fake123",
            content="Fake AI response",
            success=True,
            error=None,
        )
        # Configurable delay before returning response (for timeout/cancel tests)
        self.delay: float = 0

    async def _run(self, session_id: str | None, message: str) -> OpenCodeResponse:
        self.last_message = message
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.response


@pytest_asyncio.fixture
async def server_and_mock(tmp_path):
    """Start a NochanServer on a random port and yield (server, mock_client, fake_backend)."""
    sm = SessionManager(str(tmp_path / "test.db"))
    await sm.init()

    fake_backend = FakeOpenCodeBackend()
    prompt_builder = PromptBuilder(tmp_path / "prompts")
    server = NochanServer(
        host="127.0.0.1",
        port=0,  # OS assigns a free port
        session_manager=sm,
        opencode_backend=fake_backend,
        prompt_builder=prompt_builder,
    )

    # Start server in background; we need to find the actual port
    # Use websockets.serve directly to get the server object
    ws_server = await websockets.serve(server._handler_ws, "127.0.0.1", 0)
    # Extract the port from the server socket
    port = ws_server.sockets[0].getsockname()[1]
    server._bot_id = None  # will be set by lifecycle event

    mock = MockNapCat(f"ws://127.0.0.1:{port}")
    await mock.connect()

    # Wait briefly for lifecycle event to be processed
    await asyncio.sleep(0.1)

    yield server, mock, fake_backend

    await mock.close()
    ws_server.close()
    await ws_server.wait_closed()
    await sm.close()


async def test_connection_and_lifecycle(server_and_mock) -> None:
    """Test that server accepts connection and extracts bot_id from lifecycle event."""
    server, mock, _ = server_and_mock
    # Give server time to process the lifecycle event
    await asyncio.sleep(0.2)
    assert server._bot_id == MockNapCat.BOT_ID


async def test_send_api(server_and_mock) -> None:
    """Test that send_api sends a request and receives a response."""
    server, mock, _ = server_and_mock
    await asyncio.sleep(0.1)

    # Start receiving on mock in background
    recv_task = asyncio.create_task(mock.recv_api_call(timeout=3.0))

    # Send API call from server
    response = await server.send_api("get_login_info")

    api_call = await recv_task
    assert api_call is not None
    assert api_call["action"] == "get_login_info"
    assert response is not None
    assert response["status"] == "ok"


async def test_private_message_reply(server_and_mock) -> None:
    """Test that a private message triggers an AI response."""
    server, mock, fake_backend = server_and_mock
    await asyncio.sleep(0.1)

    # Send a private message from mock
    await mock.send_private_message(111, "Alice", "hello")

    # Server should call OpenCode and then send a reply via API
    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    assert api_call["action"] == "send_private_msg"
    assert api_call["params"]["user_id"] == 111
    # The response text should be in the message segments
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert msg_text == "Fake AI response"


async def test_group_message_ignored_without_at(server_and_mock) -> None:
    """Test that group messages without @bot are ignored."""
    server, mock, _ = server_and_mock
    await asyncio.sleep(0.1)

    # Send group message WITHOUT @bot
    await mock.send_group_message(222, "TestGroup", 111, "Alice", "hello", at_bot=False)

    # Should NOT receive any API call
    api_call = await mock.recv_api_call(timeout=1.0)
    assert api_call is None


async def test_group_message_with_at_bot(server_and_mock) -> None:
    """Test that group messages with @bot trigger a response."""
    server, mock, _ = server_and_mock
    await asyncio.sleep(0.1)

    await mock.send_group_message(222, "TestGroup", 111, "Alice", " hello", at_bot=True)

    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    assert api_call["action"] == "send_group_msg"
    assert api_call["params"]["group_id"] == 222


async def test_command_new(server_and_mock) -> None:
    """Test that /new command creates a new session."""
    server, mock, _ = server_and_mock
    await asyncio.sleep(0.1)

    await mock.send_private_message(111, "Alice", "/new")

    api_call = await mock.recv_api_call(timeout=3.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "新会话" in msg_text


async def test_command_help(server_and_mock) -> None:
    """Test that /help command returns help text."""
    server, mock, _ = server_and_mock
    await asyncio.sleep(0.1)

    await mock.send_private_message(111, "Alice", "/help")

    api_call = await mock.recv_api_call(timeout=3.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "/new" in msg_text
    assert "/help" in msg_text


async def test_command_unknown(server_and_mock) -> None:
    """Test that unknown /commands return help text."""
    server, mock, _ = server_and_mock
    await asyncio.sleep(0.1)

    await mock.send_private_message(111, "Alice", "/xyz")

    api_call = await mock.recv_api_call(timeout=3.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "/new" in msg_text


async def test_opencode_error_sends_error_message(server_and_mock) -> None:
    """Test that OpenCode errors result in an error message to the user."""
    server, mock, fake_backend = server_and_mock
    await asyncio.sleep(0.1)

    # Make the backend return an error
    fake_backend.response = OpenCodeResponse(
        session_id="ses_err",
        content="",
        success=False,
        error="API rate limit",
    )

    await mock.send_private_message(111, "Alice", "cause error")

    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "出错" in msg_text


async def test_heartbeat_no_crash(server_and_mock) -> None:
    """Test that heartbeat events don't cause errors."""
    server, mock, _ = server_and_mock
    await asyncio.sleep(0.1)

    await mock.send_heartbeat()
    await asyncio.sleep(0.2)
    # No crash = pass


async def test_command_stop_no_active(server_and_mock) -> None:
    """Test that /stop when no AI is running returns appropriate message."""
    server, mock, _ = server_and_mock
    await asyncio.sleep(0.1)

    await mock.send_private_message(111, "Alice", "/stop")

    api_call = await mock.recv_api_call(timeout=3.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "没有进行中" in msg_text


async def test_command_stop_cancels_ai(server_and_mock) -> None:
    """Test that /stop cancels an active AI processing task via WebSocket."""
    server, mock, fake_backend = server_and_mock
    await asyncio.sleep(0.1)
    fake_backend.delay = 5.0  # Long delay to keep AI "thinking"

    # Send a message that will take long to process
    await mock.send_private_message(111, "Alice", "think hard")
    await asyncio.sleep(0.2)  # Let the handler register the active task

    # Send /stop
    await mock.send_private_message(111, "Alice", "/stop")

    # Should receive the "已中断" message
    api_call = await mock.recv_api_call(timeout=3.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "已中断" in msg_text
