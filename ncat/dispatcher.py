"""Message dispatcher — thin orchestrator that routes messages.

Routes incoming QQ message events to either the unified command system
or the PromptRunner (for AI requests). Handles filtering and busy rejection.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

# Import bg_command to register /bg * commands (must be after command_registry is created)
# This module has side effects: registers commands in command_registry
from ncat import bg_command  # noqa: F401
from ncat.agent_manager import AgentManager
from ncat.bsp_client import BspClient
from ncat.command import command_registry, get_help_text
from ncat.converter import onebot_to_internal
from ncat.log import debug_event, error_event, info_event, warning_event
from ncat.models import ContentPart
from ncat.prompt_runner import PromptRunner

logger = logging.getLogger("ncat.dispatcher")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]
# Type alias for mixed content replies (text + images).
# Signature: async reply_content_fn(event: dict, parts: list[ContentPart]) -> None
ReplyContentFn = Callable[[dict, list[ContentPart]], Awaitable[None]]

# Busy rejection message (dispatching-level concern)
_MSG_BUSY = "AI 正在思考中，请等待或使用 /stop 中断。"


class MessageDispatcher:
    """
    Thin dispatcher: parse → filter → route to command system or PromptRunner.

    Decoupled from WebSocket transport: sends replies via the reply_fn callback.
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        reply_fn: ReplyFn,
        reply_content_fn: ReplyContentFn | None = None,
        thinking_notify_seconds: float = 10,
        thinking_long_notify_seconds: float = 30,
        image_download_timeout: float = 15.0,
        bsp_client: BspClient | None = None,
    ) -> None:
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn
        # Agent manager
        self._agent_manager = agent_manager
        # BSP client for background session management
        self._bsp_client = bsp_client

        async def _reply_content_fallback(event: dict, parts: list[ContentPart]) -> None:
            # Fallback: deliver text-only if the transport doesn't support images.
            text = "".join(p.text for p in parts if p.type == "text")
            await reply_fn(event, text or "AI 未返回有效回复")

        # Prompt lifecycle manager (owns active task tracking)
        self._ai = PromptRunner(
            agent_manager=agent_manager,
            reply_fn=reply_fn,
            reply_content_fn=reply_content_fn or _reply_content_fallback,
            thinking_notify_seconds=thinking_notify_seconds,
            thinking_long_notify_seconds=thinking_long_notify_seconds,
            image_download_timeout=image_download_timeout,
        )

        # Configure command registry with dependencies
        command_registry.set_dependency("agent_manager", agent_manager)
        command_registry.set_dependency("bsp_client", bsp_client)
        command_registry.set_dependency("cancel_fn", self._ai.cancel)

        info_event(
            logger,
            "command_system_ready",
            "Command system initialized",
            command_count=command_registry.get_command_count(),
        )

    async def handle_message(self, event: dict, bot_id: int | None = None) -> None:
        """
        Handle an incoming QQ message event.

        This is the main entry point called by napcat_server.py.
        The bot_id parameter is kept for backward compatibility but not used.

        Args:
            event: Raw QQ message event dict
            bot_id: Bot QQ ID (unused, kept for API compatibility)
        """
        await self.dispatch(event, bot_id)

    async def dispatch(self, event: dict, bot_id: int | None = None) -> None:
        """
        Dispatch an incoming QQ message event.

        Flow:
        1. Convert OneBot event to internal ParsedMessage
        2. Handle /send specially (strip prefix, forward to AI)
        3. Try to handle as a command
        4. Reject if AI is busy
        5. Dispatch to AI prompt runner

        Args:
            event: Raw QQ message event dict
        """
        try:
            # Step 1: Convert OneBot event to internal message format
            if bot_id is None:
                warning_event(
                    logger,
                    "message_ignored",
                    "Received message before bot_id was available",
                )
                return

            parsed = onebot_to_internal(event, bot_id)
            if not parsed:
                return

            if parsed.message_type == "group" and not parsed.is_at_bot:
                return

            chat_id = parsed.chat_id
            debug_event(
                logger,
                "message_received",
                "Received message",
                chat_id=chat_id,
                message_type=parsed.message_type,
                msg_preview=parsed.text[:50] + "..." if len(parsed.text) > 50 else parsed.text,
            )

            # Step 2: Handle /send command specially (strip prefix, forward to AI)
            send_forwarded = False
            if parsed.text.startswith("/send "):
                body = parsed.text[6:].strip()
                if not body:
                    # /send with no payload - show usage hint
                    await self._reply_fn(event, get_help_text())
                    return
                parsed.text = body
                send_forwarded = True

            # Step 3: Try to handle as a command (skip if /send forwarded)
            if not send_forwarded:
                try:
                    matched = await command_registry.execute(
                        parsed.text,
                        chat_id=chat_id,
                        event=event,
                        reply_fn=self._reply_fn,
                    )
                    if matched:
                        return
                    if parsed.text.startswith("/"):
                        await self._reply_fn(event, get_help_text())
                        return
                except Exception:
                    # Error already logged by command handler
                    return

            # Step 4: Reject if AI is already processing for this chat
            if self._ai.is_busy(chat_id):
                info_event(
                    logger,
                    "message_rejected_busy",
                    "Busy rejection while AI is already processing",
                    chat_id=chat_id,
                )
                await self._reply_fn(event, _MSG_BUSY)
                return

            # Step 5: Dispatch to AI prompt runner
            # (connection is established on demand in send_prompt)
            debug_event(
                logger,
                "message_dispatched",
                "Dispatching message to AI",
                chat_id=chat_id,
                agent_running=self._agent_manager.is_running(chat_id),
            )
            await self._ai.process(parsed, event)

        except asyncio.CancelledError:
            # Let cancellation propagate cleanly (don't send error message for /stop)
            raise
        except Exception as e:
            error_event(
                logger,
                "message_handle_fail",
                "Error handling message",
                chat_id=event.get("user_id") or event.get("group_id"),
                err=str(e),
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await self._reply_fn(event, "处理消息时发生内部错误")
