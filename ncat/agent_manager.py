"""ACP session orchestrator — maps QQ chats to independent ACP connections.

This module contains `AgentManager`, which manages multiple independent ACP
connections (one per QQ chat), orchestrates prompt sending/cancellation, and
tracks both the long-lived ACP session and the current prompt turn state.

Each chat has its own AgentConnection containing an AgentProcess and NcatAcpClient.
Protocol callbacks (e.g. `session_update`, `request_permission`) are implemented
by `ncat.acp_client.NcatAcpClient`.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from ncat.acp_client import NcatAcpClient
from ncat.agent_connection import AgentConnection
from ncat.agent_process import AgentProcess, PromptBlock
from ncat.config import McpServerConfig
from ncat.log import info_event, warning_event
from ncat.models import ContentPart, VisibleTurnEvent

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
    - Persist one ACP session per chat until /new or a hard error
    - Accumulate streamed response content for the active prompt turn only
    - Send prompts and return complete responses
    - Handle cancellation via session/cancel
    - Delegate subprocess lifecycle to AgentProcess (via AgentConnection)
    """

    def __init__(
        self,
        command: str,
        args: list[str],
        workspace_root: str,
        default_workspace: str,
        env: dict[str, str] | None = None,
        log_extra_context_env_var: str | None = None,
        mcp_servers: list[McpServerConfig] | None = None,
        initialize_timeout_seconds: float = 30.0,
        retry_interval_seconds: float = 10.0,
    ) -> None:
        self._command = command
        self._args = args
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._default_workspace = default_workspace
        self._default_workspace_path = self._resolve_workspace_path(default_workspace)
        self._env = env
        self._log_extra_context_env_var = log_extra_context_env_var
        self._initialize_timeout_seconds = initialize_timeout_seconds
        self._retry_interval_seconds = retry_interval_seconds
        self._mcp_servers = mcp_servers or []

        # Multi-connection management: chat_id -> AgentConnection
        self._connections: dict[str, AgentConnection] = {}

        # One-time cwd for the next session creation only.
        self._next_session_cwd: dict[str, str] = {}

        # PromptRunner callbacks invoked when a user-visible event boundary arrives.
        self._visible_event_notifiers: dict[str, asyncio.Task[None] | None] = {}
        self._visible_event_callbacks: dict[str, Callable[[], Awaitable[None]]] = {}

        # Lock for creating new connections (to avoid race conditions)
        self._connection_locks: dict[str, asyncio.Lock] = {}

    # --- Connection management ---

    def _get_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create a lock for the given chat_id."""
        if chat_id not in self._connection_locks:
            self._connection_locks[chat_id] = asyncio.Lock()
        return self._connection_locks[chat_id]

    def set_visible_event_notifier(self, chat_id: str, notifier) -> None:
        """Register or clear the callback fired for visible turn events."""
        if notifier is None:
            self._visible_event_callbacks.pop(chat_id, None)
            task = self._visible_event_notifiers.pop(chat_id, None)
            if task is not None:
                task.cancel()
            return

        self._visible_event_callbacks[chat_id] = notifier

    def _resolve_workspace_path(self, workspace: str | None) -> str:
        """Resolve a workspace name to an absolute path under workspace_root."""
        name = self._default_workspace if workspace is None else workspace.strip()
        if not name:
            name = self._default_workspace

        candidate = Path(name)
        if candidate.is_absolute():
            raise ValueError("工作区名称不能是绝对路径。")

        resolved = (self._workspace_root / candidate).resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise ValueError("工作区名称不能逃逸出 workspace_root。") from exc
        return str(resolved)

    def _get_connection_cwd(self, chat_id: str) -> str:
        """Choose the cwd used when starting the agent subprocess for a chat."""
        conn = self._connections.get(chat_id)
        if conn and conn.workspace_cwd is not None:
            return conn.workspace_cwd
        return self._next_session_cwd.get(chat_id, self._default_workspace_path)

    def _workspace_name_from_cwd(self, cwd: str) -> str:
        """Return the workspace-relative name for a resolved cwd."""
        try:
            return Path(cwd).resolve().relative_to(self._workspace_root).as_posix()
        except ValueError:
            return Path(cwd).name

    def _get_or_create_connection(self, chat_id: str) -> AgentConnection:
        """Get or create an AgentConnection for the given chat_id.

        Note: This does NOT start the agent process; call ensure_connection() first.
        """
        if chat_id not in self._connections:
            # Create new connection
            workspace_cwd = self._get_connection_cwd(chat_id)
            agent_process = AgentProcess(
                command=self._command,
                args=self._args,
                cwd=workspace_cwd,
                env=self._env,
                log_extra_context_env_var=self._log_extra_context_env_var,
            )
            acp_client = NcatAcpClient(agent_manager=self, chat_id=chat_id)
            connection = AgentConnection(
                chat_id=chat_id,
                acp_client=acp_client,
                agent_process=agent_process,
                workspace_cwd=workspace_cwd,
            )
            self._connections[chat_id] = connection
        else:
            conn = self._connections[chat_id]
            desired_cwd = self._get_connection_cwd(chat_id)
            if conn.workspace_cwd != desired_cwd:
                conn.workspace_cwd = desired_cwd
                conn.agent_process.set_cwd(desired_cwd)
        return self._connections[chat_id]

    # --- Lifecycle (on-demand connection, no background loop) ---

    async def start(self) -> None:
        """No-op: ncat connects lazily on the first prompt."""
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

            conn.active_session_id = None
            conn.active_turn_session_id = None
            conn.turn_accumulator.clear()
            conn.visible_turn_events.clear()
            conn.visible_turn_event_keys.clear()
            conn.turn_update_count = 0
            conn.active_prompt = False
            conn.workspace_cwd = self._get_connection_cwd(chat_id)
            workspace_name = self._workspace_name_from_cwd(conn.workspace_cwd)
            _, extra_context = conn.agent_process.build_log_extra_context(
                chat_id=chat_id,
                workspace_name=workspace_name,
                spawn_id=conn.spawn_id,
            )
            if not conn.spawn_id:
                conn.spawn_id = str(extra_context.get("spawn_id") or "") or None
            conn.extra_log_context = dict(extra_context)
            Path(conn.workspace_cwd).mkdir(parents=True, exist_ok=True)
            conn.agent_process.set_cwd(conn.workspace_cwd)
            info_event(
                logger,
                "agent_connect_start",
                "Ensuring agent connection on demand",
                chat_id=chat_id,
                cwd=conn.workspace_cwd,
                spawn_id=conn.spawn_id,
            )
            try:
                _, extra_context = await conn.agent_process.start_once(
                    conn.acp_client,
                    self._initialize_timeout_seconds,
                    chat_id=chat_id,
                    workspace_name=workspace_name,
                    spawn_id=conn.spawn_id,
                )
                conn.extra_log_context = dict(extra_context)
                if not conn.spawn_id:
                    conn.spawn_id = str(extra_context.get("spawn_id") or "") or None
                info_event(
                    logger,
                    "agent_connect_ok",
                    "Agent connection established",
                    chat_id=chat_id,
                    spawn_id=conn.spawn_id,
                )
            except Exception as e:
                warning_event(
                    logger,
                    "agent_connect_fail",
                    "Agent connection failed",
                    chat_id=chat_id,
                    spawn_id=conn.spawn_id,
                    err=str(e),
                )
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
                conn.active_session_id = None
                conn.active_turn_session_id = None
                conn.turn_accumulator.clear()
                conn.visible_turn_events.clear()
                conn.visible_turn_event_keys.clear()
                conn.turn_update_count = 0
                conn.active_prompt = False
                conn.spawn_id = None
                conn.extra_log_context.clear()
                info_event(logger, "agent_disconnect", "Agent disconnected", chat_id=chat_id)
        else:
            # Disconnect all chats
            chat_ids = list(self._connections.keys())
            for cid in chat_ids:
                conn = self._connections[cid]
                await conn.agent_process.stop()
                conn.active_session_id = None
                conn.active_turn_session_id = None
                conn.turn_accumulator.clear()
                conn.visible_turn_events.clear()
                conn.visible_turn_event_keys.clear()
                conn.turn_update_count = 0
                conn.active_prompt = False
                conn.spawn_id = None
                conn.extra_log_context.clear()
            self._connections.clear()
            info_event(logger, "agent_disconnect_all", "All agent connections closed")

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
        Each chat reuses a single ACP session until /new or a hard error clears it.
        """
        conn = self._get_or_create_connection(chat_id)
        if conn.active_session_id is not None:
            return conn.active_session_id

        return await self._create_session(chat_id)

    def set_next_session_cwd(self, chat_id: str, dir_or_none: str | None) -> None:
        """Set cwd for the next session creation only (used by /new [<workspace>])."""
        self._next_session_cwd[chat_id] = self._resolve_workspace_path(dir_or_none)

    async def _create_session(self, chat_id: str) -> str:
        """Create a new ACP session for the given chat_id."""
        conn = self._get_or_create_connection(chat_id)
        acp_conn = conn.agent_process.conn
        if acp_conn is None:
            raise RuntimeError(MSG_AGENT_NOT_CONNECTED)

        cwd = (
            self._next_session_cwd.pop(chat_id, None)
            or conn.workspace_cwd
            or self._default_workspace_path
        )
        Path(cwd).mkdir(parents=True, exist_ok=True)
        conn.workspace_cwd = cwd
        conn.agent_process.set_cwd(cwd)

        # Convert config objects to ACP-compatible dicts
        mcp_servers_payload = []
        for server in self._mcp_servers:
            if server.transport == "sse":
                if not server.url:
                    warning_event(
                        logger,
                        "mcp_config_invalid",
                        "Skipping MCP server with missing URL",
                        transport="sse",
                        server_name=server.name,
                    )
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
                    warning_event(
                        logger,
                        "mcp_config_invalid",
                        "Skipping MCP server with missing command",
                        transport="stdio",
                        server_name=server.name,
                    )
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
            info_event(
                logger,
                "mcp_session_configured",
                "Configuring session with MCP servers",
                chat_id=chat_id,
                mcp_servers=[s.get("name") for s in mcp_servers_payload],
            )
        else:
            info_event(
                logger,
                "mcp_session_empty",
                "No MCP servers configured for this session",
                chat_id=chat_id,
            )

        session = await acp_conn.new_session(
            cwd=cwd,
            mcp_servers=mcp_servers_payload,
        )
        session_id = session.session_id
        conn.active_session_id = session_id
        conn.active_turn_session_id = None
        conn.turn_accumulator.clear()
        conn.visible_turn_events.clear()
        conn.visible_turn_event_keys.clear()
        conn.turn_update_count = 0
        info_event(
            logger,
            "session_create_ok",
            "Created ACP session",
            chat_id=chat_id,
            session_id=session_id,
            cwd=cwd,
        )
        return session_id

    async def close_session(self, chat_id: str) -> None:
        """Forget the active ACP session for a chat.

        ACP has no explicit session close in the currently used API surface.
        ncat therefore stops referencing the old session locally so that the
        next interaction creates a fresh session.
        """
        conn = self._connections.get(chat_id)
        if conn:
            conn.active_session_id = None
            conn.active_turn_session_id = None
            conn.turn_accumulator.clear()
            conn.visible_turn_events.clear()
            conn.visible_turn_event_keys.clear()
            conn.turn_update_count = 0
            conn.active_prompt = False
            info_event(logger, "session_close", "Closed session", chat_id=chat_id)

    async def close_all_sessions(self) -> None:
        """Close all active sessions (e.g. on NapCat disconnect)."""
        chat_ids = list(self._connections.keys())
        for chat_id in chat_ids:
            await self.close_session(chat_id)
        info_event(logger, "session_close_all", "All sessions closed")

    # --- Content accumulation (called by NcatAcpClient.session_update) ---

    def accumulate_part(self, chat_id: str, session_id: str, part: ContentPart) -> None:
        """Accumulate a content part for the current prompt turn.

        Called from NcatAcpClient.session_update callback.
        Uses chat_id to locate the correct AgentConnection and only records
        updates that belong to the active turn.
        """
        conn = self._connections.get(chat_id)
        if (
            conn
            and conn.active_prompt
            and conn.active_turn_session_id == session_id
        ):
            conn.turn_accumulator.append(part)
            conn.turn_update_count += 1

    def record_visible_event(
        self,
        chat_id: str,
        session_id: str,
        event: VisibleTurnEvent,
    ) -> bool:
        """Record a deduplicated user-visible turn event for the active prompt."""
        conn = self._connections.get(chat_id)
        if (
            conn is None
            or not conn.active_prompt
            or conn.active_turn_session_id != session_id
            or event.key in conn.visible_turn_event_keys
        ):
            return False

        event.part_count = len(conn.turn_accumulator)
        conn.visible_turn_events.append(event)
        conn.visible_turn_event_keys.add(event.key)
        conn.turn_update_count += 1
        self._notify_visible_event(chat_id)
        return True

    def _notify_visible_event(self, chat_id: str) -> None:
        notifier = self._visible_event_callbacks.get(chat_id)
        if notifier is None:
            return

        task = self._visible_event_notifiers.get(chat_id)
        if task is not None and not task.done():
            return

        async def _run_notifier() -> None:
            try:
                await notifier()
            finally:
                current = self._visible_event_notifiers.get(chat_id)
                if current is asyncio.current_task():
                    self._visible_event_notifiers.pop(chat_id, None)

        self._visible_event_notifiers[chat_id] = asyncio.create_task(_run_notifier())

    def pop_visible_events(self, chat_id: str) -> list[VisibleTurnEvent]:
        """Return and clear pending visible turn events for the active chat."""
        conn = self._connections.get(chat_id)
        if conn is None or not conn.visible_turn_events:
            return []

        events = list(conn.visible_turn_events)
        conn.visible_turn_events.clear()
        return events

    def drain_visible_event_flushes(
        self,
        chat_id: str,
        sent_part_count: int,
    ) -> tuple[list[tuple[list[ContentPart], VisibleTurnEvent]], int]:
        """Drain pending visible events together with text accumulated before each one."""
        conn = self._connections.get(chat_id)
        if conn is None or not conn.visible_turn_events:
            return [], sent_part_count

        flushes: list[tuple[list[ContentPart], VisibleTurnEvent]] = []
        next_sent_part_count = sent_part_count
        for visible_event in conn.visible_turn_events:
            parts = list(conn.turn_accumulator[next_sent_part_count : visible_event.part_count])
            flushes.append((parts, visible_event))
            next_sent_part_count = visible_event.part_count

        conn.visible_turn_events.clear()
        return flushes, next_sent_part_count

    def clear_completed_turn_state(self, chat_id: str) -> None:
        """Clear accumulated turn state after PromptRunner finishes sending outputs."""
        conn = self._connections.get(chat_id)
        if conn is None:
            return

        conn.turn_accumulator.clear()
        conn.visible_turn_events.clear()
        conn.visible_turn_event_keys.clear()

    async def wait_for_turn_settle(
        self,
        chat_id: str,
        *,
        idle_seconds: float = 0.15,
        max_wait_seconds: float = 2.0,
    ) -> None:
        """Wait briefly for trailing session updates after prompt completion."""
        conn = self._connections.get(chat_id)
        if conn is None or not conn.active_prompt:
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_seconds
        last_count = conn.turn_update_count

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return

            await asyncio.sleep(min(idle_seconds, remaining))
            if not conn.active_prompt:
                return

            current_count = conn.turn_update_count
            if current_count == last_count:
                return
            last_count = current_count

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

        # Initialize turn-level state
        conn.turn_accumulator.clear()
        conn.visible_turn_events.clear()
        conn.visible_turn_event_keys.clear()
        conn.turn_update_count = 0
        conn.active_turn_session_id = session_id
        conn.active_prompt = True

        try:
            # Send prompt and wait for the turn to complete.
            # During this await, session_update callbacks fire
            # to accumulate content parts.
            response = await acp_conn.prompt(
                session_id=session_id,
                prompt=list(prompt),
            )

            await self.wait_for_turn_settle(chat_id)

            # Collect accumulated content for this turn only.
            parts = list(conn.turn_accumulator)
            text_len = sum(len(p.text) for p in parts if p.type == "text")

            info_event(
                logger,
                "prompt_complete",
                "Prompt completed",
                chat_id=chat_id,
                session_id=session_id,
                stop_reason=response.stop_reason,
                text_len=text_len,
                part_count=len(parts),
            )
            return parts

        except asyncio.CancelledError:
            raise

        except Exception as e:
            # Propagate partial content so the user can see what was already streamed
            partial_parts = list(conn.turn_accumulator)
            raise AgentErrorWithPartialContent(e, partial_parts) from e

        finally:
            conn.turn_update_count = 0
            conn.active_turn_session_id = None
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

        session_id = conn.active_turn_session_id
        if session_id is None:
            return False

        info_event(
            logger,
            "prompt_cancel",
            "Sending cancel for active prompt",
            chat_id=chat_id,
            session_id=session_id,
        )
        await acp_conn.cancel(session_id=session_id)
        return True
