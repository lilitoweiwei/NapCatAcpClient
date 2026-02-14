"""ACP agent subprocess manager — owns agent lifecycle and sessions.

This module contains `AgentManager`, which starts/stops the external ACP agent
process, establishes the ACP connection, and maps QQ chat IDs to ACP sessions.

Protocol callbacks (e.g. `session_update`, `request_permission`) are implemented
by `ncat.acp_client.NcatAcpClient`.
"""

import asyncio
import asyncio.subprocess as aio_subprocess
import contextlib
import json
import logging
import shutil
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from acp import PROTOCOL_VERSION, connect_to_agent
from acp.core import ClientSideConnection
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    InitializeResponse,
    ResourceContentBlock,
    TextContentBlock,
)

from ncat.acp_client import NcatAcpClient
from ncat.models import ContentPart

if TYPE_CHECKING:
    from ncat.permission import PermissionBroker

logger = logging.getLogger("ncat.acp_client")

PromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)


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
        # Content accumulators: session_id -> ordered content parts
        self._accumulators: dict[str, list[ContentPart]] = {}
        # Active prompt tracking: chat_id -> True while prompt is in flight
        self._active_prompts: set[str] = set()

        # Agent prompt capabilities (populated on initialize)
        self._supports_image: bool = False

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
        prompt_caps = getattr(init_result.agent_capabilities, "prompt_capabilities", None)
        self._supports_image = bool(getattr(prompt_caps, "image", False))

        logger.info(
            "ACP initialized: agent=%s protocol_version=%s",
            init_result.agent_info,
            init_result.protocol_version,
        )
        logger.info("ACP prompt capabilities: image=%s", self._supports_image)

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

    @property
    def supports_image(self) -> bool:
        """Whether the connected agent supports image blocks in prompts."""
        return self._supports_image

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

    # --- Content accumulation (called by NcatAcpClient.session_update) ---

    def accumulate_part(self, session_id: str, part: ContentPart) -> None:
        """Accumulate a content part for a session (called from session_update callback)."""
        if session_id in self._accumulators:
            self._accumulators[session_id].append(part)

    # --- Prompt sending ---

    def is_busy(self, chat_id: str) -> bool:
        """Check if there is an active prompt for this chat."""
        return chat_id in self._active_prompts

    async def send_prompt(self, chat_id: str, prompt: Sequence[PromptBlock]) -> list[ContentPart]:
        """Send a prompt to the agent and wait for the complete response.

        Returns the accumulated response parts.
        Raises RuntimeError if the agent is not running.
        Raises Exception on agent errors (caller should handle).
        """
        if not self.is_running:
            raise RuntimeError("Agent is not running")

        assert self._conn is not None

        # Get or create session
        session_id = await self.get_or_create_session(chat_id)

        # Initialize content accumulator for this session
        self._accumulators[session_id] = []
        self._active_prompts.add(chat_id)

        try:
            # Send prompt and wait for the turn to complete.
            # During this await, session_update callbacks fire to accumulate text.
            response = await self._conn.prompt(
                session_id=session_id,
                prompt=list(prompt),
            )

            # Collect accumulated content
            parts = self._accumulators.pop(session_id, [])
            text_len = sum(len(p.text) for p in parts if p.type == "text")

            logger.info(
                "Prompt completed for %s (session %s): stop_reason=%s, text=%d chars, parts=%d",
                chat_id,
                session_id,
                response.stop_reason,
                text_len,
                len(parts),
            )
            return parts

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
