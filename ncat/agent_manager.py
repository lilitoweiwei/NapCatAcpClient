"""ACP session orchestrator — maps QQ chats to ACP sessions.

This module contains `AgentManager`, which maps QQ chat IDs to ACP sessions,
accumulates agent response content, and orchestrates prompt sending/cancellation.

Agent subprocess lifecycle is handled by `ncat.agent_process.AgentProcess`.
Protocol callbacks (e.g. `session_update`, `request_permission`) are implemented
by `ncat.acp_client.NcatAcpClient`.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ncat.acp_client import NcatAcpClient
from ncat.agent_process import AgentProcess, PromptBlock
from ncat.models import ContentPart

if TYPE_CHECKING:
    from ncat.permission import PermissionBroker

logger = logging.getLogger("ncat.agent_manager")


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
    ) -> None:
        # Agent subprocess and ACP connection manager
        self._process = AgentProcess(
            command=command,
            args=args,
            cwd=cwd,
            env=env,
        )

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

    # --- Lifecycle (delegated to AgentProcess) ---

    async def start(self) -> None:
        """Start the agent subprocess and initialize the ACP connection."""
        # Create the ACP client with a back-reference to this manager
        client = NcatAcpClient(self)
        await self._process.start(client)

    async def stop(self) -> None:
        """Stop the agent subprocess and clean up all session state."""
        # Clear all session mappings and event references
        self._sessions.clear()
        self._accumulators.clear()
        self._active_prompts.clear()
        self._last_events.clear()

        # Delegate process and connection shutdown
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

    async def _create_session(self, chat_id: str) -> str:
        """Create a new ACP session for the given chat_id."""
        conn = self._process.conn
        assert conn is not None, "Agent not started"

        session = await conn.new_session(
            cwd=self._process.cwd,
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
            raise RuntimeError("Agent is not running")

        conn = self._process.conn
        assert conn is not None

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
