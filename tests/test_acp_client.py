"""Unit tests for NcatAcpClient — request_permission auto-approve behavior."""

import pytest
from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallUpdate,
)

from ncat.acp_client import NcatAcpClient

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
    return NcatAcpClient(agent_manager=object(), chat_id="test:123")


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
