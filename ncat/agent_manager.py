"""ACP session orchestrator — maps QQ chats to ACP sessions.

This module contains `AgentManager`, which maps QQ chat IDs to ACP sessions,
accumulates agent response content, and orchestrates prompt sending/cancellation.

Agent subprocess lifecycle is handled by `ncat.agent_process.AgentProcess`.
Protocol callbacks (e.g. `session_update`, `request_permission`) are implemented
by `ncat.acp_client.NcatAcpClient`.
"""

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ncat.acp_client import NcatAcpClient
from ncat.agent_process import AgentProcess, PromptBlock
from ncat.config import McpServerConfig
from ncat.models import ContentPart

if TYPE_CHECKING:
    from ncat.permission import PermissionBroker

logger = logging.getLogger("ncat.agent_manager")

# Reply text when agent is not connected (used by dispatcher, command, and send_prompt)
MSG_AGENT_NOT_CONNECTED = "Agent 未连接，请稍后再试。"


class AgentErrorWithPartialContent(Exception):
    """Raised when the agent errors mid-stream; carries any content already received.

    Callers can send the partial content to the user first, then report the error.
    """

    def __init__(self, cause: BaseException, partial_parts: list[ContentPart]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.partial_parts = partial_parts


class AgentManager:
    """Orchestrates ACP sessions, prompt sending, and content accumulation.

    Responsibilities:
    - Map chat_id to ACP session_id
    - Accumulate response content from session_update notifications
    - Send prompts and return complete responses
    - Handle cancellation via session/cancel
    - Delegate subprocess lifecycle to AgentProcess
    """

    def __init__(
        self,
        command: str,
        args: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        mcp_servers: list[McpServerConfig] | None = None,
        initialize_timeout_seconds: float = 30.0,
        retry_interval_seconds: float = 10.0,
    ) -> None:
        self._process = AgentProcess(
            command=command,
            args=args,
            cwd=cwd,
            env=env,
        )
        self._initialize_timeout_seconds = initialize_timeout_seconds
        self._retry_interval_seconds = retry_interval_seconds
        self._connection_task: asyncio.Task | None = None

        self._mcp_servers = mcp_servers or []

        # Chat-to-session mapping: chat_id -> ACP session_id
        self._sessions: dict[str, str] = {}
        # Content accumulators: session_id -> ordered content parts
        self._accumulators: dict[str, list[ContentPart]] = {}
        # Active prompt tracking: chat_id -> True while prompt is in flight
        self._active_prompts: set[str] = set()

        # Permission broker (set externally after construction)
        self._permission_broker: PermissionBroker | None = None
        # Last event dict per chat (needed for permission reply routing)
        self._last_events: dict[str, dict] = {}

        # One-time cwd for the next session creation only (plan: /new [<dir>]).
        # Key: chat_id; value: dir to send in session/new (None = send empty, FAG uses default).
        # Cleared when used in _create_session; not persisted.
        self._next_session_cwd: dict[str, str | None] = {}

    # --- Lifecycle (delegated to AgentProcess) ---

    async def start(self) -> None:
        """Start the connection loop in the background (non-blocking)."""
        client = NcatAcpClient(self)
        self._connection_task = asyncio.create_task(self._connection_loop(client))

    async def _connection_loop(self, client: NcatAcpClient) -> None:
        """Retry connecting to the agent with fixed interval; wait for disconnect then retry."""
        while True:
            try:
                await self._process.start_once(client, self._initialize_timeout_seconds)
                await self._process.wait()
            except asyncio.CancelledError:
                logger.info("Agent connection loop cancelled")
                break
            except Exception as e:
                logger.warning(
                    "Agent connection failed (will retry in %.1fs): %s",
                    self._retry_interval_seconds,
                    e,
                )
            await asyncio.sleep(self._retry_interval_seconds)

    async def stop(self) -> None:
        """Stop the connection loop and the agent subprocess."""
        if self._connection_task is not None:
            self._connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connection_task
            self._connection_task = None
        self._sessions.clear()
        self._accumulators.clear()
        self._active_prompts.clear()
        self._last_events.clear()
        self._next_session_cwd.clear()
        await self._process.stop()

    @property
    def is_running(self) -> bool:
        """Check if the agent process is alive."""
        return self._process.is_running

    @property
    def supports_image(self) -> bool:
        """Whether the connected agent supports image blocks in prompts."""
        return self._process.supports_image

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

    def set_next_session_cwd(self, chat_id: str, dir_or_none: str | None) -> None:
        """Set cwd for the next session creation only (used by /new [<dir>]).

        dir_or_none: None = send empty cwd (FAG uses default); str = send this dir, FAG concatenates with workspace_base.
        Each call overwrites any previous value for this chat. Consumed once in _create_session.
        """
        self._next_session_cwd[chat_id] = dir_or_none

    async def _create_session(self, chat_id: str) -> str:
        """Create a new ACP session for the given chat_id."""
        conn = self._process.conn
        if conn is None:
            raise RuntimeError(MSG_AGENT_NOT_CONNECTED)

        # One-time cwd: pop so we don't persist. None = send empty string (FAG uses default_cwd)
        cwd_val = self._next_session_cwd.pop(chat_id, None)
        # ACP library expects str; send "" for "use FAG default", or the dir string for FAG to concatenate
        cwd = cwd_val if cwd_val is not None else ""

        # Convert config objects to ACP-compatible dicts
        mcp_servers_payload = []
        for server in self._mcp_servers:
            if server.transport == "sse":
                if not server.url:
                    logger.warning("MCP server %s (sse) missing URL, skipping", server.name)
                    continue
                item = {
                    "type": "sse",
                    "name": server.name,
                    "url": server.url,
                    "headers": [],  # Required field
                }
                mcp_servers_payload.append(item)
            elif server.transport == "stdio":
                if not server.command:
                    logger.warning("MCP server %s (stdio) missing command, skipping", server.name)
                    continue
                item = {
                    "name": server.name,
                    "command": server.command,
                    "args": server.args or [],
                    "env": [
                        {"name": k, "value": v} for k, v in (server.env or {}).items()
                    ],
                }
                mcp_servers_payload.append(item)

        if mcp_servers_payload:
            logger.info(
                "Configuring session with MCP servers: %s",
                [s.get("name") for s in mcp_servers_payload],
            )
        else:
            logger.info("No MCP servers configured for this session")

        session = await conn.new_session(
            cwd=cwd,
            mcp_servers=mcp_servers_payload,
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
        """Accumulate a content part for a session.

        Called from NcatAcpClient.session_update callback.
        """
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
            raise RuntimeError(MSG_AGENT_NOT_CONNECTED)

        conn = self._process.conn
        if conn is None:
            raise RuntimeError(MSG_AGENT_NOT_CONNECTED)

        # Get or create session
        session_id = await self.get_or_create_session(chat_id)

        # Initialize content accumulator for this session
        self._accumulators[session_id] = []
        self._active_prompts.add(chat_id)

        try:
            # Send prompt and wait for the turn to complete.
            # During this await, session_update callbacks fire
            # to accumulate content parts.
            response = await conn.prompt(
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

        except Exception as e:
            # Propagate partial content so the user can see what was already streamed
            partial_parts = self._accumulators.pop(session_id, [])
            raise AgentErrorWithPartialContent(e, partial_parts) from e

        finally:
            self._active_prompts.discard(chat_id)

    # --- Cancellation ---

    async def cancel(self, chat_id: str) -> bool:
        """Send a cancel notification for the active prompt of a chat.

        Returns True if a session existed and cancel was sent,
        False otherwise.
        """
        session_id = self._sessions.get(chat_id)
        conn = self._process.conn
        if session_id is None or conn is None:
            return False

        if chat_id not in self._active_prompts:
            return False

        logger.info(
            "Sending cancel for chat %s (session %s)",
            chat_id,
            session_id,
        )
        await conn.cancel(session_id=session_id)
        return True
