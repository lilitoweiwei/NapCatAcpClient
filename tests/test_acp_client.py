"""Unit tests for NcatAcpClient — request_permission auto-approve behavior."""

import pytest
from acp.schema import (
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallStart,
    ToolCallUpdate,
)

from ncat.acp_client import NcatAcpClient
from ncat.models import UsageSnapshot, VisibleTurnEvent

pytestmark = pytest.mark.asyncio


def _tool_call(
    *,
    kind: str | None = "read",
    title: str = "Read file",
) -> ToolCallUpdate:
    """Minimal ToolCallUpdate for permission tests."""
    return ToolCallUpdate(toolCallId="tc1", kind=kind, title=title, rawInput=None)


@pytest.fixture
def client():
    """NcatAcpClient with a minimal agent manager (request_permission does not use it)."""
    return NcatAcpClient(agent_manager=RecordingAgentManager(), chat_id="test:123")


class RecordingAgentManager:
    def __init__(self) -> None:
        self.visible_events: list[tuple[str, str, VisibleTurnEvent]] = []
        self.usage_updates: list[tuple[str, UsageSnapshot | None]] = []

    def record_visible_event(self, chat_id: str, session_id: str, event: VisibleTurnEvent) -> bool:
        self.visible_events.append((chat_id, session_id, event))
        return True

    def update_usage(self, chat_id: str, usage: UsageSnapshot | None) -> None:
        self.usage_updates.append((chat_id, usage))


async def test_request_permission_prefers_allow_always(client: NcatAcpClient) -> None:
    """When options include both allow_once and allow_always, prefer allow_always."""
    options = [
        PermissionOption(kind="allow_once", name="Allow once", optionId="o1"),
        PermissionOption(kind="allow_always", name="Allow always", optionId="o2"),
        PermissionOption(kind="reject_once", name="Reject once", optionId="o3"),
    ]
    resp = await client.request_permission(
        options=options,
        session_id="s1",
        tool_call=_tool_call(),
    )
    assert isinstance(resp, RequestPermissionResponse)
    assert isinstance(resp.outcome, AllowedOutcome)
    assert resp.outcome.option_id == "o2"


async def test_request_permission_then_allow_once(client: NcatAcpClient) -> None:
    """When there is no allow_always, use allow_once."""
    options = [
        PermissionOption(kind="reject_once", name="Reject once", optionId="o3"),
        PermissionOption(kind="allow_once", name="Allow once", optionId="o1"),
    ]
    resp = await client.request_permission(
        options=options,
        session_id="s1",
        tool_call=_tool_call(),
    )
    assert isinstance(resp.outcome, AllowedOutcome)
    assert resp.outcome.option_id == "o1"


async def test_request_permission_fallback_to_first_option(client: NcatAcpClient) -> None:
    """When there is no allow_*, use the first option."""
    options = [
        PermissionOption(kind="reject_once", name="Reject once", optionId="o3"),
        PermissionOption(kind="reject_always", name="Reject always", optionId="o4"),
    ]
    resp = await client.request_permission(
        options=options,
        session_id="s1",
        tool_call=_tool_call(),
    )
    assert isinstance(resp.outcome, AllowedOutcome)
    assert resp.outcome.option_id == "o3"


async def test_request_permission_empty_options_denied(client: NcatAcpClient) -> None:
    """Empty options list returns DeniedOutcome."""
    resp = await client.request_permission(
        options=[],
        session_id="s1",
        tool_call=_tool_call(),
    )
    assert isinstance(resp.outcome, DeniedOutcome)
    assert resp.outcome.outcome == "cancelled"


async def test_session_update_records_visible_thinking_event() -> None:
    manager = RecordingAgentManager()
    client = NcatAcpClient(agent_manager=manager, chat_id="test:123")

    await client.session_update(
        session_id="s1",
        update=AgentThoughtChunk(
            sessionUpdate="agent_thought_chunk",
            content=TextContentBlock(type="text", text="thinking..."),
        ),
    )

    assert manager.visible_events[0][2].status_text == "<AI 正在思考中>"


async def test_session_update_records_visible_tool_event() -> None:
    manager = RecordingAgentManager()
    client = NcatAcpClient(agent_manager=manager, chat_id="test:123")

    await client.session_update(
        session_id="s1",
        update=ToolCallStart(
            sessionUpdate="tool_call",
            toolCallId="tc1",
            title="Read file",
            kind="read",
            status="pending",
        ),
    )

    assert manager.visible_events[0][2].status_text == "<AI 正在调用：Read file>"


async def test_session_update_records_visible_plan_event() -> None:
    manager = RecordingAgentManager()
    client = NcatAcpClient(agent_manager=manager, chat_id="test:123")

    await client.session_update(
        session_id="s1",
        update=AgentPlanUpdate(sessionUpdate="plan", entries=[]),
    )

    assert manager.visible_events[0][2].status_text == "<AI 正在规划任务>"


async def test_request_permission_records_visible_status() -> None:
    manager = RecordingAgentManager()
    client = NcatAcpClient(agent_manager=manager, chat_id="test:123")
    options = [PermissionOption(kind="allow_once", name="Allow once", optionId="o1")]

    await client.request_permission(
        options=options,
        session_id="s1",
        tool_call=ToolCallUpdate(toolCallId="tc1", kind="edit", title="Edit file", rawInput=None),
    )

    assert manager.visible_events[0][2].status_text == "<AI 请求权限：Edit file（已自动允许）>"


async def test_session_update_records_usage_snapshot() -> None:
    from acp.schema import Cost, UsageUpdate

    manager = RecordingAgentManager()
    client = NcatAcpClient(agent_manager=manager, chat_id="test:123")

    await client.session_update(
        session_id="s1",
        update=UsageUpdate(
            sessionUpdate="usage_update",
            used=120,
            size=1000,
            cost=Cost(amount=0.25, currency="USD"),
        ),
    )

    assert manager.usage_updates
    chat_id, usage = manager.usage_updates[0]
    assert chat_id == "test:123"
    assert usage is not None
    assert usage.used == 120
    assert usage.size == 1000
    assert usage.cost_amount == 0.25
