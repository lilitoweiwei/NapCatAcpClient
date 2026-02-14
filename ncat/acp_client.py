"""ACP client module — manages an ACP agent subprocess and session lifecycle.

Contains two core classes:
- NcatAcpClient: implements ACP Client protocol callbacks (session_update, request_permission, etc.)
- AgentManager: manages agent process, ACP connection, session mapping, and prompt sending.
"""

import asyncio
import asyncio.subprocess as aio_subprocess
import contextlib
import json
import logging
import shutil
import sys
from typing import TYPE_CHECKING, Any

from acp import (
    PROTOCOL_VERSION,
    Client,
    RequestError,
    connect_to_agent,
    text_block,
)
from acp.core import ClientSideConnection
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    CreateTerminalResponse,
    CurrentModeUpdate,
    EnvVariable,
    InitializeResponse,
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

if TYPE_CHECKING:
    from ncat.permission import PermissionBroker

logger = logging.getLogger("ncat.acp_client")


class NcatAcpClient(Client):
    """ACP Client protocol implementation for ncat.

    Handles callbacks from the ACP agent: accumulates response text from
    session_update notifications, forwards permission requests to QQ users
    via PermissionBroker, and rejects unsupported capabilities (fs, terminal).
    """

    def __init__(self, agent_manager: "AgentManager") -> None:
        self._agent_manager = agent_manager

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
            # Extract text content and accumulate it
            if isinstance(update.content, TextContentBlock):
                self._agent_manager.accumulate_text(session_id, update.content.text)
        elif isinstance(update, (ToolCallStart, ToolCallProgress)):
            logger.debug("Tool call update for session %s: %s", session_id, type(update).__name__)
        elif isinstance(update, AgentPlanUpdate):
            logger.debug("Agent plan update for session %s", session_id)

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        """Forward permission requests to the QQ user via PermissionBroker.

        Looks up the chat_id for this session, retrieves the last event for
        reply routing, and delegates to the PermissionBroker which handles
        caching, user interaction, and timeout.
        """
        broker = self._agent_manager.permission_broker
        if broker is None:
            # Fallback: auto-approve if no broker is configured (should not happen)
            logger.warning("No PermissionBroker configured, auto-approving")
            first = options[0]
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=first.option_id)
            )

        # Reverse lookup: session_id -> chat_id
        chat_id = self._agent_manager.get_chat_id(session_id)
        if chat_id is None:
            logger.error("No chat_id found for session %s, cancelling permission", session_id)
            from acp.schema import DeniedOutcome

            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        # Retrieve the last event for this chat so we can send reply messages
        event = self._agent_manager.get_last_event(chat_id)
        if event is None:
            logger.error("No event context for chat %s, cancelling permission", chat_id)
            from acp.schema import DeniedOutcome

            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        return await broker.handle(session_id, chat_id, event, tool_call, options)

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


# --- ACP stream observer for debug logging ---

# Maximum length of the JSON dump to log per message (avoids flooding logs
# with large prompt/response payloads).
_LOG_MAX_LEN = 2000


def _acp_stream_observer(event: Any) -> None:
    """Log every JSON-RPC message exchanged over the ACP connection.

    Registered as a StreamObserver on the underlying Connection. Logs at
    DEBUG level so it only appears when debug logging is enabled.
    """
    direction = event.direction.value  # "incoming" or "outgoing"
    msg = event.message
    method = msg.get("method", "")
    msg_id = msg.get("id", "")

    # Compact JSON for the log line, truncated to avoid giant payloads
    raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > _LOG_MAX_LEN:
        raw = raw[:_LOG_MAX_LEN] + f"... ({len(raw)} chars total)"

    # Tag with ← (incoming) or → (outgoing) for quick scanning
    arrow = "←" if direction == "incoming" else "→"
    logger.debug("ACP %s [%s] id=%s method=%s: %s", arrow, direction, msg_id, method, raw)


