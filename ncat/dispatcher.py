"""Message dispatcher — thin orchestrator that routes messages.

Routes incoming QQ message events to either the CommandExecutor (for /commands),
the PermissionBroker (for pending permission replies), or the PromptRunner
(for AI requests). Handles filtering and busy rejection.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from ncat.acp_client import AgentManager
from ncat.command import CommandExecutor
from ncat.converter import onebot_to_internal
from ncat.permission import PermissionBroker
from ncat.prompt_runner import PromptRunner

logger = logging.getLogger("ncat.dispatcher")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]

# Busy rejection message (dispatching-level concern)
_MSG_BUSY = "AI 正在思考中，请等待或使用 /stop 中断。"

# Hint shown when user sends non-numeric text while a permission request is pending
_MSG_PERMISSION_HINT = "当前有待处理的权限请求，请回复编号选择，或使用 /stop 取消。"


class MessageDispatcher:
    """
    Thin dispatcher: parse → filter → route to CommandExecutor,
    PermissionBroker, or PromptRunner.

    Decoupled from WebSocket transport: sends replies via the reply_fn callback.
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        reply_fn: ReplyFn,
        permission_broker: PermissionBroker,
        thinking_notify_seconds: float = 10,
        thinking_long_notify_seconds: float = 30,
    ) -> None:
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn
        # Agent manager (needed for storing last_event)
        self._agent_manager = agent_manager
        # Permission broker for forwarding permission requests to QQ users
        self._permission_broker = permission_broker

        # Prompt lifecycle manager (owns active task tracking)
        self._ai = PromptRunner(
            agent_manager=agent_manager,
            reply_fn=reply_fn,
            permission_broker=permission_broker,
            thinking_notify_seconds=thinking_notify_seconds,
            thinking_long_notify_seconds=thinking_long_notify_seconds,
        )

        # Command executor (cancel_fn bridges to PromptRunner without direct import)
        self._cmd = CommandExecutor(
            agent_manager=agent_manager,
            reply_fn=reply_fn,
            cancel_fn=self._ai.cancel,
        )

    async def handle_message(self, event: dict, bot_id: int) -> None:
        """
        Process an incoming message event through the full pipeline.

        Routing order:
        1. Filter (group @bot check)
        2. Commands (/new, /stop, /help)
        3. Pending permission replies (intercept all non-command messages)
        4. Busy rejection (AI already processing)
        5. AI prompt dispatch

        Args:
            event: Raw OneBot 11 message event dict
            bot_id: The bot's own QQ ID (for @bot detection)
        """
        try:
            # Step 1: Parse the message event
            parsed = onebot_to_internal(event, bot_id)

            # Step 2: Group messages require @bot
            if parsed.message_type == "group" and not parsed.is_at_bot:
                logger.debug(
                    "Ignored group message (no @bot): group=%s user=%s text=%s",
                    parsed.chat_id,
                    parsed.sender_name,
                    parsed.text[:100],
                )
                return

            logger.info(
                "Processing message from %s (%s): %s",
                parsed.sender_name,
                parsed.chat_id,
                parsed.text[:100],
            )

            # Store the latest event for this chat (used by PermissionBroker
            # for reply routing when the agent requests permission)
            self._agent_manager.set_last_event(parsed.chat_id, event)

            # Step 3: Try to handle as a command (lightweight, non-blocking)
            if await self._cmd.try_handle(parsed, event):
                return

            # Step 4: Check for pending permission request — intercept all
            # non-command messages when a permission reply is expected
            if self._permission_broker.has_pending(parsed.chat_id):
                if self._permission_broker.try_resolve(parsed.chat_id, parsed.text):
                    logger.info("Permission resolved by user reply for %s", parsed.chat_id)
                else:
                    # Invalid input — remind user about the pending request
                    await self._reply_fn(event, _MSG_PERMISSION_HINT)
                return

            # Step 5: Reject if AI is already processing for this chat
            if self._ai.is_busy(parsed.chat_id):
                logger.info(
                    "Busy rejection for %s (AI already processing)",
                    parsed.chat_id,
                )
                await self._reply_fn(event, _MSG_BUSY)
                return

            # Step 6: Dispatch to AI prompt runner
            await self._ai.process(parsed, event)

        except asyncio.CancelledError:
            # Let cancellation propagate cleanly (don't send error message for /stop)
            raise
        except Exception as e:
            logger.error("Error handling message: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await self._reply_fn(event, "处理消息时发生内部错误")
