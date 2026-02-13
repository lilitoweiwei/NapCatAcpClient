"""Message processing pipeline — thin orchestrator that routes messages.

Routes incoming QQ message events to either the CommandExecutor (for /commands)
or the AiProcessor (for AI requests). Handles filtering and busy rejection.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from nochan.ai_processor import AiProcessor
from nochan.command import CommandExecutor, parse_command
from nochan.converter import onebot_to_internal
from nochan.opencode import SubprocessOpenCodeBackend
from nochan.prompt import PromptBuilder
from nochan.session import SessionManager

logger = logging.getLogger("nochan.handler")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]

# Busy rejection message (dispatching-level concern)
_MSG_BUSY = "AI 正在思考中，请等待或使用 /stop 中断。"


class MessageHandler:
    """
    Thin orchestrator: parse → filter → route to CommandExecutor or AiProcessor.

    Decoupled from WebSocket transport: sends replies via the reply_fn callback.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        opencode_backend: SubprocessOpenCodeBackend,
        prompt_builder: PromptBuilder,
        reply_fn: ReplyFn,
        thinking_notify_seconds: float = 10,
        thinking_long_notify_seconds: float = 30,
    ) -> None:
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn

        # AI request lifecycle manager (owns active task tracking)
        self._ai = AiProcessor(
            session_manager=session_manager,
            opencode_backend=opencode_backend,
            prompt_builder=prompt_builder,
            reply_fn=reply_fn,
            thinking_notify_seconds=thinking_notify_seconds,
            thinking_long_notify_seconds=thinking_long_notify_seconds,
        )

        # Command executor (cancel_fn bridges to AiProcessor without direct import)
        self._cmd = CommandExecutor(
            session_manager=session_manager,
            reply_fn=reply_fn,
            cancel_fn=self._ai.cancel,
        )

    async def handle_message(self, event: dict, bot_id: int) -> None:
        """
        Process an incoming message event through the full pipeline.

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

            # Step 3: Check for commands (lightweight, non-blocking)
            command = parse_command(parsed.text)
            if command is not None:
                logger.info(
                    "Command received: /%s from %s", command, parsed.chat_id
                )
                await self._cmd.execute(command, parsed, event)
                return

            # Step 4: Reject if AI is already processing for this chat
            if self._ai.is_busy(parsed.chat_id):
                logger.info(
                    "Busy rejection for %s (AI already processing)",
                    parsed.chat_id,
                )
                await self._reply_fn(event, _MSG_BUSY)
                return

            # Step 5: Dispatch to AI processor
            await self._ai.process(parsed, event)

        except asyncio.CancelledError:
            # Let cancellation propagate cleanly (don't send error message for /stop)
            raise
        except Exception as e:
            logger.error("Error handling message: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await self._reply_fn(event, "处理消息时发生内部错误")
