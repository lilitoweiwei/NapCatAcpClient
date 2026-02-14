"""Unit tests for the permission broker."""

import asyncio
from typing import Any

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from ncat.permission import PermissionBroker

pytestmark = pytest.mark.asyncio


class ReplyQueue:
    """Collect outgoing replies for assertions.

    Uses an asyncio.Queue to make ordering assertions deterministic.
    """

    def __init__(self) -> None:
        self.items: asyncio.Queue[tuple[dict, str]] = asyncio.Queue()
        self.replies: list[tuple[dict, str]] = []

    async def __call__(self, event: dict, text: str) -> None:
        item = (event, text)
        self.replies.append(item)
        self.items.put_nowait(item)

    async def get_text(self, timeout: float = 1.0) -> str:
        """Get the next reply text with a timeout."""
        _, text = await asyncio.wait_for(self.items.get(), timeout=timeout)
        return text


def _tool_call(
    *,
    kind: str | None = "read",
    title: str = "Read file",
    raw_input: Any | None = None,
) -> ToolCallUpdate:
    """Build a minimal ToolCallUpdate for tests."""
    return ToolCallUpdate(toolCallId="tool_call_1", kind=kind, title=title, rawInput=raw_input)


def _options() -> list[PermissionOption]:
    """Standard permission option set used in tests."""
    return [
        PermissionOption(kind="allow_once", name="Allow once", optionId="o1"),
        PermissionOption(kind="allow_always", name="Allow always", optionId="o2"),
        PermissionOption(kind="reject_once", name="Reject once", optionId="o3"),
        PermissionOption(kind="reject_always", name="Reject always", optionId="o4"),
    ]


async def _wait_pending(broker: PermissionBroker, chat_id: str) -> None:
    """Wait until broker.has_pending(chat_id) becomes True."""
    for _ in range(200):
        if broker.has_pending(chat_id):
            return
        await asyncio.sleep(0)
    raise AssertionError("PermissionBroker did not enter pending state in time.")


async def test_handle_resolves_after_user_reply() -> None:
    """handle() should send a message and return a selected outcome after user replies."""
    replies = ReplyQueue()
    broker = PermissionBroker(reply_fn=replies, timeout=5)

    session_id = "sess_1"
    chat_id = "private:111"
    event = {"user_id": 111}
    options = _options()

    task = asyncio.create_task(
        broker.handle(
            session_id=session_id,
            chat_id=chat_id,
            event=event,
            tool_call=_tool_call(raw_input={"path": "/tmp/a.txt"}),
            options=options,
        )
    )

    first = await replies.get_text()
    assert "Agent 请求执行操作" in first

    await _wait_pending(broker, chat_id)
    assert broker.has_pending(chat_id)

    # Invalid inputs should not resolve the pending request.
    assert broker.try_resolve(chat_id, "abc") is False
    assert broker.try_resolve(chat_id, "999") is False

    # Valid reply resolves the request.
    assert broker.try_resolve(chat_id, "1") is True

    resp = await asyncio.wait_for(task, timeout=1.0)
    assert resp.outcome.outcome == "selected"
    assert resp.outcome.option_id == options[0].option_id

    assert broker.has_pending(chat_id) is False
    assert len(replies.replies) == 1


async def test_handle_timeout_cancels_and_notifies_user() -> None:
    """handle() should auto-cancel on timeout and notify the user."""
    replies = ReplyQueue()
    broker = PermissionBroker(reply_fn=replies, timeout=0.05)

    session_id = "sess_timeout"
    chat_id = "private:222"
    event = {"user_id": 222}

    task = asyncio.create_task(
        broker.handle(
            session_id=session_id,
            chat_id=chat_id,
            event=event,
            tool_call=_tool_call(raw_input="x" * 3),
            options=_options(),
        )
    )

    _ = await replies.get_text()
    resp = await asyncio.wait_for(task, timeout=1.0)
    timeout_text = await replies.get_text()

    assert resp.outcome.outcome == "cancelled"
    assert "权限请求已超时" in timeout_text
    assert broker.has_pending(chat_id) is False
    assert len(replies.replies) == 2