class AgentManager:
    """Manages the ACP agent subprocess lifecycle and session mapping.

    Responsibilities:
    - Start/stop the agent subprocess
    - Initialize ACP connection
    - Map chat_id to ACP session_id
    - Accumulate response text from session_update notifications
    - Send prompts and return complete responses
    - Handle cancellation via session/cancel
    """

    def __init__(self, command: str, args: list[str], cwd: str) -> None:
        # Agent executable and arguments
        self._command = command
        self._args = args
        # Working directory for the agent process
        self._cwd = cwd

        # Agent subprocess handle
        self._process: aio_subprocess.Process | None = None
        # ACP connection to the agent
        self._conn: ClientSideConnection | None = None
        # ACP client implementation (handles callbacks)
        self._client: NcatAcpClient | None = None

        # Chat-to-session mapping: chat_id -> ACP session_id
        self._sessions: dict[str, str] = {}
        # Text accumulators: session_id -> list of text chunks
        self._accumulators: dict[str, list[str]] = {}
        # Active prompt tracking: chat_id -> True while prompt is in flight
        self._active_prompts: set[str] = set()

        # Permission broker (set externally after construction)
        self._permission_broker: PermissionBroker | None = None
        # Last event dict per chat (needed for permission reply routing)
        self._last_events: dict[str, dict] = {}

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the agent subprocess and initialize the ACP connection."""
        logger.info(
            "Starting agent: %s %s (cwd: %s)",
            self._command,
            " ".join(self._args),
            self._cwd,
        )

        self._client = NcatAcpClient(self)

        # Resolve the executable path. On Windows, create_subprocess_exec cannot
        # run .cmd/.bat scripts directly (WinError 193), so we resolve the full
        # path via shutil.which() and let the shell handle it.
        resolved = shutil.which(self._command)
        if resolved is None:
            raise FileNotFoundError(f"Agent command not found: {self._command}")

        # On Windows, .cmd/.bat wrappers (e.g. npm-installed tools) must be
        # executed through the shell. On Linux this is not needed.
        use_shell = sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat"))
        if use_shell:
            shell_args = [resolved, *self._args]
            logger.debug("Using shell execution for .cmd wrapper: %s", resolved)
            self._process = await asyncio.create_subprocess_exec(
                "cmd",
                "/c",
                *shell_args,
                stdin=aio_subprocess.PIPE,
                stdout=aio_subprocess.PIPE,
                cwd=self._cwd,
            )
        else:
            # Direct exec (Linux, or native .exe on Windows)
            self._process = await asyncio.create_subprocess_exec(
                resolved,
                *self._args,
                stdin=aio_subprocess.PIPE,
                stdout=aio_subprocess.PIPE,
                cwd=self._cwd,
            )

        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("Agent process does not expose stdio pipes")

        # Establish ACP connection over stdin/stdout.
        # Register a stream observer to log all JSON-RPC messages for debugging.
        self._conn = connect_to_agent(
            self._client,
            self._process.stdin,
            self._process.stdout,
            observers=[_acp_stream_observer],
        )

        # Initialize the ACP protocol.
        #
        # We bypass the SDK's conn.initialize() because its serialize_params()
        # uses exclude_defaults=True, which silently drops fields whose values
        # equal pydantic defaults (e.g. clientCapabilities, fs, terminal).
        # Instead we construct the params dict directly so every field is
        # explicitly present on the wire, matching the ACP spec examples.
        init_params = {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {
                    "readTextFile": False,
                    "writeTextFile": False,
                },
                "terminal": False,
            },
            "clientInfo": {
                "name": "ncat",
                "title": "NapCat ACP Client",
                "version": "0.2.0",
            },
        }
        raw_response = await self._conn._conn.send_request("initialize", init_params)
        init_result = InitializeResponse.model_validate(raw_response)

        logger.info(
            "ACP initialized: agent=%s protocol_version=%s",
            init_result.agent_info,
            init_result.protocol_version,
        )

    async def stop(self) -> None:
        """Stop the agent subprocess and clean up resources."""
        # Clear all session mappings and event references
        self._sessions.clear()
        self._accumulators.clear()
        self._active_prompts.clear()
        self._last_events.clear()

        # Close the ACP connection
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

        # Terminate the agent subprocess
        if self._process is not None and self._process.returncode is None:
            logger.info("Terminating agent subprocess (pid=%s)", self._process.pid)
            self._process.terminate()
            with contextlib.suppress(ProcessLookupError):
                await self._process.wait()
            self._process = None

        logger.info("Agent stopped")

    @property
    def is_running(self) -> bool:
        """Check if the agent process is alive."""
        return (
            self._process is not None
            and self._process.returncode is None
            and self._conn is not None
        )

    # --- Permission broker ---

    @property
    def permission_broker(self) -> "PermissionBroker | None":
        """Get the permission broker (set externally after construction)."""
        return self._permission_broker

    @permission_broker.setter
    def permission_broker(self, broker: "PermissionBroker") -> None:
        self._permission_broker = broker

    def get_chat_id(self, session_id: str) -> str | None:
        """Reverse lookup: find the chat_id that owns this ACP session."""
        for chat_id, sid in self._sessions.items():
            if sid == session_id:
                return chat_id
        return None

    def get_last_event(self, chat_id: str) -> dict | None:
        """Get the last message event for a chat (used for reply routing)."""
        return self._last_events.get(chat_id)

    def set_last_event(self, chat_id: str, event: dict) -> None:
        """Store the last message event for a chat."""
        self._last_events[chat_id] = event

    # --- Session management ---

    async def get_or_create_session(self, chat_id: str) -> str:
        """Get existing ACP session for chat_id, or create a new one.

        Returns the ACP session_id.
        """
        if chat_id in self._sessions:
            return self._sessions[chat_id]

        return await self._create_session(chat_id)

    async def _create_session(self, chat_id: str) -> str:
        """Create a new ACP session for the given chat_id."""
        assert self._conn is not None, "Agent not started"

        session = await self._conn.new_session(
            cwd=self._cwd,
            mcp_servers=[],
        )
        session_id = session.session_id
        self._sessions[chat_id] = session_id

        logger.info("Created ACP session %s for chat %s", session_id, chat_id)
        return session_id

    async def close_session(self, chat_id: str) -> None:
        """Close and remove the ACP session for a chat.

        ACP has no explicit session close; we simply remove the mapping
        so a new session will be created on next interaction.
        Also clears any "always" permission decisions for this session.
        """
        session_id = self._sessions.pop(chat_id, None)
        if session_id:
            # Clean up any pending accumulator
            self._accumulators.pop(session_id, None)
            # Clear cached "always" permission decisions for this session
            if self._permission_broker is not None:
                self._permission_broker.clear_session(session_id)
            logger.info("Closed session %s for chat %s", session_id, chat_id)
        # Clean up last event reference
        self._last_events.pop(chat_id, None)

    async def close_all_sessions(self) -> None:
        """Close all active sessions (e.g. on NapCat disconnect)."""
        chat_ids = list(self._sessions.keys())
        for chat_id in chat_ids:
            await self.close_session(chat_id)
        logger.info("All sessions closed")

    # --- Text accumulation (called by NcatAcpClient.session_update) ---

    def accumulate_text(self, session_id: str, text: str) -> None:
        """Accumulate a text chunk for a session (called from session_update callback)."""
        if session_id in self._accumulators:
            self._accumulators[session_id].append(text)

    # --- Prompt sending ---

    def is_busy(self, chat_id: str) -> bool:
        """Check if there is an active prompt for this chat."""
        return chat_id in self._active_prompts

    async def send_prompt(self, chat_id: str, text: str) -> str:
        """Send a prompt to the agent and wait for the complete response.

        Returns the accumulated response text.
        Raises RuntimeError if the agent is not running.
        Raises Exception on agent errors (caller should handle).
        """
        if not self.is_running:
            raise RuntimeError("Agent is not running")

        assert self._conn is not None

        # Get or create session
        session_id = await self.get_or_create_session(chat_id)

        # Initialize text accumulator for this session
        self._accumulators[session_id] = []
        self._active_prompts.add(chat_id)

        try:
            # Send prompt and wait for the turn to complete.
            # During this await, session_update callbacks fire to accumulate text.
            response = await self._conn.prompt(
                session_id=session_id,
                prompt=[text_block(text)],
            )

            # Collect accumulated text
            parts = self._accumulators.pop(session_id, [])
            result = "".join(parts)

            logger.info(
                "Prompt completed for %s (session %s): stop_reason=%s, %d chars",
                chat_id,
                session_id,
                response.stop_reason,
                len(result),
            )
            return result

        except asyncio.CancelledError:
            # Clean up accumulator on cancellation
            self._accumulators.pop(session_id, None)
            raise

        finally:
            self._active_prompts.discard(chat_id)

    # --- Cancellation ---

    async def cancel(self, chat_id: str) -> bool:
        """Send a cancel notification for the active prompt of a chat.

        Returns True if a session existed and cancel was sent, False otherwise.
        """
        session_id = self._sessions.get(chat_id)
        if session_id is None or self._conn is None:
            return False

        if chat_id not in self._active_prompts:
            return False

        logger.info("Sending cancel for chat %s (session %s)", chat_id, session_id)
        await self._conn.cancel(session_id=session_id)
        return True
