"""ACP client module — ACP protocol callbacks for ncat.

Contains `NcatAcpClient`, which implements ACP Client protocol callbacks
(`session_update`, `request_permission`, etc.). Permission requests are
auto-approved by the server (no user prompt). Agent subprocess management
and session lifecycle live in `ncat.agent_manager.AgentManager`.
"""

import logging
from typing import TYPE_CHECKING, Any

from acp import Client, RequestError
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    CreateTerminalResponse,
    CurrentModeUpdate,
    DeniedOutcome,
    EnvVariable,
    ImageContentBlock,
    KillTerminalCommandResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    SessionInfoUpdate,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from ncat.log import debug_event
from ncat.models import ContentPart, UsageSnapshot, VisibleTurnEvent

if TYPE_CHECKING:
    from ncat.agent_manager import AgentManager

logger = logging.getLogger("ncat.acp_client")


def _format_tool_label(update: ToolCallStart | ToolCallProgress | ToolCallUpdate) -> str:
    title = (update.title or "").strip() if getattr(update, "title", None) else ""
    if title:
        return title
    kind = update.kind or "other"
    return kind


def _record_visible_event(
    agent_manager: Any,
    chat_id: str,
    session_id: str,
    event: VisibleTurnEvent,
) -> None:
    record = getattr(agent_manager, "record_visible_event", None)
    if callable(record):
        record(chat_id, session_id, event)


class NcatAcpClient(Client):
    """ACP Client protocol implementation for ncat.

    Handles callbacks from the ACP agent: accumulates response text from
    session_update notifications, auto-approves all permission requests
    (prefer allow_always, then allow_once, else first option), and rejects
    unsupported capabilities (fs, terminal).
    """

    def __init__(self, agent_manager: "AgentManager", chat_id: str) -> None:
        self._agent_manager = agent_manager
        self._chat_id = chat_id  # Used to locate AgentConnection in callbacks

    # --- Core callbacks ---

    async def session_update(
        self,
        session_id: str,
        update: (
            UserMessageChunk
            | AgentMessageChunk
            | AgentThoughtChunk
            | ToolCallStart
            | ToolCallProgress
            | AgentPlanUpdate
            | AvailableCommandsUpdate
            | CurrentModeUpdate
            | ConfigOptionUpdate
            | SessionInfoUpdate
            | UsageUpdate
        ),
        **kwargs: Any,
    ) -> None:
        """Handle streaming updates from the agent.

        Accumulates text from AgentMessageChunk updates. Other update types
        are logged but not processed (e.g. tool calls, plans).
        """
        if isinstance(update, AgentMessageChunk):
            # Extract content and accumulate it in order.
            if isinstance(update.content, TextContentBlock):
                self._agent_manager.accumulate_part(
                    self._chat_id,
                    session_id,
                    ContentPart(type="text", text=update.content.text),
                )
            elif isinstance(update.content, ImageContentBlock):
                self._agent_manager.accumulate_part(
                    self._chat_id,
                    session_id,
                    ContentPart(
                        type="image",
                        image_base64=update.content.data,
                        image_mime=update.content.mime_type,
                    ),
                )
        elif isinstance(update, (ToolCallStart, ToolCallProgress)):
            status = update.status or "pending"
            if status in {"pending", "in_progress", "failed"}:
                tool_label = _format_tool_label(update)
                if status == "failed":
                    status_text = f"<AI 调用失败：{tool_label}>"
                else:
                    status_text = f"<AI 正在调用：{tool_label}>"
                _record_visible_event(
                    self._agent_manager,
                    self._chat_id,
                    session_id,
                    VisibleTurnEvent(
                        key=f"tool:{update.tool_call_id}:{status}",
                        status_text=status_text,
                    ),
                )
            debug_event(
                logger,
                "tool_call_update",
                "Tool call update received",
                session_id=session_id,
                chat_id=self._chat_id,
                update_type=type(update).__name__,
            )
        elif isinstance(update, AgentPlanUpdate):
            _record_visible_event(
                self._agent_manager,
                self._chat_id,
                session_id,
                VisibleTurnEvent(
                    key=f"plan:{len(update.entries)}",
                    status_text="<AI 正在规划任务>",
                ),
            )
            debug_event(
                logger,
                "agent_plan_update",
                "Agent plan update received",
                session_id=session_id,
                chat_id=self._chat_id,
            )
        elif isinstance(update, AgentThoughtChunk):
            _record_visible_event(
                self._agent_manager,
                self._chat_id,
                session_id,
                VisibleTurnEvent(
                    key="thinking",
                    status_text="<AI 正在思考中>",
                ),
            )
        elif isinstance(update, UsageUpdate):
            self._agent_manager.update_usage(
                self._chat_id,
                UsageSnapshot(
                    used=update.used,
                    size=update.size,
                    cost_amount=(update.cost.amount if update.cost is not None else None),
                    cost_currency=(update.cost.currency if update.cost is not None else None),
                ),
            )

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        """Auto-approve permission requests.

        Prefer allow_always, then allow_once, otherwise fall back to the first option.
        """
        if not options:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        selected = None
        for opt in options:
            if opt.kind == "allow_always":
                selected = opt
                break
        if selected is None:
            for opt in options:
                if opt.kind == "allow_once":
                    selected = opt
                    break
        if selected is None:
            selected = options[0]
        tool_label = _format_tool_label(tool_call)
        _record_visible_event(
            self._agent_manager,
            self._chat_id,
            session_id,
            VisibleTurnEvent(
                key=f"permission:{tool_call.tool_call_id}",
                status_text=f"<AI 请求权限：{tool_label}（已自动允许）>",
            ),
        )
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=selected.option_id)
        )

    # --- Unsupported capabilities (fs, terminal) ---

    async def write_text_file(
        self, content: str, path: str, session_id: str, **kwargs: Any
    ) -> WriteTextFileResponse | None:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalCommandResponse | None:
        raise RequestError.method_not_found("terminal/kill")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise RequestError.method_not_found(method)

    def on_connect(self, conn: Any) -> None:
        """Called when the ACP connection is established (no-op for ncat)."""
        return None