async def test_cancel_pending_cancels_handle() -> None:
    """cancel_pending() should cancel a waiting handle() call."""
    replies = ReplyQueue()
    broker = PermissionBroker(reply_fn=replies, timeout=10)

    session_id = "sess_cancel"
    chat_id = "private:333"
    event = {"user_id": 333}

    task = asyncio.create_task(
        broker.handle(
            session_id=session_id,
            chat_id=chat_id,
            event=event,
            tool_call=_tool_call(raw_input={"a": 1}),
            options=_options(),
        )
    )

    _ = await replies.get_text()
    await _wait_pending(broker, chat_id)

    broker.cancel_pending(chat_id)

    resp = await asyncio.wait_for(task, timeout=1.0)
    assert resp.outcome.outcome == "cancelled"
    assert broker.has_pending(chat_id) is False
    assert len(replies.replies) == 1


async def test_always_cache_and_clear_session() -> None:
    """Selecting an always option should cache it per session+tool kind."""
    replies = ReplyQueue()
    broker = PermissionBroker(reply_fn=replies, timeout=5)

    session_id = "sess_cache"
    chat_id = "private:444"
    event = {"user_id": 444}
    options = _options()
    tool_call = _tool_call(kind="read", raw_input={"path": "x"})

    first_task = asyncio.create_task(
        broker.handle(
            session_id=session_id,
            chat_id=chat_id,
            event=event,
            tool_call=tool_call,
            options=options,
        )
    )

    _ = await replies.get_text()
    await _wait_pending(broker, chat_id)

    # Pick "allow_always"
    assert broker.try_resolve(chat_id, "2") is True
    first = await asyncio.wait_for(first_task, timeout=1.0)
    assert first.outcome.outcome == "selected"
    assert first.outcome.option_id == options[1].option_id
    assert len(replies.replies) == 1

    # Second request: should be auto-resolved without sending another QQ message.
    second = await broker.handle(
        session_id=session_id,
        chat_id=chat_id,
        event={"user_id": 444, "seq": 2},
        tool_call=_tool_call(kind="read", raw_input={"path": "y"}),
        options=options,
    )
    assert second.outcome.outcome == "selected"
    assert second.outcome.option_id == options[1].option_id
    assert len(replies.replies) == 1

    # Clearing the session should remove the always cache.
    broker.clear_session(session_id)

    third_task = asyncio.create_task(
        broker.handle(
            session_id=session_id,
            chat_id=chat_id,
            event={"user_id": 444, "seq": 3},
            tool_call=_tool_call(kind="read", raw_input={"path": "z"}),
            options=options,
        )
    )
    _ = await replies.get_text()
    await _wait_pending(broker, chat_id)
    assert broker.try_resolve(chat_id, "1") is True
    third = await asyncio.wait_for(third_task, timeout=1.0)
    assert third.outcome.outcome == "selected"
    assert len(replies.replies) == 2


async def test_format_permission_message_truncates_raw_input() -> None:
    """_format_permission_message() should truncate raw_input when configured."""

    async def _noop_reply(_: dict, __: str) -> None:
        return None

    broker = PermissionBroker(reply_fn=_noop_reply, timeout=1, raw_input_max_len=10)
    tool_call = _tool_call(kind="read", title="Read file", raw_input="0123456789ABCDE")
    msg = broker._format_permission_message(
        tool_call=tool_call,
        options=[PermissionOption(kind="allow_once", name="Allow once", optionId="o1")],
    )

    assert "Agent 请求执行操作" in msg
    assert "[read]" in msg
    assert "Read file" in msg
    assert "参数:" in msg
    assert "0123456789...(已截断)" in msg
    assert "1. Allow once" in msg
