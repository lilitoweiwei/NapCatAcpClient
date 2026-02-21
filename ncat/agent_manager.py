"""ACP session orchestrator — maps QQ chats to independent ACP connections.

This module contains `AgentManager`, which manages multiple independent ACP
connections (one per QQ chat), orchestrates prompt sending/cancellation, and
accumulates response content from session_update notifications.

Each chat has its own AgentConnection containing an AgentProcess and NcatAcpClient.
Protocol callbacks (e.g. `session_update`, `request_permission`) are implemented
by `ncat.acp_client.NcatAcpClient`.
"""

import asyncio
import logging
from collections.abc import Sequence

from ncat.acp_client import NcatAcpClient
from ncat.agent_connection import AgentConnection
from ncat.agent_process import AgentProcess, PromptBlock
from ncat.config import McpServerConfig
from ncat.models import ContentPart

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
    """Orchestrates multiple ACP connections, prompt sending, and content accumulation.

    Responsibilities:
    - Map chat_id to independent AgentConnection (one per chat)
    - Accumulate response content from session_update notifications
    - Send prompts and return complete responses
    - Handle cancellation via session/cancel
    - Delegate subprocess lifecycle to AgentProcess (via AgentConnection)
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
        self._command = command
        self._args = args
        self._cwd = cwd
        self._env = env
        self._initialize_timeout_seconds = initialize_timeout_seconds
        self._retry_interval_seconds = retry_interval_seconds
        self._mcp_servers = mcp_servers or []

        # Multi-connection management: chat_id -> AgentConnection
        self._connections: dict[str, AgentConnection] = {}

        # One-time cwd for the next session creation only (plan: /new [<dir>]).
        # Key: chat_id; value: dir to send in session/new (None = send empty, FAG uses default).
        # Cleared when used in _create_session; not persisted.
        self._next_session_cwd: dict[str, str | None] = {}

        # Lock for creating new connections (to avoid race conditions)
        self._connection_locks: dict[str, asyncio.Lock] = {}

    # --- Connection management ---

    def _get_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create a lock for the given chat_id."""
        if chat_id not in self._connection_locks:
            self._connection_locks[chat_id] = asyncio.Lock()
        return self._connection_locks[chat_id]

    def _get_or_create_connection(self, chat_id: str) -> AgentConnection:
        """Get or create an AgentConnection for the given chat_id.

        Note: This does NOT start the agent process; call ensure_connection() first.
        """
        if chat_id not in self._connections:
            # Create new connection
            agent_process = AgentProcess(
                command=self._command,
                args=self._args,
                cwd=self._cwd,
                env=self._env,
            )
            acp_client = NcatAcpClient(agent_manager=self, chat_id=chat_id)
            connection = AgentConnection(
                chat_id=chat_id,
                acp_client=acp_client,
                agent_process=agent_process,
                accumulators={},
            )
            self._connections[chat_id] = connection
        return self._connections[chat_id]

    # --- Lifecycle (on-demand connection, no background loop) ---

    async def start(self) -> None:
        """No-op: ncat does not connect at startup; connection is established on first send_prompt."""
        pass

    async def ensure_connection(self, chat_id: str) -> None:
        """Establish connection to the agent for the given chat_id if not already connected.

        Idempotent under lock for the specific chat_id.
        """
        conn = self._get_or_create_connection(chat_id)
        if conn.is_running:
            return

        lock = self._get_lock(chat_id)
        async with lock:
            # Double-check after acquiring lock
            if conn.is_running:
                return

            logger.info("Ensuring agent connection for chat %s (on-demand)...", chat_id)
            try:
                await conn.agent_process.start_once(
                    conn.acp_client, self._initialize_timeout_seconds
                )
                logger.info("Agent connection established for chat %s", chat_id)
            except Exception as e:
                logger.warning("Agent connection failed for chat %s: %s", chat_id, e)
                raise

    async def disconnect(self, chat_id: str | None = None) -> None:
        """Stop the agent subprocess and clear session state.

        If chat_id is provided, only disconnect that specific chat.
        If chat_id is None, disconnect all chats.

        Call after /new or NapCat disconnect.
        """
        if chat_id is not None:
            # Disconnect specific chat
            conn = self._connections.pop(chat_id, None)
            if conn:
                await conn.agent_process.stop()
                conn.accumulators.clear()
                conn.active_prompt = False
                logger.info("Agent disconnected for chat %s", chat_id)
        else:
            # Disconnect all chats
            chat_ids = list(self._connections.keys())
            for cid in chat_ids:
                conn = self._connections[cid]
                await conn.agent_process.stop()
                conn.accumulators.clear()
                conn.active_prompt = False
            self._connections.clear()
            logger.info("All agent connections closed")

    async def stop(self) -> None:
        """Stop all agent subprocesses and clear state (e.g. on ncat shutdown)."""
        await self.disconnect()

    def is_running(self, chat_id: str) -> bool:
        """Check if the agent process for the given chat is alive."""
        conn = self._connections.get(chat_id)
        return conn.is_running if conn else False

    def supports_image(self, chat_id: str) -> bool:
        """Whether the connected agent for the given chat supports image blocks in prompts."""
        conn = self._connections.get(chat_id)
        return conn.supports_image if conn else False

    # --- Session management ---

    async def get_or_create_session(self, chat_id: str) -> str:
        """Get existing ACP session for chat_id, or create a new one.

        Returns the ACP session_id.
        In the multi-connection design, each chat has its own connection,
        so we always create a new session if the connection doesn't have one.
        """
        conn = self._get_or_create_connection(chat_id)
        # Check if connection already has an active session (has accumulators)
        if conn.accumulators:
            # Return existing session_id
            session_id = next(iter(conn.accumulators.keys()))
            return session_id

        return await self._create_session(chat_id)

    def set_next_session_cwd(self, chat_id: str, dir_or_none: str | None) -> None:
        """Set cwd for the next session creation only (used by /new [<dir>]).

        dir_or_none: None = send empty (FAG default), else dir for FAG to concatenate
        Each call overwrites any previous value for this chat. Consumed once in _create_session.
        """
        self._next_session_cwd[chat_id] = dir_or_none

    async def _create_session(self, chat_id: str) -> str:
        """Create a new ACP session for the given chat_id."""
        conn = self._get_or_create_connection(chat_id)
        acp_conn = conn.agent_process.conn
        if acp_conn is None:
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

        session = await acp_conn.new_session(
            cwd=cwd,
            mcp_servers=mcp_servers_payload,
        )
        session_id = session.session_id
        conn.accumulators[session_id] = []
        logger.info("Created ACP session %s for chat %s", session_id, chat_id)
        return session_id

    async def close_session(self, chat_id: str) -> None:
        """Close and remove the ACP session for a chat.

        ACP has no explicit session close; we simply remove the mapping
        so a new session will be created on next interaction.
        """
        conn = self._connections.get(chat_id)
        if conn:
            # Clean up any pending accumulator
            conn.accumulators.clear()
            logger.info("Closed session for chat %s", chat_id)

    async def close_all_sessions(self) -> None:
        """Close all active sessions (e.g. on NapCat disconnect)."""
        chat_ids = list(self._connections.keys())
        for chat_id in chat_ids:
            await self.close_session(chat_id)
        logger.info("All sessions closed")

    # --- Content accumulation (called by NcatAcpClient.session_update) ---

    def accumulate_part(self, chat_id: str, session_id: str, part: ContentPart) -> None:
        """Accumulate a content part for a session.

        Called from NcatAcpClient.session_update callback.
        Uses chat_id to locate the correct AgentConnection.
        """
        conn = self._connections.get(chat_id)
        if conn and session_id in conn.accumulators:
            conn.accumulators[session_id].append(part)

    # --- Prompt sending ---

    def is_busy(self, chat_id: str) -> bool:
        """Check if there is an active prompt for this chat."""
        conn = self._connections.get(chat_id)
        return conn.active_prompt if conn else False

    async def send_prompt(self, chat_id: str, prompt: Sequence[PromptBlock]) -> list[ContentPart]:
        """Send a prompt to the agent and wait for the complete response.

        Connects on demand if not already connected. Returns the accumulated response parts.
        Raises RuntimeError if the agent is not running (e.g. connection failed).
        Raises Exception on agent errors (caller should handle).
        """
        await self.ensure_connection(chat_id)
        if not self.is_running(chat_id):
            raise RuntimeError(MSG_AGENT_NOT_CONNECTED)

        conn = self._get_or_create_connection(chat_id)
        acp_conn = conn.agent_process.conn
        if acp_conn is None:
            raise RuntimeError(MSG_AGENT_NOT_CONNECTED)

        # Get or create session
        session_id = await self.get_or_create_session(chat_id)

        # Initialize content accumulator for this session
        conn.accumulators[session_id] = []
        conn.active_prompt = True

        try:
            # Send prompt and wait for the turn to complete.
            # During this await, session_update callbacks fire
            # to accumulate content parts.
            response = await acp_conn.prompt(
                session_id=session_id,
                prompt=list(prompt),
            )

            # Collect accumulated content
            parts = conn.accumulators.pop(session_id, [])
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
            conn.accumulators.pop(session_id, None)
            raise

        except Exception as e:
            # Propagate partial content so the user can see what was already streamed
            partial_parts = conn.accumulators.pop(session_id, [])
            raise AgentErrorWithPartialContent(e, partial_parts) from e

        finally:
            conn.active_prompt = False

    # --- Cancellation ---

    async def cancel(self, chat_id: str) -> bool:
        """Send a cancel notification for the active prompt of a chat.

        Returns True if a session existed and cancel was sent,
        False otherwise.
        """
        conn = self._connections.get(chat_id)
        if conn is None:
            return False

        acp_conn = conn.agent_process.conn
        if acp_conn is None:
            return False

        if not conn.active_prompt:
            return False

        # Find session_id for this chat
        session_id = None
        for sid in conn.accumulators.keys():
            session_id = sid
            break

        if session_id is None:
            return False

        logger.info(
            "Sending cancel for chat %s (session %s)",
            chat_id,
            session_id,
        )
        await acp_conn.cancel(session_id=session_id)
        return True
