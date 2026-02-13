"""Integration tests: full pipeline from mock NapCat to fake OpenCode and back."""

import asyncio

import pytest
import pytest_asyncio
import websockets

from nochan.opencode import OpenCodeResponse, SubprocessOpenCodeBackend
from nochan.server import NochanServer
from nochan.session import SessionManager
from tests.mock_napcat import MockNapCat


pytestmark = pytest.mark.asyncio


class FakeOpenCodeBackend(SubprocessOpenCodeBackend):
    """Fake backend that records calls and returns configurable responses."""

    def __init__(self) -> None:
        super().__init__(command="echo", work_dir=".", max_concurrent=1)
        self.calls: list[tuple[str | None, str]] = []
        self.response = OpenCodeResponse(
            session_id="ses_integ_001",
            content="Integration AI response",
            success=True,
            error=None,
        )

    async def _run(self, session_id: str | None, message: str) -> OpenCodeResponse:
        self.calls.append((session_id, message))
        return self.response


@pytest_asyncio.fixture
async def full_stack(tmp_path):
    """Set up the full nochan stack with mock NapCat and fake OpenCode."""
    sm = SessionManager(str(tmp_path / "integ.db"))
    await sm.init()

    fake_backend = FakeOpenCodeBackend()
    server = NochanServer(
        host="127.0.0.1",
        port=0,
        session_manager=sm,
        opencode_backend=fake_backend,
    )

    ws_server = await websockets.serve(server._handler_ws, "127.0.0.1", 0)
    port = ws_server.sockets[0].getsockname()[1]

    mock = MockNapCat(f"ws://127.0.0.1:{port}")
    await mock.connect()
    await asyncio.sleep(0.2)

    yield server, mock, fake_backend, sm

    await mock.close()
    ws_server.close()
    await ws_server.wait_closed()
    await sm.close()


async def test_full_private_conversation(full_stack) -> None:
    """Test a full private chat: message -> AI -> reply, session persisted."""
    server, mock, fake_backend, sm = full_stack

    # Send first message
    await mock.send_private_message(111, "Alice", "你好")
    api_call = await mock.recv_api_call(timeout=5.0)

    assert api_call is not None
    assert api_call["action"] == "send_private_msg"
    assert api_call["params"]["message"][0]["data"]["text"] == "Integration AI response"

    # Verify session was created
    session = await sm.get_active_session("private:111")
    assert session is not None
    assert session.opencode_session_id == "ses_integ_001"

    # Verify OpenCode was called with context
    assert len(fake_backend.calls) == 1
    oc_session_id, prompt = fake_backend.calls[0]
    assert oc_session_id is None  # first call, no session yet
    assert "[私聊，用户 Alice(111)]" in prompt
    assert "你好" in prompt


async def test_full_group_conversation(full_stack) -> None:
    """Test a full group chat flow with @bot."""
    server, mock, fake_backend, sm = full_stack

    await mock.send_group_message(
        222, "开发群", 333, "Bob", " 帮我写个函数", at_bot=True
    )
    api_call = await mock.recv_api_call(timeout=5.0)

    assert api_call is not None
    assert api_call["action"] == "send_group_msg"
    assert api_call["params"]["group_id"] == 222

    # Verify prompt includes group context
    _, prompt = fake_backend.calls[0]
    assert "[群聊 开发群(222)" in prompt
    assert "用户 Bob(333)]" in prompt


async def test_session_continuation(full_stack) -> None:
    """Test that the second message in a session passes the OpenCode session ID."""
    server, mock, fake_backend, sm = full_stack

    # First message: no session yet
    await mock.send_private_message(444, "Carol", "first")
    await mock.recv_api_call(timeout=5.0)
    assert fake_backend.calls[0][0] is None

    # Second message: should reuse session
    await mock.send_private_message(444, "Carol", "second")
    await mock.recv_api_call(timeout=5.0)
    assert fake_backend.calls[1][0] == "ses_integ_001"


async def test_new_command_resets_session(full_stack) -> None:
    """Test that /new archives session and creates a new one."""
    server, mock, fake_backend, sm = full_stack

    # Create initial session by sending a message
    await mock.send_private_message(555, "Dave", "hello")
    await mock.recv_api_call(timeout=5.0)

    s1 = await sm.get_active_session("private:555")
    assert s1 is not None

    # Send /new command
    await mock.send_private_message(555, "Dave", "/new")
    api_call = await mock.recv_api_call(timeout=3.0)
    assert "新会话" in api_call["params"]["message"][0]["data"]["text"]

    # Session should be different
    s2 = await sm.get_active_session("private:555")
    assert s2 is not None
    assert s2.id != s1.id
    assert s2.opencode_session_id is None  # new session, not yet called OpenCode


async def test_multiple_users_isolated(full_stack) -> None:
    """Test that different users get separate sessions."""
    server, mock, fake_backend, sm = full_stack

    # User A sends message
    await mock.send_private_message(111, "UserA", "msg from A")
    await mock.recv_api_call(timeout=5.0)

    # User B sends message
    await mock.send_private_message(222, "UserB", "msg from B")
    await mock.recv_api_call(timeout=5.0)

    sa = await sm.get_active_session("private:111")
    sb = await sm.get_active_session("private:222")
    assert sa is not None
    assert sb is not None
    assert sa.id != sb.id


async def test_group_message_ignored_without_at(full_stack) -> None:
    """Integration test: group messages without @bot are silently dropped."""
    server, mock, fake_backend, sm = full_stack

    await mock.send_group_message(222, "G", 111, "X", "no at", at_bot=False)
    api_call = await mock.recv_api_call(timeout=1.0)
    assert api_call is None
    assert len(fake_backend.calls) == 0


async def test_opencode_empty_response(full_stack) -> None:
    """Test that empty AI response sends appropriate error message."""
    server, mock, fake_backend, sm = full_stack

    fake_backend.response = OpenCodeResponse(
        session_id="ses_empty",
        content="",
        success=True,
        error=None,
    )

    await mock.send_private_message(111, "Alice", "test empty")
    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "未返回有效回复" in msg_text


async def test_opencode_failure(full_stack) -> None:
    """Test that OpenCode failure sends user-friendly error message."""
    server, mock, fake_backend, sm = full_stack

    fake_backend.response = OpenCodeResponse(
        session_id="ses_fail",
        content="",
        success=False,
        error="Process crashed",
    )

    await mock.send_private_message(111, "Alice", "crash test")
    api_call = await mock.recv_api_call(timeout=5.0)
    assert api_call is not None
    msg_text = api_call["params"]["message"][0]["data"]["text"]
    assert "出错" in msg_text
