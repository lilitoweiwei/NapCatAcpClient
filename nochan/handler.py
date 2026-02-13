"""Message processing pipeline — business logic for handling QQ messages."""

import contextlib
import logging
from collections.abc import Awaitable, Callable

from nochan.command import HELP_TEXT, parse_command
from nochan.converter import ParsedMessage, onebot_to_internal
from nochan.opencode import SubprocessOpenCodeBackend
from nochan.prompt import PromptBuilder
from nochan.session import SessionManager

logger = logging.getLogger("nochan.handler")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]


class MessageHandler:
    """
    Processes incoming QQ message events through the full nochan pipeline:
    parse → filter → command/AI → reply.

    Decoupled from WebSocket transport: sends replies via the reply_fn callback.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        opencode_backend: SubprocessOpenCodeBackend,
        prompt_builder: PromptBuilder,
        reply_fn: ReplyFn,
    ) -> None:
        # Session manager for chat-to-opencode session mapping
        self._session_manager = session_manager
        # OpenCode backend for sending prompts and receiving AI responses
        self._opencode_backend = opencode_backend
        # Prompt builder for constructing context-enriched prompts
        self._prompt_builder = prompt_builder
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn

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

            # Step 3: Check for commands
            command = parse_command(parsed.text)
            if command is not None:
                logger.info("Command received: /%s from %s", command, parsed.chat_id)
                await self._handle_command(command, parsed, event)
                return

            # Step 4: Get or create session
            session = await self._session_manager.get_active_session(parsed.chat_id)
            if session is None:
                session = await self._session_manager.create_session(parsed.chat_id)

            # Step 5: Build prompt with context (include session_init on first message)
            is_new_session = session.opencode_session_id is None
            prompt = self._prompt_builder.build(parsed, is_new_session)

            # Step 6: Check queue and send queuing notice if needed
            if self._opencode_backend.is_queue_full():
                await self._reply_fn(event, "AI 正在忙，你的请求已排队，请稍候...")

            # Step 7: Call OpenCode
            response = await self._opencode_backend.send_message(
                session.opencode_session_id, prompt
            )

            # Step 8: Update session with OpenCode session ID if new
            if session.opencode_session_id is None and response.session_id:
                await self._session_manager.update_opencode_session_id(
                    session.id, response.session_id
                )

            # Step 9-10: Convert and send reply
            if response.success and response.content:
                logger.info(
                    "Sending AI reply to %s (%d chars)",
                    parsed.chat_id,
                    len(response.content),
                )
                await self._reply_fn(event, response.content)
            elif response.success and not response.content:
                logger.warning("OpenCode returned empty content for %s", parsed.chat_id)
                await self._reply_fn(event, "AI 未返回有效回复")
            else:
                logger.error(
                    "OpenCode failed for %s: %s",
                    parsed.chat_id,
                    response.error,
                )
                await self._reply_fn(event, "AI 处理出错，请稍后重试")

        except Exception as e:
            logger.error("Error handling message: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await self._reply_fn(event, "处理消息时发生内部错误")

    async def _handle_command(self, command: str, parsed: ParsedMessage, event: dict) -> None:
        """Handle a user command (/new, /help, etc.)."""
        if command == "new":
            # Archive current session and create a new one
            await self._session_manager.archive_active_session(parsed.chat_id)
            await self._session_manager.create_session(parsed.chat_id)
            await self._reply_fn(event, "已创建新会话，AI 上下文已清空。")
            logger.info("New session created for %s", parsed.chat_id)

        elif command == "help" or command == "unknown":
            await self._reply_fn(event, HELP_TEXT)
