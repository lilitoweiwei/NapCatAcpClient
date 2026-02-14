"""Tests for the WebSocket server module."""

import asyncio

import pytest
import pytest_asyncio
import websockets

from ncat.napcat_server import NcatNapCatServer
from tests.conftest import MockAgentManager
from tests.mock_napcat import MockNapCat

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def server_and_mock():
    """Start a NcatNapCatServer on a random port and yield (server, mock_client, mock_agent)."""
    mock_agent = MockAgentManager()
    server = NcatNapCatServer(
        host="127.0.0.1",
        port=0,  # OS assigns a free port
        agent_manager=mock_agent,
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

    yield server, mock, mock_agent

    await mock.close()
    ws_server.close()
    await ws_server.wait_closed()


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
    server, mock, mock_agent = server_and_mock
    await asyncio.sleep(0.1)

    # Send a private message from mock
    await mock.send_private_message(111, "Alice", "hello")

    # Server should call agent and then send a reply via API
    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    assert api_call["action"] == "send_private_msg"
    assert api_call["params"]["user_id"] == 111
    # The response text should be in the message segments
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert msg_text == "Mock AI response"


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
    """Test that /new command closes session."""
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


async def test_disconnect_closes_all_sessions(server_and_mock) -> None:
    """Test that NapCat disconnect closes all ACP sessions."""
    server, mock, mock_agent = server_and_mock
    await asyncio.sleep(0.1)

    # Send a message first to establish a session mapping
    await mock.send_private_message(111, "Alice", "hello")
    await mock.recv_api_call(timeout=5.0)

    # Disconnect the mock client
    await mock.close()
    await asyncio.sleep(0.3)

    # Agent manager should have been told to close all sessions
    assert mock_agent.all_sessions_closed
