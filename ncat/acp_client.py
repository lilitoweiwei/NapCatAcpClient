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

from ncat.models import ContentPart

if TYPE_CHECKING:
    from ncat.agent_manager import AgentManager

logger = logging.getLogger("ncat.acp_client")


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
            logger.debug("Tool call update for session %s (chat %s): %s", session_id, self._chat_id, type(update).__name__)
        elif isinstance(update, AgentPlanUpdate):
            logger.debug("Agent plan update for session %s (chat %s)", session_id, self._chat_id)

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        """Auto-approve permission requests: prefer allow_always, then allow_once, else first option."""
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
